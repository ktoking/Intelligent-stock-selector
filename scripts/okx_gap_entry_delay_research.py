#!/usr/bin/env python3
"""Research whether waiting 5/10/15 minutes improves the V5 opening-gap fade.

Each variant enters at the *next* five-minute bar open after its confirmation
window has completed.  Stops and 30/60/90-minute exits are replayed from that
later entry, so the comparison does not retain the 09:35 fill after observing
additional candles.  This is retrospective research and never enables Demo
orders by itself.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_gap_feature_research import opening_features  # noqa: E402
from scripts.okx_gap_strategy_v3 import load_event_sets, market_regime, metrics  # noqa: E402
from scripts.okx_gap_strategy_v4 import (  # noqa: E402
    SELECTION_FOLDS,
    V4Config,
    chronological_folds,
    label_base_candidates,
    latest_completed_us_session,
)
from scripts.okx_gap_strategy_v5 import (  # noqa: E402
    AdaptiveHorizonConfig,
    add_horizon_outcomes,
    causal_adaptive_horizon_trades,
    risk_weighted_portfolio_metrics,
    robustness_report,
)
from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import (  # noqa: E402
    aggregate_bars,
    load_market_data,
    weekday_sessions,
)
from scripts.okx_research_universe import load_symbols  # noqa: E402

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
OUTPUT = ROOT / "data" / "okx_gap_entry_delay_research.json"
DELAYS = (5, 10, 15)
HORIZONS = (30, 60, 90)
BENCHMARK_SYMBOL = "SPY-USDT-SWAP"
EXCLUDED_SYMBOLS = ("SPY-USDT-SWAP", "QQQ-USDT-SWAP")


def clock_for_open_offset(offset_minutes: int) -> str:
    """Return the 5m bar-start clock at an offset from the 09:30 cash open."""
    if offset_minutes < 0 or offset_minutes % 5:
        raise ValueError("offset_minutes must be a non-negative multiple of five")
    anchor = datetime(2000, 1, 1, 9, 30) + timedelta(minutes=offset_minutes)
    return anchor.strftime("%H:%M")


def confirmation_clock(delay_minutes: int) -> str:
    """Last completed confirmation bar before a delayed entry."""
    return clock_for_open_offset(delay_minutes - 5)


def exit_bar_clock(delay_minutes: int, horizon_minutes: int) -> str:
    """Bar whose close completes ``horizon_minutes`` after the entry open."""
    return clock_for_open_offset(delay_minutes + horizon_minutes - 5)


def _session_record(
    symbol: str,
    day_name: str,
    previous: pd.DataFrame,
    current: pd.DataFrame,
    delay_minutes: int,
) -> dict[str, Any] | None:
    """Build one causal delayed-entry row from indexed five-minute sessions."""
    entry_clock = clock_for_open_offset(delay_minutes)
    confirm_clock = confirmation_clock(delay_minutes)
    exit_clocks = {horizon: exit_bar_clock(delay_minutes, horizon) for horizon in HORIZONS}
    required_previous = {"09:30", "15:55"}
    required_current = {"09:30", confirm_clock, entry_clock, *exit_clocks.values()}
    if not required_previous.issubset(previous.index) or not required_current.issubset(current.index):
        return None

    previous_close = float(previous.loc["15:55"].close)
    previous_open = float(previous.loc["09:30"].open)
    open_price = float(current.loc["09:30"].open)
    entry = float(current.loc[entry_clock].open)
    path = current.loc[
        (current.index >= entry_clock) & (current.index <= exit_clocks[max(HORIZONS)])
    ].sort_values("ts")
    # A 90-minute replay requires exactly 18 complete five-minute bars.
    if len(path) < max(HORIZONS) // 5:
        return None

    record: dict[str, Any] = {
        "symbol": symbol,
        "date": day_name,
        "entry_time": int(current.loc[entry_clock].ts),
        "entry": entry,
        "gap_bps": (open_price / previous_close - 1.0) * 10_000.0,
        # Retain the legacy field name so the frozen V5 filter can be reused.
        # For delayed variants this is the cumulative opening move through the
        # completed 10/15-minute confirmation window.
        "first5_bps": (float(current.loc[confirm_clock].close) / open_price - 1.0) * 10_000.0,
        "previous_day_bps": (previous_close / previous_open - 1.0) * 10_000.0,
        "confirmation_minutes": delay_minutes,
        "path_150": [
            (float(item.high), float(item.low), float(item.close))
            for _, item in path.iterrows()
        ],
    }
    for horizon, clock in exit_clocks.items():
        record[f"exit_{horizon}"] = float(current.loc[clock].close)
    return record


def build_session_frames(
    raw: dict[str, list[list[str]]],
) -> dict[str, dict[str, pd.DataFrame]]:
    """Aggregate raw candles once and index each New York cash session."""
    result: dict[str, dict[str, pd.DataFrame]] = {}
    for symbol, source in raw.items():
        bars = aggregate_bars(source, 5)
        if not bars:
            continue
        frame = pd.DataFrame(
            bars,
            columns=["ts", "open", "high", "low", "close", "volume", "v1", "v2", "confirm"],
        )
        frame["stamp"] = pd.to_datetime(frame.ts.astype("int64"), unit="ms", utc=True)
        frame["local"] = frame.stamp.dt.tz_convert(NY)
        frame["date"] = frame.local.dt.date.astype(str)
        frame["clock"] = frame.local.dt.strftime("%H:%M")
        for column in ("open", "high", "low", "close", "volume"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        result[symbol] = {
            str(day): part.set_index("clock")
            for day, part in frame.groupby("date")
        }
    return result


def delayed_opportunities_from_sessions(
    sessions: dict[str, dict[str, pd.DataFrame]],
    delay_minutes: int,
    excluded_symbols: tuple[str, ...] = EXCLUDED_SYMBOLS,
) -> pd.DataFrame:
    """Build stock-token rows and attach the matching delayed SPY confirmation."""
    records: list[dict[str, Any]] = []
    for symbol, by_date in sessions.items():
        dates = sorted(by_date)
        for index, day_name in enumerate(dates[1:], 1):
            record = _session_record(
                symbol, day_name, by_date[dates[index - 1]], by_date[day_name], delay_minutes,
            )
            if record is not None:
                records.append(record)
    if not records:
        return pd.DataFrame()
    value = pd.DataFrame(records)
    benchmark = value[value.symbol == BENCHMARK_SYMBOL][
        ["date", "gap_bps", "first5_bps", "previous_day_bps"]
    ].rename(columns={
        "gap_bps": "spy_gap",
        "first5_bps": "spy_first5",
        "previous_day_bps": "spy_previous_day",
    })
    return value[~value.symbol.isin(excluded_symbols)].merge(benchmark, on="date", how="inner")


def delayed_opportunities(
    raw: dict[str, list[list[str]]],
    delay_minutes: int,
    excluded_symbols: tuple[str, ...] = EXCLUDED_SYMBOLS,
) -> pd.DataFrame:
    """Convenience wrapper used by focused tests and one-off research."""
    return delayed_opportunities_from_sessions(
        build_session_frames(raw), delay_minutes, excluded_symbols,
    )


def build_delayed_features(
    sessions: dict[str, dict[str, pd.DataFrame]],
    causal_open_features: pd.DataFrame,
    delay_minutes: int,
) -> pd.DataFrame:
    rows = delayed_opportunities_from_sessions(sessions, delay_minutes).merge(
        causal_open_features, on=["symbol", "date"], how="left",
    )
    if rows.empty:
        return rows
    rows = rows.dropna(subset=[
        "entry", "exit_30", "exit_60", "exit_90", "spy_gap", "spy_first5",
        "spy_previous_day", "open_volume_ratio", "prior_range_bps",
    ]).copy()
    rows = rows[rows.path_150.map(lambda item: isinstance(item, (list, tuple)) and len(item) >= 18)].copy()
    rows["relative_gap"] = rows.gap_bps - rows.spy_gap
    rows["relative_first5"] = rows.first5_bps - rows.spy_first5
    rows["relative_previous"] = rows.previous_day_bps - rows.spy_previous_day
    rows["relative_gap_rank"] = rows.groupby("date").relative_gap.transform(
        lambda values: values.abs().rank(pct=True)
    )
    rows["atr_bps"] = (rows.prior_range_bps.astype(float) / 8.0).clip(lower=20.0, upper=250.0)
    rows["direction"] = np.where(rows.relative_gap > 0, -1.0, 1.0)
    stamp = pd.to_datetime(rows.entry_time.astype("int64"), unit="ms", utc=True).dt.tz_convert(NY)
    rows["weekday"] = stamp.dt.weekday.astype(float)
    macro, earnings = load_event_sets()
    tickers = rows.symbol.astype(str).str.split("-", n=1).str[0].str.upper()
    rows["macro_day"] = rows.date.astype(str).isin(macro).astype(float)
    rows["earnings_window"] = [
        float((ticker, str(day_name)) in earnings)
        for ticker, day_name in zip(tickers, rows.date)
    ]
    rows["regime"] = rows.spy_gap.map(
        lambda value: market_regime(value, V4Config().max_spy_gap_bps)
    )
    return rows


def _fold_metrics(
    trades: list[dict[str, Any]], folds: dict[str, set[str]],
) -> dict[str, dict[str, Any]]:
    return {
        name: metrics(item for item in trades if item["date"] in target)
        for name, target in folds.items() if name != "warmup"
    }


def delay_selection_history(
    base: pd.DataFrame,
    days: list[str],
    max_positions: int = 5,
) -> list[dict[str, Any]]:
    """Create prior-day 30m paper outcomes used only to choose entry timing."""
    history: list[dict[str, Any]] = []
    for day_name in days:
        current = base[base.date == day_name]
        ranked = current.sort_values(
            "relative_gap", key=lambda values: values.abs(), ascending=False,
        ).head(max_positions)
        for _, row in ranked.iterrows():
            history.append({
                "date": str(row.date),
                "symbol": str(row.symbol),
                "side": str(row.side),
                "relative_gap_bps": round(float(row.relative_gap), 2),
                "net_pct": round(float(row.net_30), 6),
            })
    return history


def causal_delay_trades(
    results: dict[str, dict[str, Any]],
    days: list[str],
    *,
    by_side: bool,
    lookback_sessions: int = 20,
    min_global_samples: int = 12,
    min_side_samples: int = 7,
    min_expectancy_pct: float = -0.10,
    max_positions: int = 5,
) -> list[dict[str, Any]]:
    """Choose today's delay only from completed 30m paper trades before today."""
    paper = {
        name: list(value.get("selection_history", []))
        for name, value in results.items()
    }
    executable = {
        name: list(value.get("trades", []))
        for name, value in results.items()
    }
    selected: list[dict[str, Any]] = []
    for index, day_name in enumerate(days):
        if index < lookback_sessions:
            continue
        history_days = set(days[index - lookback_sessions:index])
        sides: tuple[str | None, ...] = ("LONG", "SHORT") if by_side else (None,)
        current: list[dict[str, Any]] = []
        for side in sides:
            choices: list[tuple[float, str, int]] = []
            for name, rows in paper.items():
                values = [
                    float(item["net_pct"])
                    for item in rows
                    if item["date"] in history_days and (side is None or item["side"] == side)
                ]
                required = min_side_samples if side is not None else min_global_samples
                if len(values) < required:
                    continue
                expectation = sum(values) / len(values)
                if expectation > min_expectancy_pct:
                    choices.append((expectation, name, len(values)))
            if not choices:
                continue
            expectation, chosen, samples = max(choices)
            for item in executable[chosen]:
                if item["date"] != day_name or (side is not None and item["side"] != side):
                    continue
                current.append({
                    **item,
                    "selected_delay_minutes": int(chosen),
                    "prior_delay_samples": samples,
                    "prior_delay_expectancy_pct": round(expectation, 6),
                })
        current.sort(key=lambda item: abs(float(item.get("relative_gap_bps", 0.0))), reverse=True)
        selected.extend(current[:max_positions])
    return selected


def development_eligible(parts: dict[str, dict[str, Any]]) -> bool:
    """Use only the three declared development folds for model selection."""
    return all(
        int(parts.get(name, {}).get("trades", 0)) >= 15
        and float(parts.get(name, {}).get("equal_weight_net_pct", 0.0)) > 0.0
        and float(parts.get(name, {}).get("profit_factor") or 0.0) >= 1.10
        for name in SELECTION_FOLDS
    )


def _selection_score(value: dict[str, Any]) -> tuple[float, float]:
    parts = [value["folds"][name] for name in SELECTION_FOLDS]
    worst_expectancy = min(float(part["expectancy_pct_per_trade"]) for part in parts)
    total_trades = sum(int(part["trades"]) for part in parts)
    combined_expectancy = (
        sum(float(part["equal_weight_net_pct"]) for part in parts) / total_trades
        if total_trades else float("-inf")
    )
    return worst_expectancy, combined_expectancy


def assess_delay(rows: pd.DataFrame, delay_minutes: int) -> dict[str, Any]:
    days = sorted(str(value) for value in rows.date.unique())
    trade_config = replace(V4Config(), skip_macro_days=False, skip_earnings_days=False)
    adaptive = AdaptiveHorizonConfig()
    base = add_horizon_outcomes(label_base_candidates(rows, trade_config), trade_config)
    trades = causal_adaptive_horizon_trades(base, days, adaptive)
    folds = _fold_metrics(trades, chronological_folds(days))
    recent_month = set(days[-20:])
    recent_week = set(days[-5:])
    robustness = robustness_report(
        trades, days, adaptive, trade_config.round_trip_cost_bps, bootstrap_samples=2_000,
    )
    return {
        "delay_minutes": delay_minutes,
        "entry_clock": clock_for_open_offset(delay_minutes),
        "confirmation_clock": confirmation_clock(delay_minutes),
        "effective_sessions": {"count": len(days), "start": days[0], "end": days[-1]},
        "feature_rows": len(rows),
        "base_candidates": len(base),
        "effective_dates": days,
        "selection_history": delay_selection_history(base, days, adaptive.max_positions),
        "all": metrics(trades),
        "folds": folds,
        "recent_month": metrics(item for item in trades if item["date"] in recent_month),
        "recent_week": metrics(item for item in trades if item["date"] in recent_week),
        "risk_weighted_portfolio": risk_weighted_portfolio_metrics(trades, adaptive),
        "robustness": robustness,
        "development_eligible": development_eligible(folds),
        "trades": trades,
    }


def research(raw: dict[str, list[list[str]]]) -> dict[str, Any]:
    sessions = build_session_frames(raw)
    causal_features = opening_features(raw)
    results = {
        str(delay): assess_delay(
            build_delayed_features(sessions, causal_features, delay), delay,
        )
        for delay in DELAYS
    }
    eligible = [name for name, value in results.items() if value["development_eligible"]]
    selected = max(eligible, key=lambda name: _selection_score(results[name]), default=None)

    days = list(results["5"]["effective_dates"])
    adaptive_results: dict[str, Any] = {}
    for name, by_side in (("global_prior20", False), ("side_prior20", True)):
        trades = causal_delay_trades(results, days, by_side=by_side)
        folds = _fold_metrics(trades, chronological_folds(days))
        recent_month = set(days[-20:])
        recent_week = set(days[-5:])
        adaptive_results[name] = {
            "rule": (
                "choose one delay globally from prior20 30m paper expectancy"
                if not by_side else
                "choose LONG/SHORT delays independently from prior20 same-side 30m paper expectancy"
            ),
            "all": metrics(trades),
            "folds": folds,
            "recent_month": metrics(item for item in trades if item["date"] in recent_month),
            "recent_week": metrics(item for item in trades if item["date"] in recent_week),
            "risk_weighted_portfolio": risk_weighted_portfolio_metrics(
                trades, AdaptiveHorizonConfig(),
            ),
            "delay_usage": {
                str(delay): sum(int(item["selected_delay_minutes"]) == delay for item in trades)
                for delay in DELAYS
            },
            "development_eligible": development_eligible(folds),
            "trades": trades,
        }

    baseline = results["5"]
    challenger = None
    for name in ("10", "15"):
        value = results[name]
        improves_each_fold = all(
            float(value["folds"][fold]["expectancy_pct_per_trade"])
            > float(baseline["folds"][fold]["expectancy_pct_per_trade"])
            for fold in SELECTION_FOLDS
        )
        improves_portfolio = (
            float(value["risk_weighted_portfolio"]["return_pct"])
            > float(baseline["risk_weighted_portfolio"]["return_pct"])
        )
        if value["development_eligible"] and improves_each_fold and improves_portfolio:
            if challenger is None or _selection_score(value) > _selection_score(results[challenger]):
                challenger = name

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "experiment": "real_fill_after_5_10_15_minute_opening_confirmation",
        "trade_config": asdict(replace(V4Config(), skip_macro_days=False, skip_earnings_days=False)),
        "adaptive_config": asdict(AdaptiveHorizonConfig()),
        "selection_folds": list(SELECTION_FOLDS),
        "results": results,
        "causal_adaptive_delay": adaptive_results,
        "selected_by_development_folds": selected,
        "strict_challenger_delay": int(challenger) if challenger is not None else None,
        "forward_shadow_recommendation": (
            f"freeze_delay_{challenger}_as_separate_shadow_challenger"
            if challenger is not None else "retain_frozen_v5_delay_5"
        ),
        "promotion_passed": False,
        "warning": (
            "Retrospective timing research. Diagnostic/latest sessions and all three delays "
            "have now been inspected; any winner still requires a new forward-shadow cohort."
        ),
    }


def run(end: date | None = None, sessions_count: int = 100) -> dict[str, Any]:
    sessions = weekday_sessions(end or latest_completed_us_session(), sessions_count)
    symbols = load_symbols("historical_90d")
    raw = load_market_data(OKX(settings()), symbols, sessions)
    report = research(raw)
    report["requested_sessions"] = sessions_count
    report["universe_size"] = len(symbols)
    return report


def main() -> None:
    report = run()
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    compact = {
        "selected_by_development_folds": report["selected_by_development_folds"],
        "strict_challenger_delay": report["strict_challenger_delay"],
        "forward_shadow_recommendation": report["forward_shadow_recommendation"],
        "results": {
            name: {
                "entry_clock": value["entry_clock"],
                "base_candidates": value["base_candidates"],
                "all": value["all"],
                "folds": value["folds"],
                "recent_month": value["recent_month"],
                "risk_weighted_return_pct": value["risk_weighted_portfolio"]["return_pct"],
                "risk_weighted_max_drawdown_pct": value["risk_weighted_portfolio"]["max_drawdown_pct"],
                "development_eligible": value["development_eligible"],
            }
            for name, value in report["results"].items()
        },
        "causal_adaptive_delay": {
            name: {
                "all": value["all"],
                "folds": value["folds"],
                "recent_month": value["recent_month"],
                "risk_weighted_return_pct": value["risk_weighted_portfolio"]["return_pct"],
                "risk_weighted_max_drawdown_pct": value["risk_weighted_portfolio"]["max_drawdown_pct"],
                "delay_usage": value["delay_usage"],
                "development_eligible": value["development_eligible"],
            }
            for name, value in report["causal_adaptive_delay"].items()
        },
        "promotion_passed": report["promotion_passed"],
        "warning": report["warning"],
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
