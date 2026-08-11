#!/usr/bin/env python3
"""Test a causal symbol-specific SPY beta for the V5 relative-gap fade.

Frozen V5 subtracts SPY one-for-one from every stock-token opening gap.  This
research estimates each symbol's opening-gap beta from at most 40 *prior*
sessions, shrinks it toward 1.0, and uses that same beta for the opening move,
confirmation move, and previous-day move.  It changes only the residual
definition; filters, costs, stops, horizons, and sizing remain identical.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_gap_benchmark_research import apply_benchmark, build_opening_table  # noqa: E402
from scripts.okx_gap_strategy_v3 import market_regime, metrics  # noqa: E402
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
from scripts.okx_multitimeframe_backtest import load_market_data, weekday_sessions  # noqa: E402
from scripts.okx_research_universe import load_symbols  # noqa: E402

UTC = timezone.utc
OUTPUT = ROOT / "data" / "okx_gap_beta_research.json"
BENCHMARK = "SPY-USDT-SWAP"
EXCLUDED = ("SPY-USDT-SWAP", "QQQ-USDT-SWAP")


def attach_causal_gap_beta(
    rows: pd.DataFrame,
    *,
    lookback_sessions: int = 40,
    prior_strength: float = 20.0,
    min_sessions: int = 5,
    min_beta: float = 0.5,
    max_beta: float = 2.0,
) -> pd.DataFrame:
    """Calculate a prior-only, shrunk, bounded opening-gap beta per symbol."""
    fields = ("gap_bps", "first5_bps", "previous_day_bps")
    benchmark = rows[rows.symbol == BENCHMARK][["date", *fields]].rename(
        columns={field: f"spy_{field}" for field in fields}
    )
    value = rows.merge(benchmark, on="date", how="inner")
    pieces: list[pd.DataFrame] = []
    for _symbol, group in value.groupby("symbol", sort=False):
        ordered = group.sort_values("date").copy()
        stock = ordered.gap_bps.astype(float).to_numpy()
        market = ordered.spy_gap_bps.astype(float).to_numpy()
        betas: list[float] = []
        for index in range(len(ordered)):
            start = max(0, index - lookback_sessions)
            x = market[start:index]
            y = stock[start:index]
            valid = np.isfinite(x) & np.isfinite(y)
            x = x[valid]
            y = y[valid]
            if len(x) < min_sessions or float(np.dot(x, x)) <= 1e-9:
                betas.append(1.0)
                continue
            raw_beta = float(np.dot(x, y) / np.dot(x, x))
            weight = len(x) / (len(x) + prior_strength)
            shrunk = 1.0 + (raw_beta - 1.0) * weight
            betas.append(float(np.clip(shrunk, min_beta, max_beta)))
        ordered["causal_gap_beta"] = betas
        pieces.append(ordered)
    if not pieces:
        return value.iloc[0:0].copy()

    value = pd.concat(pieces, ignore_index=True)
    value["spy_gap"] = value.spy_gap_bps
    value["spy_first5"] = value.spy_first5_bps
    value["spy_previous_day"] = value.spy_previous_day_bps
    value["relative_gap"] = value.gap_bps - value.causal_gap_beta * value.spy_gap
    value["relative_first5"] = value.first5_bps - value.causal_gap_beta * value.spy_first5
    value["relative_previous"] = (
        value.previous_day_bps - value.causal_gap_beta * value.spy_previous_day
    )
    value = value[~value.symbol.isin(EXCLUDED)].copy()
    value["relative_gap_rank"] = value.groupby("date").relative_gap.transform(
        lambda items: items.abs().rank(pct=True)
    )
    value["direction"] = np.where(value.relative_gap > 0, -1.0, 1.0)
    value["regime"] = value.spy_gap.map(
        lambda item: market_regime(item, V4Config().max_spy_gap_bps)
    )
    return value


def _development_eligible(parts: dict[str, dict[str, Any]]) -> bool:
    return all(
        int(parts[name]["trades"]) >= 15
        and float(parts[name]["equal_weight_net_pct"]) > 0.0
        and float(parts[name]["profit_factor"] or 0.0) >= 1.10
        for name in SELECTION_FOLDS
    )


def _selection_score(value: dict[str, Any]) -> tuple[float, float]:
    parts = [value["folds"][name] for name in SELECTION_FOLDS]
    total = sum(int(part["trades"]) for part in parts)
    return (
        min(float(part["expectancy_pct_per_trade"]) for part in parts),
        sum(float(part["equal_weight_net_pct"]) for part in parts) / total if total else -np.inf,
    )


def assess(rows: pd.DataFrame, days: list[str]) -> dict[str, Any]:
    trade_config = replace(V4Config(), skip_macro_days=False, skip_earnings_days=False)
    adaptive = AdaptiveHorizonConfig()
    base = add_horizon_outcomes(label_base_candidates(rows, trade_config), trade_config)
    trades = causal_adaptive_horizon_trades(base, days, adaptive)
    folds = chronological_folds(days)
    parts = {
        name: metrics(item for item in trades if item["date"] in target)
        for name, target in folds.items() if name != "warmup"
    }
    recent_month = set(days[-20:])
    recent_week = set(days[-5:])
    return {
        "feature_rows": len(rows),
        "base_candidates": len(base),
        "all": metrics(trades),
        "folds": parts,
        "recent_month": metrics(item for item in trades if item["date"] in recent_month),
        "recent_week": metrics(item for item in trades if item["date"] in recent_week),
        "risk_weighted_portfolio": risk_weighted_portfolio_metrics(trades, adaptive),
        "robustness": robustness_report(
            trades, days, adaptive, trade_config.round_trip_cost_bps, bootstrap_samples=2_000,
        ),
        "development_eligible": _development_eligible(parts),
        "trades": trades,
    }


def research(opening_rows: pd.DataFrame) -> dict[str, Any]:
    baseline = apply_benchmark(opening_rows, {"SPY": 1.0})
    rolling = attach_causal_gap_beta(opening_rows)
    days = sorted(str(value) for value in baseline.date.unique())
    results = {
        "fixed_beta_1": assess(baseline, days),
        "rolling_beta_40_shrunk_to_1": assess(rolling, days),
    }
    eligible = [name for name, value in results.items() if value["development_eligible"]]
    selected = max(eligible, key=lambda name: _selection_score(results[name]), default=None)
    base = results["fixed_beta_1"]
    challenger = results["rolling_beta_40_shrunk_to_1"]
    improves_each_fold = all(
        float(challenger["folds"][fold]["expectancy_pct_per_trade"])
        > float(base["folds"][fold]["expectancy_pct_per_trade"])
        for fold in SELECTION_FOLDS
    )
    improves_risk_portfolio = (
        float(challenger["risk_weighted_portfolio"]["return_pct"])
        > float(base["risk_weighted_portfolio"]["return_pct"])
        and float(challenger["risk_weighted_portfolio"]["max_drawdown_pct"])
        <= float(base["risk_weighted_portfolio"]["max_drawdown_pct"])
    )
    beta_values = rolling.causal_gap_beta.astype(float)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "effective_sessions": {"count": len(days), "start": days[0], "end": days[-1]},
        "experiment": "symbol_specific_prior40_opening_gap_beta_shrunk_to_one",
        "beta_config": {
            "lookback_sessions": 40, "prior_strength": 20, "min_sessions": 5,
            "bounds": [0.5, 2.0], "uses_current_session": False,
        },
        "beta_distribution": {
            "p05": round(float(beta_values.quantile(0.05)), 4),
            "median": round(float(beta_values.median()), 4),
            "p95": round(float(beta_values.quantile(0.95)), 4),
        },
        "trade_config": asdict(replace(V4Config(), skip_macro_days=False, skip_earnings_days=False)),
        "adaptive_config": asdict(AdaptiveHorizonConfig()),
        "selection_folds": list(SELECTION_FOLDS),
        "results": results,
        "selected_by_development_folds": selected,
        "strict_challenger_passed": bool(
            challenger["development_eligible"] and improves_each_fold and improves_risk_portfolio
        ),
        "forward_shadow_recommendation": (
            "freeze_rolling_beta_as_separate_shadow_challenger"
            if challenger["development_eligible"] and improves_each_fold and improves_risk_portfolio
            else "retain_frozen_v5_fixed_beta_1"
        ),
        "promotion_passed": False,
        "warning": (
            "Retrospective residual-definition research. A selected challenger still requires "
            "new forward sessions and historical spread/fill validation."
        ),
    }


def run(end: date | None = None, sessions_count: int = 100) -> dict[str, Any]:
    sessions = weekday_sessions(end or latest_completed_us_session(), sessions_count)
    symbols = load_symbols("historical_90d")
    raw = load_market_data(OKX(settings()), symbols, sessions)
    report = research(build_opening_table(raw))
    report["requested_sessions"] = sessions_count
    report["universe_size"] = len(symbols)
    return report


def main() -> None:
    report = run()
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    compact = {
        "effective_sessions": report["effective_sessions"],
        "beta_distribution": report["beta_distribution"],
        "selected_by_development_folds": report["selected_by_development_folds"],
        "strict_challenger_passed": report["strict_challenger_passed"],
        "forward_shadow_recommendation": report["forward_shadow_recommendation"],
        "results": {
            name: {
                "base_candidates": value["base_candidates"], "all": value["all"],
                "folds": value["folds"], "recent_month": value["recent_month"],
                "risk_weighted_return_pct": value["risk_weighted_portfolio"]["return_pct"],
                "risk_weighted_max_drawdown_pct": value["risk_weighted_portfolio"]["max_drawdown_pct"],
                "development_eligible": value["development_eligible"],
            }
            for name, value in report["results"].items()
        },
        "promotion_passed": report["promotion_passed"],
        "warning": report["warning"],
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
