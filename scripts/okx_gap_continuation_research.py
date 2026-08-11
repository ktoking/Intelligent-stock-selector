#!/usr/bin/env python3
"""Cross-source validation of a causal opening-gap continuation challenger.

The US-stock hourly proxy suggested that some symbols continue a quiet-market
relative opening gap when their own completed history supports the pattern.
This replay moves that idea to OKX's five-minute path: entry is 09:35 New York,
all confirmation inputs are known then, stops are checked on subsequent 5m
bars, and an optional symbol gate uses only Yahoo proxy sessions before the
OKX trade date.  This remains retrospective research and cannot enable Demo.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_gap_strategy_v3 import build_features, dynamic_stop_bps, metrics  # noqa: E402
from scripts.okx_gap_strategy_v4 import V4Config, chronological_folds, latest_completed_us_session  # noqa: E402
from scripts.okx_gap_strategy_v5 import (  # noqa: E402
    AdaptiveHorizonConfig,
    risk_weighted_portfolio_metrics,
    robustness_report,
    side_horizon_choices,
)
from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import load_market_data, weekday_sessions  # noqa: E402
from scripts.okx_research_universe import load_symbols  # noqa: E402
from scripts.yfinance_gap_proxy_walkforward import (  # noqa: E402
    SymbolEdgeGateConfig,
    base_candidates as proxy_base_candidates,
    build_proxy_rows,
)
from scripts.yfinance_regime_walkforward import download as yfinance_download, sessions as yfinance_sessions  # noqa: E402

UTC = timezone.utc
OUTPUT = ROOT / "data" / "okx_gap_continuation_research.json"
HORIZONS = (30, 60, 90)
VARIANTS = ("prior_aligned", "first5_or_prior_aligned", "first5_strict_aligned")
STOP_MODES = ("v5_dynamic", "fixed_300bp")
PROXY_HORIZON_MAP = {30: 60, 60: 120, 90: 180}


@dataclass(frozen=True)
class ContinuationConfig:
    min_relative_gap_bps: float = 100.0
    max_relative_gap_bps: float = 600.0
    max_spy_gap_bps: float = 75.0
    min_cross_sectional_rank: float = 0.75
    round_trip_cost_bps: float = 14.0
    max_positions: int = 5


def continuation_outcome(
    row: pd.Series,
    horizon: int,
    trade_config: V4Config,
    stop_mode: str,
) -> tuple[float, str]:
    if stop_mode not in STOP_MODES:
        raise ValueError(f"unknown stop mode: {stop_mode}")
    direction = 1 if float(row.relative_gap) > 0 else -1
    stop_bps = (
        300.0 if stop_mode == "fixed_300bp"
        else dynamic_stop_bps(float(row.atr_bps), trade_config)
    )
    stop_pct = stop_bps / 100.0
    for high, low, _close in row.path_150[: max(1, horizon // 5)]:
        adverse = (
            (float(low) / float(row.entry) - 1.0) * 100.0
            if direction > 0
            else (1.0 - float(high) / float(row.entry)) * 100.0
        )
        if adverse <= -stop_pct:
            return -stop_pct - trade_config.round_trip_cost_bps / 100.0, "stop"
    gross = direction * (float(row[f"exit_{horizon}"]) / float(row.entry) - 1.0) * 100.0
    return gross - trade_config.round_trip_cost_bps / 100.0, "horizon"


def continuation_base(
    rows: pd.DataFrame,
    variant: str,
    stop_mode: str,
    config: ContinuationConfig | None = None,
) -> pd.DataFrame:
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant: {variant}")
    config = config or ContinuationConfig()
    value = rows[
        rows.relative_gap.abs().between(
            config.min_relative_gap_bps, config.max_relative_gap_bps, inclusive="both",
        )
        & (rows.spy_gap.abs() <= config.max_spy_gap_bps)
        & (rows.relative_gap_rank >= config.min_cross_sectional_rank)
        & rows.exit_90.notna()
    ].copy()
    prior_aligned = value.relative_gap * value.relative_previous >= 0
    first5_aligned = value.relative_gap * value.relative_first5 > 0
    if variant == "prior_aligned":
        value = value[prior_aligned]
    elif variant == "first5_or_prior_aligned":
        value = value[first5_aligned | prior_aligned]
    else:
        value = value[first5_aligned]
    trade_config = replace(
        V4Config(), round_trip_cost_bps=config.round_trip_cost_bps,
        skip_macro_days=False, skip_earnings_days=False,
    )
    value["side"] = value.relative_gap.map(lambda item: "LONG" if item > 0 else "SHORT")
    value["stop_bps"] = (
        300.0 if stop_mode == "fixed_300bp"
        else value.atr_bps.map(lambda item: dynamic_stop_bps(float(item), trade_config))
    )
    if value.empty:
        for horizon in HORIZONS:
            value[f"net_{horizon}"] = pd.Series(dtype=float)
            value[f"reason_{horizon}"] = pd.Series(dtype=str)
        return value
    for horizon in HORIZONS:
        outcomes = value.apply(
            lambda row: continuation_outcome(row, horizon, trade_config, stop_mode), axis=1,
        )
        value[f"net_{horizon}"] = [item[0] for item in outcomes]
        value[f"reason_{horizon}"] = [item[1] for item in outcomes]
    return value


def causal_continuation_trades(
    base: pd.DataFrame,
    days: list[str],
    adaptive: AdaptiveHorizonConfig,
    proxy_base: pd.DataFrame | None = None,
    proxy_gate: SymbolEdgeGateConfig | None = None,
) -> list[dict[str, Any]]:
    """Replay OKX signals; optional symbol quality comes from earlier proxy rows."""
    trades: list[dict[str, Any]] = []
    for index, day_name in enumerate(days):
        if index < adaptive.lookback_sessions:
            continue
        history_days = set(days[index - adaptive.lookback_sessions:index])
        choices = side_horizon_choices(base[base.date.isin(history_days)], adaptive)
        current = base[(base.date == day_name) & base.side.isin(choices)]
        ranked = current.sort_values(
            "relative_gap", key=lambda values: values.abs(), ascending=False,
        ).head(adaptive.max_positions)
        for _, row in ranked.iterrows():
            choice = choices[str(row.side)]
            horizon = int(choice["horizon_minutes"])
            proxy_samples = None
            proxy_edge = None
            if proxy_base is not None and proxy_gate is not None:
                ticker = str(row.symbol).split("-", 1)[0]
                prior_dates = sorted(
                    str(value) for value in proxy_base.date.unique() if str(value) < day_name
                )[-proxy_gate.lookback_sessions:]
                prior = proxy_base[
                    (proxy_base.symbol == ticker) & proxy_base.date.isin(set(prior_dates))
                ]
                proxy_horizon = PROXY_HORIZON_MAP[horizon]
                proxy_values = prior[f"net_{proxy_horizon}"].astype(float)
                proxy_samples = len(proxy_values)
                if proxy_samples < proxy_gate.min_samples:
                    continue
                proxy_edge = float(proxy_values.mean())
                if proxy_edge <= proxy_gate.min_expectancy_pct:
                    continue
            trades.append({
                "date": str(row.date), "symbol": str(row.symbol), "side": str(row.side),
                "entry_time": int(row.entry_time),
                "horizon_minutes": horizon,
                "relative_gap_bps": round(float(row.relative_gap), 2),
                "prior_side_samples": int(choice["prior_side_samples"]),
                "prior_side_expectancy_pct": float(choice["prior_best_horizon_expectancy_pct"]),
                "proxy_symbol_samples": proxy_samples,
                "proxy_symbol_expectancy_pct": round(proxy_edge, 6) if proxy_edge is not None else None,
                "stop_bps": round(float(row.stop_bps), 3),
                "net_pct": round(float(row[f"net_{horizon}"]), 6),
                "exit_reason": str(row[f"reason_{horizon}"]),
            })
    return trades


def assess(
    trades: list[dict[str, Any]], days: list[str], adaptive: AdaptiveHorizonConfig,
    cost_bps: float,
) -> dict[str, Any]:
    folds = chronological_folds(days)
    parts = {
        name: metrics(item for item in trades if item["date"] in target)
        for name, target in folds.items() if name != "warmup"
    }
    return {
        "all": metrics(trades),
        "folds": parts,
        "risk_weighted_portfolio": risk_weighted_portfolio_metrics(trades, adaptive),
        "robustness": robustness_report(trades, days, adaptive, cost_bps, bootstrap_samples=5_000),
        "trades": trades,
    }


def research(rows: pd.DataFrame, days: list[str], proxy_base: pd.DataFrame) -> dict[str, Any]:
    config = ContinuationConfig()
    adaptive = AdaptiveHorizonConfig(horizons_minutes=HORIZONS)
    proxy_gate = SymbolEdgeGateConfig()
    results: dict[str, Any] = {}
    for variant in VARIANTS:
        results[variant] = {}
        for stop_mode in STOP_MODES:
            base = continuation_base(rows, variant, stop_mode, config)
            baseline = causal_continuation_trades(base, days, adaptive)
            gated = causal_continuation_trades(base, days, adaptive, proxy_base, proxy_gate)
            results[variant][stop_mode] = {
                "base_candidates": len(base),
                "ungated": assess(baseline, days, adaptive, config.round_trip_cost_bps),
                "cross_source_symbol_gate": assess(gated, days, adaptive, config.round_trip_cost_bps),
            }
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "effective_sessions": {"count": len(days), "start": days[0], "end": days[-1]},
        "strategy": asdict(config),
        "adaptive": asdict(adaptive),
        "proxy_gate": {
            **asdict(proxy_gate),
            "horizon_mapping": {str(key): value for key, value in PROXY_HORIZON_MAP.items()},
            "strictly_prior_proxy_dates": True,
        },
        "results": results,
        "promotion_passed": False,
        "warning": (
            "All OKX folds and the Yahoo proxy were already inspected. This is a cross-source "
            "retrospective challenger; only a new forward-shadow cohort can authorize Demo."
        ),
    }


def run() -> dict[str, Any]:
    end = latest_completed_us_session()
    session_dates = weekday_sessions(end, 99)
    days = [item.isoformat() for item in session_dates]
    symbols = load_symbols("historical_90d")
    raw = load_market_data(OKX(settings()), symbols, session_dates)
    rows = build_features(raw)
    proxy_market = yfinance_sessions(yfinance_download())
    proxy_rows = build_proxy_rows(proxy_market, "open_prior_continuation")
    proxy_trade_config = replace(V4Config(), skip_macro_days=False, skip_earnings_days=False)
    proxy_base = proxy_base_candidates(
        proxy_rows, "open_prior_continuation", proxy_trade_config, "hour_close_300bp",
    )
    return research(rows, days, proxy_base)


def main() -> None:
    report = run()
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    compact = {
        "effective_sessions": report["effective_sessions"],
        "proxy_gate": report["proxy_gate"],
        "results": {
            variant: {
                stop: {
                    key: {
                        "all": value[key]["all"],
                        "folds": value[key]["folds"],
                        "risk_return_pct": value[key]["risk_weighted_portfolio"]["return_pct"],
                        "risk_max_drawdown_pct": value[key]["risk_weighted_portfolio"]["max_drawdown_pct"],
                    }
                    for key in ("ungated", "cross_source_symbol_gate")
                }
                for stop, value in stops.items()
            }
            for variant, stops in report["results"].items()
        },
        "promotion_passed": report["promotion_passed"],
        "warning": report["warning"],
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
