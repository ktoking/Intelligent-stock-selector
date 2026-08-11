#!/usr/bin/env python3
"""Causal V4 research for the relative opening-gap fade.

V4 is deliberately small.  It tests two pre-declared adaptive gates after
the V3 audit showed that most of the gap-fade edge is realised in the first
30 minutes and that LONG/SHORT edge changes through time.  A signal is
eligible only when completed trades from *prior* sessions show that the same
side still has positive-enough expectancy.

The first three chronological folds select between the two candidates.  The
diagnostic and latest folds are reported only after selection and never take
part in choosing the strategy.  This remains OHLCV research, not an order
authorization.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_gap_strategy_v3 import (  # noqa: E402
    V3Config,
    _base_candidates,
    build_features,
    dynamic_stop_bps,
    metrics,
)
from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import load_market_data, weekday_sessions  # noqa: E402
from scripts.okx_research_universe import load_symbols  # noqa: E402

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
OUTPUT = ROOT / "data" / "okx_gap_strategy_v4_backtest.json"


@dataclass(frozen=True)
class EdgeGate:
    name: str
    lookback_sessions: int
    min_side_samples: int
    min_side_expectancy_pct: float


@dataclass(frozen=True)
class V4Config:
    min_relative_gap_bps: float = 100.0
    max_relative_gap_bps: float = 600.0
    max_spy_gap_bps: float = 75.0
    min_cross_sectional_rank: float = 0.75
    horizon_minutes: int = 30
    atr_stop_multiple: float = 1.2
    min_stop_bps: float = 75.0
    max_stop_bps: float = 300.0
    round_trip_cost_bps: float = 14.0
    max_positions: int = 5
    skip_macro_days: bool = True
    skip_earnings_days: bool = True


CANDIDATES = (
    EdgeGate("side_edge_20_loose", 20, 7, -0.10),
    EdgeGate("side_edge_10_strict", 10, 7, 0.10),
)

SELECTION_FOLDS = ("validation_a", "validation_b", "validation_c")


def _side(row: pd.Series) -> str:
    return "SHORT" if float(row.relative_gap) > 0 else "LONG"


def trade_net_pct(row: pd.Series, config: V4Config) -> tuple[float, str]:
    """Replay the fixed horizon with conservative same-bar stop precedence."""
    direction = -1 if float(row.relative_gap) > 0 else 1
    stop_pct = dynamic_stop_bps(float(row.atr_bps), config) / 100.0
    bars = max(1, config.horizon_minutes // 5)
    for high, low, _close in row.path_150[:bars]:
        adverse = (
            (float(low) / float(row.entry) - 1) * 100
            if direction > 0
            else (1 - float(high) / float(row.entry)) * 100
        )
        if adverse <= -stop_pct:
            return -stop_pct - config.round_trip_cost_bps / 100.0, "atr_stop"
    exit_price = float(row[f"exit_{config.horizon_minutes}"])
    gross_pct = direction * (exit_price / float(row.entry) - 1) * 100
    return gross_pct - config.round_trip_cost_bps / 100.0, "horizon"


def label_base_candidates(rows: pd.DataFrame, config: V4Config) -> pd.DataFrame:
    """Build causal entry candidates and attach outcomes for completed sessions."""
    base = _base_candidates(rows, config).copy()
    outcomes = base.apply(lambda row: trade_net_pct(row, config), axis=1)
    base["net_pct"] = [value[0] for value in outcomes]
    base["exit_reason"] = [value[1] for value in outcomes]
    base["side"] = base.apply(_side, axis=1)
    return base


def causal_gate_trades(
    base: pd.DataFrame,
    days: list[str],
    gate: EdgeGate,
    config: V4Config,
) -> list[dict[str, Any]]:
    """Select trades using same-side outcomes from prior sessions only."""
    trades: list[dict[str, Any]] = []
    for index, day in enumerate(days):
        if index < gate.lookback_sessions:
            continue
        history_days = set(days[index - gate.lookback_sessions:index])
        history = base[base.date.isin(history_days)]
        current = base[base.date == day]
        if current.empty:
            continue
        side_stats = history.groupby("side").net_pct.agg(["count", "mean"])
        eligible_sides = {
            str(side)
            for side, value in side_stats.iterrows()
            if int(value["count"]) >= gate.min_side_samples
            and float(value["mean"]) > gate.min_side_expectancy_pct
        }
        eligible = current[current.side.isin(eligible_sides)]
        ranked = eligible.reindex(eligible.relative_gap.abs().sort_values(ascending=False).index)
        for _, row in ranked.head(config.max_positions).iterrows():
            stats = side_stats.loc[row.side]
            trades.append({
                "date": str(row.date),
                "symbol": str(row.symbol),
                "entry_time": int(row.entry_time),
                "exit_time": int(row.entry_time + config.horizon_minutes * 60_000),
                "side": str(row.side),
                "relative_gap_bps": round(float(row.relative_gap), 2),
                "prior_side_samples": int(stats["count"]),
                "prior_side_expectancy_pct": round(float(stats["mean"]), 6),
                "stop_bps": round(dynamic_stop_bps(float(row.atr_bps), config), 3),
                "net_pct": round(float(row.net_pct), 6),
                "exit_reason": str(row.exit_reason),
            })
    return trades


def ungated_trades(base: pd.DataFrame, config: V4Config) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    for _entry_time, group in base.groupby("entry_time", sort=True):
        ranked = group.reindex(group.relative_gap.abs().sort_values(ascending=False).index)
        for _, row in ranked.head(config.max_positions).iterrows():
            trades.append({
                "date": str(row.date), "symbol": str(row.symbol), "side": str(row.side),
                "net_pct": round(float(row.net_pct), 6), "exit_reason": str(row.exit_reason),
            })
    return trades


def chronological_folds(days: list[str]) -> dict[str, set[str]]:
    """Return fixed chronological folds; later folds never select a model."""
    return {
        "warmup": set(days[:20]),
        "validation_a": set(days[20:40]),
        "validation_b": set(days[40:60]),
        "validation_c": set(days[60:80]),
        "diagnostic": set(days[80:90]),
        "latest_unseen": set(days[90:]),
    }


def fold_metrics(trades: list[dict[str, Any]], folds: dict[str, set[str]]) -> dict[str, dict[str, Any]]:
    return {
        name: metrics(item for item in trades if item["date"] in target)
        for name, target in folds.items()
        if name != "warmup"
    }


def candidate_eligible(parts: dict[str, dict[str, Any]]) -> bool:
    """Pre-declared development gate; intentionally ignores later folds."""
    selected = [parts.get(name, {}) for name in SELECTION_FOLDS]
    return bool(
        sum(int(part.get("trades", 0)) for part in selected) >= 60
        and all(int(part.get("trades", 0)) >= 15 for part in selected)
        and all(float(part.get("equal_weight_net_pct", 0.0)) > 0 for part in selected)
        and all(float(part.get("profit_factor") or 0.0) >= 1.10 for part in selected)
    )


def select_candidate(results: dict[str, dict[str, Any]]) -> str | None:
    """Choose by worst-fold expectancy, using selection folds exclusively."""
    eligible = {
        name: value for name, value in results.items()
        if candidate_eligible(value["folds"])
    }
    if not eligible:
        return None

    def score(item: tuple[str, dict[str, Any]]) -> tuple[float, float]:
        parts = [item[1]["folds"][name] for name in SELECTION_FOLDS]
        minimum = min(float(part["expectancy_pct_per_trade"]) for part in parts)
        trades = sum(int(part["trades"]) for part in parts)
        combined = sum(float(part["equal_weight_net_pct"]) for part in parts) / trades
        return minimum, combined

    return max(eligible.items(), key=score)[0]


def promotion_gate(parts: dict[str, dict[str, Any]]) -> tuple[bool, list[str]]:
    """Require later diagnostics plus genuinely fresh forward sessions."""
    reasons: list[str] = []
    diagnostic = parts.get("diagnostic", {})
    latest = parts.get("latest_unseen", {})
    if int(diagnostic.get("trades", 0)) < 15:
        reasons.append("diagnostic_trades_below_15")
    if float(diagnostic.get("equal_weight_net_pct", 0.0)) <= 0:
        reasons.append("diagnostic_not_profitable")
    if float(diagnostic.get("profit_factor") or 0.0) < 1.10:
        reasons.append("diagnostic_pf_below_1_10")
    # The latest fold has already been inspected during research.  Require a
    # future forward-shadow cohort before enabling Demo execution.
    if int(latest.get("trades", 0)) < 25:
        reasons.append("fresh_forward_trades_below_25")
    return not reasons, reasons


def latest_completed_us_session(now: datetime | None = None) -> date:
    """Return the latest weekday whose 16:00 New York close has passed."""
    local = (now or datetime.now(NY)).astimezone(NY)
    candidate = local.date()
    if candidate.weekday() >= 5 or local.time() < clock_time(16, 0):
        candidate -= timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _baseline_90m(rows: pd.DataFrame) -> dict[str, Any]:
    config = V3Config()
    base = _base_candidates(rows, config).copy()
    base["side"] = base.apply(_side, axis=1)
    values: list[dict[str, Any]] = []
    for _entry_time, group in base.groupby("entry_time", sort=True):
        ranked = group.reindex(group.relative_gap.abs().sort_values(ascending=False).index)
        for _, row in ranked.head(config.max_positions).iterrows():
            direction = -1 if float(row.relative_gap) > 0 else 1
            stop_pct = dynamic_stop_bps(float(row.atr_bps), config) / 100.0
            outcome = None
            reason = "horizon"
            for high, low, _close in row.path_150[:18]:
                adverse = ((float(low) / float(row.entry) - 1) * 100 if direction > 0
                           else (1 - float(high) / float(row.entry)) * 100)
                if adverse <= -stop_pct:
                    outcome = -stop_pct - config.round_trip_cost_bps / 100.0
                    reason = "atr_stop"
                    break
            if outcome is None:
                outcome = direction * (float(row.exit_90) / float(row.entry) - 1) * 100 - config.round_trip_cost_bps / 100.0
            values.append({"date": str(row.date), "net_pct": outcome, "exit_reason": reason})
    return {"metrics": metrics(values), "trades": values}


def research(rows: pd.DataFrame) -> dict[str, Any]:
    config = V4Config()
    days = sorted(str(value) for value in rows.date.unique())
    folds = chronological_folds(days)
    base = label_base_candidates(rows, config)
    results: dict[str, dict[str, Any]] = {}
    all_trades: dict[str, list[dict[str, Any]]] = {}
    for gate in CANDIDATES:
        trades = causal_gate_trades(base, days, gate, config)
        all_trades[gate.name] = trades
        results[gate.name] = {
            "gate": asdict(gate),
            "folds": fold_metrics(trades, folds),
            "all": metrics(trades),
            "selection_eligible": False,
        }
        results[gate.name]["selection_eligible"] = candidate_eligible(results[gate.name]["folds"])
    chosen = select_candidate(results)
    chosen_parts = results[chosen]["folds"] if chosen else {}
    passed, reasons = promotion_gate(chosen_parts) if chosen else (False, ["no_candidate_passed_selection"])
    baseline_30 = ungated_trades(base, config)
    baseline_90 = _baseline_90m(rows)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "effective_sessions": {"count": len(days), "start": days[0], "end": days[-1]},
        "strategy": asdict(config),
        "candidate_family": [asdict(item) for item in CANDIDATES],
        "selection_folds": list(SELECTION_FOLDS),
        "holdout_folds_excluded_from_selection": ["diagnostic", "latest_unseen"],
        "baselines": {
            "ungated_30m": {"all": metrics(baseline_30), "folds": fold_metrics(baseline_30, folds)},
            "ungated_90m": {"all": baseline_90["metrics"], "folds": fold_metrics(baseline_90["trades"], folds)},
        },
        "candidates": results,
        "selected_candidate": chosen,
        "selected_trades": all_trades.get(chosen, []),
        "promotion_passed": passed,
        "promotion_blockers": reasons,
        "warning": (
            "OHLCV proxy with conservative stop precedence and 14bp cost. "
            "The latest fold was inspected during research and is not fresh promotion evidence."
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
    compact = {key: report[key] for key in (
        "effective_sessions", "universe_size", "baselines", "candidates",
        "selected_candidate", "promotion_passed", "promotion_blockers", "warning",
    )}
    # Avoid duplicating the full trade list in terminal output.
    for value in compact["candidates"].values():
        value.pop("trades", None)
    print(json.dumps(compact, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
