#!/usr/bin/env python3
"""Test execution-management overlays against the frozen V5 gap-fade rule.

Entry selection remains identical to V5.  A newly triggered breakeven or ATR
trail becomes active only on the next five-minute bar, so the replay never
assumes a favorable intrabar ordering.  These overlays are research only;
the script does not mutate the Demo executor or the forward-shadow lane.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_gap_strategy_v3 import build_features, dynamic_stop_bps, metrics  # noqa: E402
from scripts.okx_gap_strategy_v4 import (  # noqa: E402
    SELECTION_FOLDS,
    V4Config,
    chronological_folds,
    label_base_candidates,
    latest_completed_us_session,
    trade_net_pct,
)
from scripts.okx_gap_strategy_v5 import (  # noqa: E402
    AdaptiveHorizonConfig,
    causal_adaptive_horizon_trades,
    risk_weighted_portfolio_metrics,
    robustness_report,
)
from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import load_market_data, weekday_sessions  # noqa: E402
from scripts.okx_research_universe import load_symbols  # noqa: E402

UTC = timezone.utc
OUTPUT = ROOT / "data" / "okx_gap_execution_management_research.json"
FROZEN_HORIZONS = (30, 60, 90)
MANAGEMENT_MODES = (
    "frozen_fixed_horizon",
    "breakeven_at_1r",
    "partial_40_at_1_5r",
    "breakeven_plus_atr_trail",
    "full_partial_breakeven_trail",
)


def managed_outcome(
    row: pd.Series,
    horizon_minutes: int,
    trade_config: V4Config,
    mode: str,
) -> tuple[float, str]:
    """Replay one managed exit with stop-first, next-bar activation semantics."""
    if mode not in MANAGEMENT_MODES[1:]:
        raise ValueError(f"unsupported managed mode: {mode}")
    use_breakeven = mode in {
        "breakeven_at_1r", "breakeven_plus_atr_trail", "full_partial_breakeven_trail",
    }
    use_partial = mode in {"partial_40_at_1_5r", "full_partial_breakeven_trail"}
    use_trail = mode in {"breakeven_plus_atr_trail", "full_partial_breakeven_trail"}
    direction = -1 if float(row.relative_gap) > 0 else 1
    entry = float(row.entry)
    stop_bps = dynamic_stop_bps(float(row.atr_bps), trade_config)
    risk = entry * stop_bps / 10_000.0
    atr_distance = entry * float(row.atr_bps) / 10_000.0
    active_stop = entry - direction * risk
    favorable_extreme = entry
    remaining = 1.0
    gross_pct = 0.0
    partial_filled = False
    reason = "horizon"
    for high, low, _close in row.path_150[: max(1, horizon_minutes // 5)]:
        high, low = float(high), float(low)
        stop_hit = low <= active_stop if direction > 0 else high >= active_stop
        if stop_hit:
            gross_pct += remaining * direction * (active_stop / entry - 1.0) * 100.0
            remaining = 0.0
            reason = "managed_stop"
            break

        favorable = high if direction > 0 else low
        favorable_extreme = (
            max(favorable_extreme, favorable) if direction > 0
            else min(favorable_extreme, favorable)
        )
        favorable_r = direction * (favorable - entry) / risk
        if use_partial and not partial_filled and favorable_r >= 1.5:
            gross_pct += 0.40 * 1.5 * stop_bps / 100.0
            remaining -= 0.40
            partial_filled = True

        # All changes below are based on this completed bar and become active
        # only for the following bar.
        next_stop = active_stop
        if use_breakeven and favorable_r >= 1.0:
            next_stop = max(next_stop, entry) if direction > 0 else min(next_stop, entry)
        if use_trail and favorable_r >= 1.2:
            trailing_stop = favorable_extreme - direction * atr_distance
            next_stop = (
                max(next_stop, trailing_stop) if direction > 0
                else min(next_stop, trailing_stop)
            )
        active_stop = next_stop
    if remaining > 0.0:
        exit_price = float(row[f"exit_{horizon_minutes}"])
        gross_pct += remaining * direction * (exit_price / entry - 1.0) * 100.0
        reason = "horizon_partial" if partial_filled else "horizon"
    return gross_pct - trade_config.round_trip_cost_bps / 100.0, reason


def label_outcomes(
    base: pd.DataFrame,
    trade_config: V4Config,
    mode: str,
    horizons: tuple[int, ...],
) -> pd.DataFrame:
    value = base.copy()
    value["stop_bps"] = value.atr_bps.map(
        lambda item: dynamic_stop_bps(float(item), trade_config),
    )
    value["side"] = value.relative_gap.map(lambda item: "SHORT" if item > 0 else "LONG")
    for horizon in horizons:
        if mode == "frozen_fixed_horizon":
            outcomes = value.apply(
                lambda row: trade_net_pct(row, replace(trade_config, horizon_minutes=horizon)), axis=1,
            )
        else:
            outcomes = value.apply(
                lambda row: managed_outcome(row, horizon, trade_config, mode), axis=1,
            )
        value[f"net_{horizon}"] = [item[0] for item in outcomes]
        value[f"reason_{horizon}"] = [item[1] for item in outcomes]
    return value


def assess(
    base: pd.DataFrame,
    days: list[str],
    trade_config: V4Config,
    mode: str,
    horizons: tuple[int, ...],
) -> dict[str, Any]:
    adaptive = AdaptiveHorizonConfig(horizons_minutes=horizons)
    labeled = label_outcomes(base, trade_config, mode, horizons)
    trades = causal_adaptive_horizon_trades(labeled, days, adaptive)
    folds = chronological_folds(days)
    parts = {
        name: metrics(item for item in trades if item["date"] in target)
        for name, target in folds.items() if name != "warmup"
    }
    return {
        "mode": mode,
        "horizons_minutes": list(horizons),
        "all": metrics(trades),
        "folds": parts,
        "risk_weighted_portfolio": risk_weighted_portfolio_metrics(trades, adaptive),
        "robustness": robustness_report(
            trades, days, adaptive, trade_config.round_trip_cost_bps, bootstrap_samples=5_000,
        ),
        "horizon_usage": {
            str(horizon): sum(int(item["horizon_minutes"]) == horizon for item in trades)
            for horizon in horizons
        },
        "trades": trades,
    }


def _strictly_improves(challenger: dict[str, Any], baseline: dict[str, Any]) -> bool:
    return bool(
        all(
            float(challenger["folds"][fold]["expectancy_pct_per_trade"])
            > float(baseline["folds"][fold]["expectancy_pct_per_trade"])
            for fold in SELECTION_FOLDS
        )
        and float(challenger["risk_weighted_portfolio"]["return_pct"])
        > float(baseline["risk_weighted_portfolio"]["return_pct"])
        and float(challenger["risk_weighted_portfolio"]["max_drawdown_pct"])
        <= float(baseline["risk_weighted_portfolio"]["max_drawdown_pct"])
    )


def research(rows: pd.DataFrame) -> dict[str, Any]:
    days = sorted(str(value) for value in rows.date.unique())
    trade_config = replace(V4Config(), skip_macro_days=False, skip_earnings_days=False)
    base = label_base_candidates(rows, trade_config)
    results = {
        mode: assess(base, days, trade_config, mode, FROZEN_HORIZONS)
        for mode in MANAGEMENT_MODES
    }
    extensions = {
        "through_120m": assess(
            base, days, trade_config, "frozen_fixed_horizon", (30, 60, 90, 120),
        ),
        "through_150m": assess(
            base, days, trade_config, "frozen_fixed_horizon", (30, 60, 90, 120, 150),
        ),
    }
    baseline = results["frozen_fixed_horizon"]
    passed = [
        name for name, value in {**results, **extensions}.items()
        if name != "frozen_fixed_horizon" and _strictly_improves(value, baseline)
    ]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "effective_sessions": {"count": len(days), "start": days[0], "end": days[-1]},
        "trade_config": asdict(trade_config),
        "entry_rule": "frozen V5 candidate membership and causal side-specific horizon choice",
        "intrabar_policy": "active stop first; new BE/trail effective next 5m bar",
        "results": results,
        "horizon_extensions": extensions,
        "strict_challengers": passed,
        "promotion_passed": False,
        "recommendation": (
            "retain_frozen_fixed_horizon" if not passed else "forward_shadow_best_strict_challenger"
        ),
        "warning": (
            "All sessions were already inspected; this can reject overlays but cannot authorize execution."
        ),
    }


def run() -> dict[str, Any]:
    end = latest_completed_us_session()
    # Match V5's 100 requested weekdays; feature construction needs the first
    # day only as the prior session, leaving 99 effective entry sessions.
    session_dates = weekday_sessions(end, 100)
    raw = load_market_data(OKX(settings()), load_symbols("historical_90d"), session_dates)
    return research(build_features(raw))


def main() -> None:
    report = run()
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({
        "effective_sessions": report["effective_sessions"],
        "results": {
            name: {
                "all": value["all"],
                "folds": value["folds"],
                "risk_return_pct": value["risk_weighted_portfolio"]["return_pct"],
                "risk_max_drawdown_pct": value["risk_weighted_portfolio"]["max_drawdown_pct"],
            }
            for name, value in report["results"].items()
        },
        "horizon_extensions": {
            name: {
                "all": value["all"],
                "folds": value["folds"],
                "risk_return_pct": value["risk_weighted_portfolio"]["return_pct"],
                "risk_max_drawdown_pct": value["risk_weighted_portfolio"]["max_drawdown_pct"],
            }
            for name, value in report["horizon_extensions"].items()
        },
        "strict_challengers": report["strict_challengers"],
        "recommendation": report["recommendation"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
