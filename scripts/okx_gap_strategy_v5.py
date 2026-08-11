#!/usr/bin/env python3
"""Adaptive-horizon V5 research for the relative opening-gap fade.

The exit horizon is selected independently for LONG and SHORT signals from
completed trades in the preceding 20 sessions.  This lets the system shorten
exposure when the fade decays quickly and retain a position for 60/90 minutes
only when that side's recent history supports it.  Current/future outcomes are
never used for the decision being replayed.

V5 was designed after inspecting the V4 folds, so every date in this report is
retrospective research.  It must collect a new forward-shadow cohort before it
can replace the Demo executor.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_gap_strategy_v3 import build_features, dynamic_stop_bps, metrics  # noqa: E402
from scripts.okx_gap_strategy_v4 import (  # noqa: E402
    V4Config,
    chronological_folds,
    label_base_candidates,
    latest_completed_us_session,
    trade_net_pct,
)
from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import load_market_data, weekday_sessions  # noqa: E402
from scripts.okx_research_universe import load_symbols  # noqa: E402

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
OUTPUT = ROOT / "data" / "okx_gap_strategy_v5_backtest.json"
HORIZONS = (30, 60, 90)


@dataclass(frozen=True)
class AdaptiveHorizonConfig:
    lookback_sessions: int = 20
    min_side_samples: int = 7
    min_best_expectancy_pct: float = -0.10
    horizons_minutes: tuple[int, ...] = HORIZONS
    max_positions: int = 5
    risk_fraction: float = 0.0035
    max_position_equity_fraction: float = 0.20
    max_gross_equity_fraction: float = 0.80


def add_horizon_outcomes(base: pd.DataFrame, trade_config: V4Config) -> pd.DataFrame:
    value = base.copy()
    # A newly listed contract can have a 90m exit but an incomplete intraday
    # path after an alternative benchmark changes candidate membership.  Such
    # rows cannot support stop-aware replay and must not become trades.
    if "path_150" in value:
        value = value[value["path_150"].map(lambda item: isinstance(item, (list, tuple)))].copy()
    required_exits = [f"exit_{horizon}" for horizon in HORIZONS]
    for column in required_exits:
        if column in value:
            value = value[value[column].notna()].copy()
    value["stop_bps"] = value.atr_bps.map(lambda item: dynamic_stop_bps(float(item), trade_config))
    for horizon in HORIZONS:
        config = replace(trade_config, horizon_minutes=horizon)
        outcomes = value.apply(lambda row: trade_net_pct(row, config), axis=1)
        value[f"net_{horizon}"] = [item[0] for item in outcomes]
        value[f"reason_{horizon}"] = [item[1] for item in outcomes]
    return value


def resolved_trade_exit(
    row: pd.Series,
    horizon_minutes: int,
    exit_reason: str,
) -> dict[str, int | str | None]:
    """Resolve the first stop bar end without pretending to know intrabar fill time.

    ``path_150`` contains completed five-minute OHLC bars but no one-minute
    ordering.  A stop therefore resolves to the end of the first five-minute
    bar whose adverse extreme crossed it.  This is more accurate than the old
    scheduled-horizon timestamp, while remaining explicit that the exact
    intrabar execution time is unavailable.
    """
    entry_time = int(row.entry_time)
    scheduled = entry_time + int(horizon_minutes) * 60_000
    if exit_reason != "atr_stop":
        return {
            "exit_time": scheduled,
            "exit_time_basis": "scheduled_horizon",
            "stop_bar_number": None,
        }

    direction = -1 if float(row.relative_gap) > 0 else 1
    stop_pct = float(row.get("stop_bps") or 0.0) / 100.0
    path = row.get("path_150")
    if stop_pct <= 0.0 or not isinstance(path, (list, tuple)):
        raise ValueError("atr_stop trade is missing a usable stop distance or path_150")
    for index, (high, low, _close) in enumerate(path[:max(1, horizon_minutes // 5)]):
        adverse = (
            (float(low) / float(row.entry) - 1) * 100
            if direction > 0
            else (1 - float(high) / float(row.entry)) * 100
        )
        if adverse <= -stop_pct:
            return {
                "exit_time": entry_time + (index + 1) * 5 * 60_000,
                "exit_time_basis": "first_triggering_5m_bar_end",
                "stop_bar_number": index + 1,
            }
    raise ValueError("atr_stop outcome has no matching stop-crossing bar")


def side_horizon_choices(
    history: pd.DataFrame,
    config: AdaptiveHorizonConfig,
) -> dict[str, dict[str, float | int]]:
    """Summarize the frozen decision available for the next/current session."""
    choices: dict[str, dict[str, float | int]] = {}
    for side in ("LONG", "SHORT"):
        prior = history[history.side == side]
        if len(prior) < config.min_side_samples:
            continue
        edges = {
            horizon: float(prior[f"net_{horizon}"].mean())
            for horizon in config.horizons_minutes
        }
        chosen = max(edges, key=edges.get)
        if edges[chosen] <= config.min_best_expectancy_pct:
            continue
        choices[side] = {
            "horizon_minutes": chosen,
            "prior_side_samples": len(prior),
            "prior_best_horizon_expectancy_pct": round(edges[chosen], 6),
            **{f"edge_{horizon}_pct": round(edges[horizon], 6) for horizon in config.horizons_minutes},
        }
    return choices


def next_session_symbol_edge_scores(
    base: pd.DataFrame,
    days: list[str],
    config: AdaptiveHorizonConfig,
    *,
    symbol_lookback_sessions: int = 40,
    prior_strength: float = 5.0,
) -> dict[str, dict[str, dict[str, float | int | None]]]:
    """Emit prior-only symbol scores for a separate same-gross shadow sizing study."""
    if not days:
        return {}
    side_days = set(days[-config.lookback_sessions:])
    symbol_days = set(days[-symbol_lookback_sessions:])
    choices = side_horizon_choices(base[base.date.isin(side_days)], config)
    symbols = sorted(str(value) for value in base.symbol.unique())
    result: dict[str, dict[str, dict[str, float | int | None]]] = {}
    for side, choice in choices.items():
        horizon = int(choice["horizon_minutes"])
        global_edge = float(choice[f"edge_{horizon}_pct"])
        result[side] = {}
        for symbol in symbols:
            prior = base[(base.symbol == symbol) & base.date.isin(symbol_days)]
            values = prior[f"net_{horizon}"].astype(float).tolist()
            shrunk = (sum(values) + prior_strength * global_edge) / (len(values) + prior_strength)
            result[side][symbol] = {
                "horizon_minutes": horizon,
                "prior_symbol_samples": len(values),
                "prior_symbol_expectancy_pct": (
                    round(sum(values) / len(values), 6) if values else None
                ),
                "shrunk_symbol_edge_pct": round(shrunk, 6),
                "allocation_multiplier": round(max(0.5, min(1.5, 1.0 + shrunk)), 6),
            }
    return result


def causal_adaptive_horizon_trades(
    base: pd.DataFrame,
    days: list[str],
    config: AdaptiveHorizonConfig,
) -> list[dict[str, Any]]:
    """Choose the horizon from prior same-side trades, then rank today's gaps."""
    trades: list[dict[str, Any]] = []
    for index, day in enumerate(days):
        if index < config.lookback_sessions:
            continue
        history_days = set(days[index - config.lookback_sessions:index])
        history = base[base.date.isin(history_days)]
        current = base[base.date == day]
        choices = side_horizon_choices(history, config)
        eligible = [group for side, group in current.groupby("side") if str(side) in choices]
        if not eligible:
            continue
        ranked = pd.concat(eligible).sort_values(
            "relative_gap", key=lambda values: values.abs(), ascending=False,
        ).head(config.max_positions)
        for _, row in ranked.iterrows():
            choice = choices[str(row.side)]
            horizon = int(choice["horizon_minutes"])
            exit_reason = str(row[f"reason_{horizon}"])
            exit_metadata = resolved_trade_exit(row, horizon, exit_reason)
            trades.append({
                "date": str(row.date), "symbol": str(row.symbol),
                "entry_time": int(row.entry_time),
                **exit_metadata,
                "side": str(row.side), "relative_gap_bps": round(float(row.relative_gap), 2),
                "horizon_minutes": horizon, "prior_side_samples": int(choice["prior_side_samples"]),
                "prior_best_horizon_expectancy_pct": float(choice["prior_best_horizon_expectancy_pct"]),
                "stop_bps": round(float(row.get("stop_bps") or 0.0), 3),
                "net_pct": round(float(row[f"net_{horizon}"]), 6),
                "exit_reason": exit_reason,
            })
    return trades


def portfolio_metrics(
    trades: list[dict[str, Any]],
    config: AdaptiveHorizonConfig,
    initial_equity: float = 10_000.0,
) -> dict[str, Any]:
    """Compound a conservative unlevered portfolio with an 80% gross cap."""
    equity = peak = float(initial_equity)
    max_drawdown = 0.0
    daily: list[dict[str, Any]] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        grouped.setdefault(str(trade["date"]), []).append(trade)
    for day in sorted(grouped):
        values = grouped[day]
        allocation = min(
            config.max_position_equity_fraction,
            config.max_gross_equity_fraction / len(values),
        )
        start = equity
        day_return = sum(allocation * float(item["net_pct"]) / 100.0 for item in values)
        equity *= 1.0 + day_return
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak * 100.0)
        daily.append({
            "date": day, "trades": len(values),
            "allocation_pct_each": round(allocation * 100.0, 2),
            "pnl": round(equity - start, 2), "equity": round(equity, 2),
        })
    return {
        "initial_equity": round(initial_equity, 2), "final_equity": round(equity, 2),
        "net_pnl": round(equity - initial_equity, 2),
        "return_pct": round((equity / initial_equity - 1) * 100.0, 3),
        "max_drawdown_pct": round(max_drawdown, 3), "daily": daily,
    }


def risk_weighted_portfolio_metrics(
    trades: list[dict[str, Any]],
    config: AdaptiveHorizonConfig,
    initial_equity: float = 10_000.0,
) -> dict[str, Any]:
    """Compound the same stop-distance risk budget intended for execution."""
    equity = peak = float(initial_equity)
    max_drawdown = 0.0
    daily: list[dict[str, Any]] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        grouped.setdefault(str(trade["date"]), []).append(trade)
    for day in sorted(grouped):
        values = grouped[day]
        desired = [
            min(
                config.max_position_equity_fraction,
                config.risk_fraction / max(float(item.get("stop_bps") or 0) / 10_000.0, 1e-9),
            )
            for item in values
        ]
        scale = min(1.0, config.max_gross_equity_fraction / sum(desired)) if sum(desired) else 0.0
        weights = [value * scale for value in desired]
        start = equity
        day_return = sum(weight * float(item["net_pct"]) / 100.0 for weight, item in zip(weights, values))
        equity *= 1.0 + day_return
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak * 100.0)
        daily.append({
            "date": day, "trades": len(values),
            "gross_allocation_pct": round(sum(weights) * 100.0, 2),
            "allocation_pct_each": [round(weight * 100.0, 2) for weight in weights],
            "pnl": round(equity - start, 2), "equity": round(equity, 2),
        })
    return {
        "initial_equity": round(initial_equity, 2), "final_equity": round(equity, 2),
        "net_pnl": round(equity - initial_equity, 2),
        "return_pct": round((equity / initial_equity - 1) * 100.0, 3),
        "max_drawdown_pct": round(max_drawdown, 3), "daily": daily,
    }


def robustness_report(
    trades: list[dict[str, Any]],
    days: list[str],
    config: AdaptiveHorizonConfig,
    base_round_trip_cost_bps: float,
    bootstrap_samples: int = 5_000,
) -> dict[str, Any]:
    """Stress costs and resample whole sessions to preserve trade clustering."""
    cost_sensitivity: dict[str, Any] = {}
    for cost_bps in (14, 25, 40, 50):
        incremental_pct = (cost_bps - base_round_trip_cost_bps) / 100.0
        adjusted = [{**item, "net_pct": float(item["net_pct"]) - incremental_pct} for item in trades]
        portfolio = portfolio_metrics(adjusted, config)
        cost_sensitivity[str(cost_bps)] = {
            "trade_metrics": metrics(adjusted),
            "portfolio_return_pct": portfolio["return_pct"],
            "portfolio_max_drawdown_pct": portfolio["max_drawdown_pct"],
        }

    side = {
        name: metrics(item for item in trades if item["side"] == name)
        for name in ("LONG", "SHORT")
    }
    horizon = {
        str(value): metrics(item for item in trades if int(item["horizon_minutes"]) == value)
        for value in config.horizons_minutes
    }

    by_day: dict[str, list[dict[str, Any]]] = {}
    for item in trades:
        by_day.setdefault(str(item["date"]), []).append(item)
    daily = [
        (sum(float(item["net_pct"]) for item in by_day.get(day, [])), len(by_day.get(day, [])))
        for day in days[config.lookback_sessions:]
    ]
    rng = np.random.default_rng(42)
    expectation: list[float] = []
    if daily and bootstrap_samples > 0:
        for _ in range(bootstrap_samples):
            indexes = rng.integers(0, len(daily), len(daily))
            total = sum(daily[index][0] for index in indexes)
            count = sum(daily[index][1] for index in indexes)
            expectation.append(total / count if count else 0.0)
    interval = np.percentile(expectation, [2.5, 50.0, 97.5]).tolist() if expectation else [0.0] * 3
    return {
        "cost_sensitivity": cost_sensitivity,
        "by_side": side,
        "by_horizon": horizon,
        "session_cluster_bootstrap": {
            "samples": bootstrap_samples,
            "expectancy_pct_per_trade_p2_5": round(float(interval[0]), 4),
            "expectancy_pct_per_trade_median": round(float(interval[1]), 4),
            "expectancy_pct_per_trade_p97_5": round(float(interval[2]), 4),
            "probability_expectancy_le_zero": round(float(np.mean(np.asarray(expectation) <= 0.0)), 4) if expectation else None,
        },
    }


def research(rows: pd.DataFrame) -> dict[str, Any]:
    # Event labels are retained for attribution.  The audit showed that a
    # blanket macro-day exclusion destroys the edge; events are not used as a
    # directional signal and still face the same causal entry rules.
    trade_config = replace(V4Config(), skip_macro_days=False, skip_earnings_days=False)
    adaptive_config = AdaptiveHorizonConfig()
    days = sorted(str(value) for value in rows.date.unique())
    base = add_horizon_outcomes(label_base_candidates(rows, trade_config), trade_config)
    trades = causal_adaptive_horizon_trades(base, days, adaptive_config)
    folds = chronological_folds(days)
    parts = {
        name: metrics(item for item in trades if item["date"] in target)
        for name, target in folds.items() if name != "warmup"
    }
    horizon_use = {
        str(horizon): sum(int(item["horizon_minutes"]) == horizon for item in trades)
        for horizon in adaptive_config.horizons_minutes
    }
    portfolio = portfolio_metrics(trades, adaptive_config)
    risk_portfolio = risk_weighted_portfolio_metrics(trades, adaptive_config)
    robustness = robustness_report(
        trades, days, adaptive_config, trade_config.round_trip_cost_bps,
    )
    recent_days = set(days[-adaptive_config.lookback_sessions:])
    next_session_choices = side_horizon_choices(base[base.date.isin(recent_days)], adaptive_config)
    next_symbol_scores = next_session_symbol_edge_scores(base, days, adaptive_config)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "effective_sessions": {"count": len(days), "start": days[0], "end": days[-1]},
        "trade_filter": asdict(trade_config),
        "event_labels": {
            "macro_rows": int(rows.macro_day.sum()),
            "earnings_window_rows": int(rows.earnings_window.sum()),
            "source": "data/okx_event_calendar.json",
        },
        "adaptive_horizon": asdict(adaptive_config),
        "rule": (
            "At 09:35 fade a confirmed 100-600bp stock-token gap when SPY gap <=75bp; "
            "for each side choose 30/60/90m by its mean net result over the prior 20 sessions."
        ),
        "metrics": {"all": metrics(trades), "folds": parts, "portfolio": portfolio,
                    "risk_weighted_portfolio": risk_portfolio},
        "robustness": robustness,
        "horizon_usage": horizon_use,
        "next_session_side_choices": next_session_choices,
        "next_session_symbol_edge_scores": next_symbol_scores,
        "symbol_edge_shadow_config": {
            "lookback_sessions": 40,
            "prior_strength_equivalent_trades": 5,
            "allocation_multiplier": "clip(1 + shrunk_symbol_edge_pct, 0.5, 1.5)",
            "same_daily_gross": True,
            "shadow_only": True,
        },
        "trades": trades,
        "promotion_passed": False,
        "promotion_blockers": [
            "v5_was_designed_after_v4_holdouts_were_inspected",
            "requires_at_least_25_new_forward_shadow_trades",
            "requires_historical_l2_spread_and_fill_validation",
        ],
        "warning": (
            "All dates are retrospective because V5 was designed after V4 analysis. "
            "Portfolio return assumes at most 20% equity per position and 80% gross, no leverage."
        ),
    }


def run(end: date | None = None, sessions_count: int = 100) -> dict[str, Any]:
    end = end or latest_completed_us_session()
    sessions = weekday_sessions(end, sessions_count)
    symbols = load_symbols("historical_90d")
    raw = load_market_data(OKX(settings()), symbols, sessions)
    rows = build_features(raw)
    report = research(rows)
    report["requested_sessions"] = sessions_count
    report["universe_size"] = len(symbols)
    return report


def main() -> None:
    report = run()
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    compact = {
        "effective_sessions": report["effective_sessions"], "universe_size": report["universe_size"],
        "rule": report["rule"], "metrics": {"all": report["metrics"]["all"],
        "folds": report["metrics"]["folds"], "portfolio": {
            key: report["metrics"]["portfolio"][key]
            for key in ("initial_equity", "final_equity", "net_pnl", "return_pct", "max_drawdown_pct")
        }, "risk_weighted_portfolio": {
            key: report["metrics"]["risk_weighted_portfolio"][key]
            for key in ("initial_equity", "final_equity", "net_pnl", "return_pct", "max_drawdown_pct")
        }},
        "horizon_usage": report["horizon_usage"], "promotion_passed": report["promotion_passed"],
        "promotion_blockers": report["promotion_blockers"], "warning": report["warning"],
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
