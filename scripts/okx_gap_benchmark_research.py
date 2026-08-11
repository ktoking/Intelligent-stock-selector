#!/usr/bin/env python3
"""Compare SPY, QQQ, and a 50/50 opening-gap benchmark for V5.

All variants use identical stock-token paths, costs, stops, adaptive horizons,
and chronological folds.  The benchmark is the only changed input.  This is
retrospective model research because prior V5 results were already inspected.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_gap_feature_research import opening_features  # noqa: E402
from scripts.okx_gap_research import opportunities  # noqa: E402
from scripts.okx_gap_strategy_v3 import load_event_sets, market_regime, metrics  # noqa: E402
from scripts.okx_gap_strategy_v4 import (  # noqa: E402
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
)
from scripts.okx_intraday_agent import NY, OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import load_market_data, weekday_sessions  # noqa: E402
from scripts.okx_research_universe import load_symbols  # noqa: E402

UTC = timezone.utc
OUTPUT = ROOT / "data" / "okx_gap_benchmark_v6_research.json"
BENCHMARKS = {
    "SPY": {"SPY": 1.0, "QQQ": 0.0},
    "QQQ": {"SPY": 0.0, "QQQ": 1.0},
    "SPY_QQQ_50_50": {"SPY": 0.5, "QQQ": 0.5},
}


def build_opening_table(raw: dict[str, list[list[str]]]) -> pd.DataFrame:
    rows = opportunities(raw, excluded_symbols=()).merge(
        opening_features(raw), on=["symbol", "date"], how="left",
    )
    rows = rows.dropna(subset=[
        "entry", "exit_30", "exit_60", "exit_90", "open_volume_ratio", "prior_range_bps",
    ]).copy()
    rows = rows[rows.path_150.map(lambda item: isinstance(item, (list, tuple)))].copy()
    macro, earnings = load_event_sets()
    ticker = rows.symbol.astype(str).str.split("-", n=1).str[0].str.upper()
    rows["macro_day"] = rows.date.astype(str).isin(macro).astype(float)
    rows["earnings_window"] = [float((name, str(day)) in earnings) for name, day in zip(ticker, rows.date)]
    rows["atr_bps"] = (rows.prior_range_bps.astype(float) / 8.0).clip(lower=20.0, upper=250.0)
    stamp = pd.to_datetime(rows.entry_time.astype("int64"), unit="ms", utc=True).dt.tz_convert(NY)
    rows["weekday"] = stamp.dt.weekday.astype(float)
    return rows


def apply_benchmark(rows: pd.DataFrame, weights: dict[str, float]) -> pd.DataFrame:
    """Attach one causal benchmark and remove benchmark instruments from trading."""
    benchmark = None
    fields = ("gap_bps", "first5_bps", "previous_day_bps")
    for name, weight in weights.items():
        part = rows[rows.symbol == f"{name}-USDT-SWAP"][["date", *fields]].copy()
        part = part.rename(columns={field: f"{name}_{field}" for field in fields})
        benchmark = part if benchmark is None else benchmark.merge(part, on="date", how="inner")
    if benchmark is None or benchmark.empty:
        return rows.iloc[0:0].copy()
    value = rows.merge(benchmark, on="date", how="inner")
    for field in fields:
        value[f"benchmark_{field}"] = sum(
            float(weight) * value[f"{name}_{field}"] for name, weight in weights.items()
        )
    value["spy_gap"] = value.benchmark_gap_bps
    value["spy_first5"] = value.benchmark_first5_bps
    value["spy_previous_day"] = value.benchmark_previous_day_bps
    value["relative_gap"] = value.gap_bps - value.spy_gap
    value["relative_first5"] = value.first5_bps - value.spy_first5
    value["relative_previous"] = value.previous_day_bps - value.spy_previous_day
    value = value[~value.symbol.isin(["SPY-USDT-SWAP", "QQQ-USDT-SWAP"])].copy()
    value["relative_gap_rank"] = value.groupby("date").relative_gap.transform(
        lambda items: items.abs().rank(pct=True)
    )
    value["direction"] = np.where(value.relative_gap > 0, -1.0, 1.0)
    value["regime"] = value.spy_gap.map(lambda item: market_regime(item, V4Config().max_spy_gap_bps))
    return value


def _fold_metrics(trades: list[dict[str, Any]], folds: dict[str, set[str]]) -> dict[str, Any]:
    return {
        name: metrics(item for item in trades if item["date"] in target)
        for name, target in folds.items() if name != "warmup"
    }


def research(rows: pd.DataFrame) -> dict[str, Any]:
    days = sorted(str(value) for value in rows.date.unique())
    folds = chronological_folds(days)
    trade_config = replace(V4Config(), skip_macro_days=False, skip_earnings_days=False)
    adaptive = AdaptiveHorizonConfig()
    results: dict[str, Any] = {}
    for name, weights in BENCHMARKS.items():
        value = apply_benchmark(rows, weights)
        base = add_horizon_outcomes(label_base_candidates(value, trade_config), trade_config)
        trades = causal_adaptive_horizon_trades(base, days, adaptive)
        results[name] = {
            "weights": weights, "base_candidates": len(base), "all": metrics(trades),
            "folds": _fold_metrics(trades, folds),
            "risk_weighted_portfolio": risk_weighted_portfolio_metrics(trades, adaptive),
            "trades": trades,
        }
    selection_names = ("validation_a", "validation_b", "validation_c")
    eligible = {
        name: value for name, value in results.items()
        if all(value["folds"][fold]["trades"] >= 15 for fold in selection_names)
        and all(value["folds"][fold]["equal_weight_net_pct"] > 0 for fold in selection_names)
        and all((value["folds"][fold]["profit_factor"] or 0) >= 1.10 for fold in selection_names)
    }
    selected = max(
        eligible,
        key=lambda name: min(results[name]["folds"][fold]["expectancy_pct_per_trade"] for fold in selection_names),
        default=None,
    )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "effective_sessions": {"count": len(days), "start": days[0], "end": days[-1]},
        "trade_config": asdict(trade_config), "adaptive_config": asdict(adaptive),
        "selection_folds": list(selection_names), "results": results,
        "selected_by_development_folds": selected,
        "promotion_passed": False,
        "warning": "Retrospective benchmark research; diagnostic/latest folds were already inspected.",
    }


def run(sessions_count: int = 100) -> dict[str, Any]:
    sessions = weekday_sessions(latest_completed_us_session(), sessions_count)
    symbols = load_symbols("historical_90d")
    raw = load_market_data(OKX(settings()), symbols, sessions)
    report = research(build_opening_table(raw))
    report["universe_size"] = len(symbols)
    return report


def main() -> None:
    report = run()
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    compact = {
        "effective_sessions": report["effective_sessions"],
        "selected_by_development_folds": report["selected_by_development_folds"],
        "results": {
            name: {"all": value["all"], "folds": value["folds"],
                   "risk_weighted_return_pct": value["risk_weighted_portfolio"]["return_pct"],
                   "risk_weighted_max_drawdown_pct": value["risk_weighted_portfolio"]["max_drawdown_pct"]}
            for name, value in report["results"].items()
        },
        "promotion_passed": report["promotion_passed"], "warning": report["warning"],
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
