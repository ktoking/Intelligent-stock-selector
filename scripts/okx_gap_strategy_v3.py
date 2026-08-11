#!/usr/bin/env python3
"""Causal V3 research strategy for OKX opening-gap trading.

V3 keeps the interpretable V2 filters and adds three controls that are useful
in a real portfolio: a walk-forward expected-return model, volatility-aware
stops/size, and a promotion gate that checks out-of-sample segments.  The
model is a ranker, not an order authority; when its expected net return is not
positive the strategy abstains.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_gap_strategy_v2 import build_features as build_v2_features  # noqa: E402
from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import load_market_data, weekday_sessions  # noqa: E402
from scripts.okx_research_universe import load_symbols  # noqa: E402

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
OUTPUT = ROOT / "data" / "okx_gap_strategy_v3_backtest.json"
EVENT_PATH = ROOT / "data" / "okx_event_calendar.json"

MODEL_FEATURES = (
    "relative_gap", "relative_first5", "relative_previous", "relative_gap_rank",
    "abs_relative_gap", "abs_spy_gap", "spy_first5", "spy_previous_day",
    "open_volume_ratio", "prior_range_bps", "atr_bps", "direction", "weekday",
)


@dataclass(frozen=True)
class V3Config:
    min_relative_gap_bps: float = 100.0
    max_relative_gap_bps: float = 600.0
    max_spy_gap_bps: float = 75.0
    min_cross_sectional_rank: float = 0.75
    min_expected_net_pct: float = 0.05
    horizon_minutes: int = 90
    atr_stop_multiple: float = 1.2
    min_stop_bps: float = 75.0
    max_stop_bps: float = 300.0
    round_trip_cost_bps: float = 14.0
    max_positions: int = 5
    risk_fraction: float = 0.0035
    max_position_equity_fraction: float = 0.20
    max_gross_equity_fraction: float = 0.80
    model_lookback_sessions: int = 40
    model_min_rows: int = 80
    skip_macro_days: bool = True
    skip_earnings_days: bool = True
    promotion_min_trades: int = 25
    promotion_min_pf: float = 1.20
    promotion_max_drawdown_pct_points: float = 30.0


def dynamic_stop_bps(atr_bps: float, config: V3Config) -> float:
    """Return an ATR stop distance bounded for thin/high-volatility contracts."""
    value = float(atr_bps or 0.0) * config.atr_stop_multiple
    return max(config.min_stop_bps, min(config.max_stop_bps, value))


def risk_budget_notional(
    equity: float,
    risk_fraction: float,
    entry: float,
    stop_bps: float,
    gross_room: float,
    max_position_equity_fraction: float = 1.0,
) -> float:
    """Convert a risk budget and stop distance into quote notional."""
    if equity <= 0 or entry <= 0 or stop_bps <= 0 or gross_room <= 0:
        return 0.0
    risk_budget = equity * risk_fraction
    loss_fraction = stop_bps / 10_000.0
    return max(0.0, min(
        risk_budget / loss_fraction,
        equity * max_position_equity_fraction,
        gross_room,
    ))


def market_regime(spy_gap_bps: float, max_spy_gap_bps: float) -> str:
    return "quiet" if abs(float(spy_gap_bps)) <= max_spy_gap_bps else "event_risk"


def load_event_sets(path: Path = EVENT_PATH) -> tuple[set[str], set[tuple[str, str]]]:
    """Load macro dates and +/-1 calendar-day reported-earnings windows."""
    if not path.exists():
        return set(), set()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return set(), set()
    macro: set[str] = set()
    earnings: set[tuple[str, str]] = set()
    for event in data.get("macro_events", []):
        try:
            macro.add(datetime.fromisoformat(str(event["scheduled_at"])).astimezone(NY).date().isoformat())
        except (KeyError, TypeError, ValueError):
            continue
    for event in data.get("earnings", []):
        if event.get("event_type") != "EARNINGS_REPORTED":
            continue
        symbol = str(event.get("symbol", "")).strip().upper()
        stamp = str(event.get("scheduled_at", ""))[:10]
        if not symbol or len(stamp) != 10:
            continue
        try:
            event_day = date.fromisoformat(stamp)
        except ValueError:
            continue
        for offset in (-1, 0, 1):
            earnings.add((symbol, (event_day + timedelta(days=offset)).isoformat()))
    return macro, earnings


def _base_candidates(rows: pd.DataFrame, config: V3Config) -> pd.DataFrame:
    selected = rows[
        rows.relative_gap.abs().between(
            config.min_relative_gap_bps, config.max_relative_gap_bps, inclusive="both"
        )
        & (rows.spy_gap.abs() <= config.max_spy_gap_bps)
        & (rows.relative_gap_rank >= config.min_cross_sectional_rank)
        & rows.exit_90.notna()
    ].copy()
    confirmed = (
        (selected.relative_gap * selected.relative_first5 < 0)
        | (selected.relative_gap * selected.relative_previous <= 0)
    )
    selected = selected[confirmed]
    if config.skip_macro_days and "macro_day" in selected:
        selected = selected[selected.macro_day <= 0]
    if config.skip_earnings_days and "earnings_window" in selected:
        selected = selected[selected.earnings_window <= 0]
    return selected


def select_v3_candidates(rows: pd.DataFrame, config: V3Config) -> pd.DataFrame:
    """Apply causal filters plus the model's expected-return threshold."""
    selected = _base_candidates(rows, config)
    if "expected_net_pct" not in selected:
        return selected.iloc[0:0].copy()
    return selected[selected.expected_net_pct >= config.min_expected_net_pct].copy()


def _feature_frame(rows: pd.DataFrame) -> pd.DataFrame:
    values = rows.copy()
    for column in MODEL_FEATURES:
        if column not in values:
            values[column] = 0.0
    return values.loc[:, MODEL_FEATURES].replace([np.inf, -np.inf], np.nan).fillna(0.0).astype(float)


def _label_net_pct(row: pd.Series, config: V3Config) -> float:
    direction = -1 if float(row.relative_gap) > 0 else 1
    stop_pct = dynamic_stop_bps(float(row.atr_bps), config) / 100.0
    for high, low, _close in row.path_150[:18]:
        adverse = (float(low) / float(row.entry) - 1) * 100 if direction > 0 else (1 - float(high) / float(row.entry)) * 100
        if adverse <= -stop_pct:
            return -stop_pct - config.round_trip_cost_bps / 100.0
    return direction * (float(row.exit_90) / float(row.entry) - 1) * 100 - config.round_trip_cost_bps / 100.0


def build_features(raw: dict[str, list[list[str]]]) -> pd.DataFrame:
    rows = build_v2_features(raw)
    # The public opening table has no stable L2 ATR at 09:35.  Prior-day
    # range is therefore used as a causal volatility proxy; live code should
    # replace this field with the current 5m ATR before sizing an order.
    rows["atr_bps"] = (rows["prior_range_bps"].astype(float) / 8.0).clip(lower=20.0, upper=250.0)
    rows["direction"] = np.where(rows.relative_gap > 0, -1.0, 1.0)
    stamp = pd.to_datetime(rows.entry_time.astype("int64"), unit="ms", utc=True).dt.tz_convert(NY)
    rows["weekday"] = stamp.dt.weekday.astype(float)
    macro_dates, earnings_windows = load_event_sets()
    tickers = rows.symbol.astype(str).str.split("-", n=1).str[0].str.upper()
    rows["macro_day"] = rows.date.astype(str).isin(macro_dates).astype(float)
    rows["earnings_window"] = [
        float((ticker, str(day)) in earnings_windows)
        for ticker, day in zip(tickers, rows.date)
    ]
    rows["regime"] = rows.spy_gap.map(lambda value: market_regime(value, V3Config().max_spy_gap_bps))
    return rows


def walkforward_expected_returns(rows: pd.DataFrame, days: list[str], config: V3Config) -> pd.DataFrame:
    """Fit a return model only on sessions before the scored session."""
    scored: list[pd.DataFrame] = []
    base = _base_candidates(rows, config).copy()
    base["label_net_pct"] = base.apply(lambda row: _label_net_pct(row, config), axis=1)
    for index, day in enumerate(days):
        if index < config.model_lookback_sessions:
            continue
        history_days = set(days[index - config.model_lookback_sessions:index])
        train = base[base.date.isin(history_days)]
        current = base[base.date == day]
        if len(train) < config.model_min_rows or current.empty or train.label_net_pct.nunique() < 2:
            continue
        model = HistGradientBoostingRegressor(
            learning_rate=0.045, max_iter=120, max_leaf_nodes=5,
            min_samples_leaf=15, l2_regularization=8, random_state=42,
        )
        model.fit(_feature_frame(train), train.label_net_pct.astype(float))
        value = current.copy()
        value["expected_net_pct"] = model.predict(_feature_frame(current))
        scored.append(value)
    if not scored:
        return pd.DataFrame(columns=[*rows.columns, "expected_net_pct"])
    return pd.concat(scored, ignore_index=True)


def replay(rows: pd.DataFrame, config: V3Config) -> list[dict[str, Any]]:
    scored = rows if "expected_net_pct" in rows else rows.iloc[0:0].copy()
    selected = select_v3_candidates(scored, config)
    trades: list[dict[str, Any]] = []
    for _entry_time, group in selected.groupby("entry_time", sort=True):
        for _, row in group.nlargest(config.max_positions, "expected_net_pct").iterrows():
            direction = -1 if float(row.relative_gap) > 0 else 1
            stop_bps = dynamic_stop_bps(float(row.atr_bps), config)
            stop_pct = stop_bps / 100.0
            net_pct = None
            reason = "horizon"
            for high, low, _close in row.path_150[:18]:
                adverse = (float(low) / float(row.entry) - 1) * 100 if direction > 0 else (1 - float(high) / float(row.entry)) * 100
                if adverse <= -stop_pct:
                    net_pct = -stop_pct - config.round_trip_cost_bps / 100.0
                    reason = "atr_stop"
                    break
            if net_pct is None:
                net_pct = direction * (float(row.exit_90) / float(row.entry) - 1) * 100 - config.round_trip_cost_bps / 100.0
            trades.append({
                "date": str(row.date), "symbol": str(row.symbol),
                "entry_time": int(row.entry_time), "exit_time": int(row.entry_time + config.horizon_minutes * 60_000),
                "side": "LONG" if direction > 0 else "SHORT", "expected_net_pct": round(float(row.expected_net_pct), 6),
                "stop_bps": round(stop_bps, 3), "net_pct": round(float(net_pct), 6), "exit_reason": reason,
            })
    return trades


def metrics(trades: Iterable[dict[str, Any]]) -> dict[str, Any]:
    values = [float(item["net_pct"]) for item in trades]
    gross_profit = sum(value for value in values if value > 0)
    gross_loss = -sum(value for value in values if value <= 0)
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value; peak = max(peak, equity); drawdown = max(drawdown, peak - equity)
    return {
        "trades": len(values), "wins": sum(value > 0 for value in values),
        "win_rate_pct": round(sum(value > 0 for value in values) / len(values) * 100, 2) if values else 0.0,
        "equal_weight_net_pct": round(sum(values), 3),
        "expectancy_pct_per_trade": round(sum(values) / len(values), 4) if values else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
        "max_drawdown_pct_points": round(drawdown, 3),
    }


def promotion_gate(parts: dict[str, dict[str, Any]], config: V3Config) -> bool:
    for split in ("validation", "development"):
        value = parts.get(split) or {}
        if value.get("trades", 0) < config.promotion_min_trades:
            return False
        if value.get("equal_weight_net_pct", 0) <= 0:
            return False
        if (value.get("profit_factor") or 0) < config.promotion_min_pf:
            return False
        if value.get("max_drawdown_pct_points", float("inf")) > config.promotion_max_drawdown_pct_points:
            return False
    final = parts.get("final_diagnostic") or {}
    return bool(
        final.get("trades", 0) >= config.promotion_min_trades * 2
        and final.get("equal_weight_net_pct", 0) > 0
        and (final.get("profit_factor") or 0) >= 1.0
        and final.get("max_drawdown_pct_points", float("inf")) <= config.promotion_max_drawdown_pct_points * 2
    )


def risk_size_for_signal(equity: float, gross_notional: float, entry: float, atr_bps: float, config: V3Config) -> dict[str, float]:
    stop_bps = dynamic_stop_bps(atr_bps, config)
    gross_room = max(0.0, equity * config.max_gross_equity_fraction - gross_notional)
    notional = risk_budget_notional(
        equity, config.risk_fraction, entry, stop_bps, gross_room, config.max_position_equity_fraction,
    )
    return {"stop_bps": stop_bps, "notional": notional, "risk_budget": equity * config.risk_fraction}


def run(end: date | None = None, sessions_count: int = 90) -> dict[str, Any]:
    end = end or (datetime.now(NY).date() - timedelta(days=1))
    sessions = weekday_sessions(end, sessions_count)
    days = [item.isoformat() for item in sessions]
    symbols = load_symbols("historical_90d")
    raw = load_market_data(OKX(settings()), symbols, sessions)
    rows = build_features(raw)
    config = V3Config()
    scored = walkforward_expected_returns(rows, days, config)
    trades = replay(scored, config)
    split_days = {
        "validation": set(days[max(0, len(days) - 50):max(0, len(days) - 40)]),
        "development": set(days[max(0, len(days) - 40):max(0, len(days) - 30)]),
        "final_diagnostic": set(days[max(0, len(days) - 30):]),
    }
    parts = {name: metrics([item for item in trades if item["date"] in target]) for name, target in split_days.items()}
    return {
        "generated_at": datetime.now(UTC).isoformat(), "requested_sessions": sessions_count,
        "effective_dates": sorted(rows.date.unique()), "universe_size": len(symbols),
        "strategy": asdict(config), "model": "HistGradientBoostingRegressor; causal rolling 40-session expected-net-return ranker",
        "model_rows": len(scored), "trades": len(trades), "metrics": parts,
        "promotion_passed": promotion_gate(parts, config),
        "warning": "OHLCV proxy; live activation still requires current 5m ATR, L2 fill audit, and forward shadow sessions.",
    }


def main() -> None:
    report = run(sessions_count=90)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
