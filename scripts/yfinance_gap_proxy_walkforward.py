#!/usr/bin/env python3
"""Two-year hourly proxy validation for the V5 relative opening-gap premise.

This uses adjusted US-stock hourly bars, not OKX contracts.  It therefore
tests whether idiosyncratic overnight gaps tend to fade intraday across a much
longer period, while execution costs, spreads, and fills remain governed by
the separate OKX replay.  All entry filters are known before the chosen entry.
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

from scripts.okx_gap_strategy_v3 import dynamic_stop_bps, metrics  # noqa: E402
from scripts.okx_gap_strategy_v4 import V4Config  # noqa: E402
from scripts.okx_gap_strategy_v5 import (  # noqa: E402
    AdaptiveHorizonConfig,
    risk_weighted_portfolio_metrics,
    robustness_report,
)
from scripts.yfinance_regime_walkforward import SYMBOLS, download, sessions  # noqa: E402

UTC = timezone.utc
OUTPUT = ROOT / "data" / "yfinance_gap_proxy_walkforward.json"
HORIZONS = (60, 120, 180)
FADE_VARIANTS = ("open_prior_confirm", "hour1_or_prior_confirm", "hour1_strict_confirm")
CONTINUATION_VARIANTS = (
    "open_prior_continuation",
    "hour1_or_prior_continuation",
    "hour1_strict_continuation",
)
VARIANTS = FADE_VARIANTS + CONTINUATION_VARIANTS
SELECTION_FOLDS = ("development_a", "development_b", "development_c")


@dataclass(frozen=True)
class SymbolEdgeGateConfig:
    lookback_sessions: int = 240
    min_samples: int = 8
    min_expectancy_pct: float = 0.0


def _record(
    symbol: str,
    day_name: str,
    previous: list[dict[str, float]],
    current: list[dict[str, float]],
    entry_after_hours: int,
) -> dict[str, Any] | None:
    if len(previous) < 6 or len(current) < entry_after_hours + 3:
        return None
    previous_close = float(previous[-1]["close"])
    previous_open = float(previous[0]["open"])
    current_open = float(current[0]["open"])
    if min(previous_close, previous_open, current_open) <= 0:
        return None
    entry = float(current[entry_after_hours]["open"])
    path = current[entry_after_hours:entry_after_hours + 3]
    value: dict[str, Any] = {
        "symbol": symbol,
        "date": day_name,
        "entry": entry,
        "gap_bps": (current_open / previous_close - 1.0) * 10_000.0,
        "first_hour_bps": (float(current[0]["close"]) / current_open - 1.0) * 10_000.0,
        "previous_day_bps": (previous_close / previous_open - 1.0) * 10_000.0,
        "prior_range_bps": (
            max(float(item["high"]) for item in previous)
            / min(float(item["low"]) for item in previous) - 1.0
        ) * 10_000.0,
        "path_hourly": [
            (float(item["high"]), float(item["low"]), float(item["close"]))
            for item in path
        ],
    }
    for bars, horizon in enumerate(HORIZONS, 1):
        value[f"exit_{horizon}"] = float(path[bars - 1]["close"])
    return value


def build_proxy_rows(
    market: dict[str, dict[str, list[dict[str, float]]]],
    variant: str,
    trade_symbols: tuple[str, ...] = SYMBOLS,
) -> pd.DataFrame:
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant: {variant}")
    entry_after_hours = 0 if variant.startswith("open_prior_") else 1
    days = sorted(day for day, rows in market.items() if "SPY" in rows)
    records: list[dict[str, Any]] = []
    for index, day_name in enumerate(days[1:], 1):
        previous_day = days[index - 1]
        for symbol in (*trade_symbols, "SPY"):
            previous = market.get(previous_day, {}).get(symbol)
            current = market.get(day_name, {}).get(symbol)
            if not previous or not current:
                continue
            value = _record(symbol, day_name, previous, current, entry_after_hours)
            if value is not None:
                records.append(value)
    rows = pd.DataFrame(records)
    if rows.empty:
        return rows
    spy = rows[rows.symbol == "SPY"][[
        "date", "gap_bps", "first_hour_bps", "previous_day_bps",
    ]].rename(columns={
        "gap_bps": "spy_gap",
        "first_hour_bps": "spy_first_hour",
        "previous_day_bps": "spy_previous_day",
    })
    rows = rows[rows.symbol != "SPY"].merge(spy, on="date", how="inner")
    rows["relative_gap"] = rows.gap_bps - rows.spy_gap
    rows["relative_first_hour"] = rows.first_hour_bps - rows.spy_first_hour
    rows["relative_previous"] = rows.previous_day_bps - rows.spy_previous_day
    rows["relative_gap_rank"] = rows.groupby("date").relative_gap.transform(
        lambda values: values.abs().rank(pct=True)
    )
    rows["atr_bps"] = (rows.prior_range_bps.astype(float) / 8.0).clip(lower=20.0, upper=250.0)
    continuation = variant in CONTINUATION_VARIANTS
    rows["side"] = rows.relative_gap.map(
        lambda value: ("LONG" if value > 0 else "SHORT")
        if continuation else ("SHORT" if value > 0 else "LONG")
    )
    rows["thesis"] = "continuation" if continuation else "fade"
    return rows


def _hourly_outcome(
    row: pd.Series, horizon: int, config: V4Config, stop_mode: str = "v5_dynamic",
) -> tuple[float, str]:
    default_side = "SHORT" if float(row.relative_gap) > 0 else "LONG"
    direction = 1 if str(row.get("side", default_side)) == "LONG" else -1
    if stop_mode not in {
        "v5_dynamic", "fixed_300bp", "hour_close_dynamic", "hour_close_300bp", "close_only",
    }:
        raise ValueError(f"unknown stop_mode: {stop_mode}")
    stop_bps = (
        300.0 if stop_mode in {"fixed_300bp", "hour_close_300bp"}
        else dynamic_stop_bps(float(row.atr_bps), config)
    )
    stop_pct = stop_bps / 100.0
    bars = horizon // 60
    if stop_mode in {"v5_dynamic", "fixed_300bp"}:
        for high, low, _close in row.path_hourly[:bars]:
            adverse = (
                (float(low) / float(row.entry) - 1.0) * 100.0
                if direction > 0 else
                (1.0 - float(high) / float(row.entry)) * 100.0
            )
            if adverse <= -stop_pct:
                return -stop_pct - config.round_trip_cost_bps / 100.0, "atr_stop"
    elif stop_mode in {"hour_close_dynamic", "hour_close_300bp"}:
        for _high, _low, close in row.path_hourly[:bars]:
            adverse = direction * (float(close) / float(row.entry) - 1.0) * 100.0
            if adverse <= -stop_pct:
                return adverse - config.round_trip_cost_bps / 100.0, "hour_close_stop"
    gross = direction * (float(row[f"exit_{horizon}"]) / float(row.entry) - 1.0) * 100.0
    return gross - config.round_trip_cost_bps / 100.0, "horizon"


def base_candidates(
    rows: pd.DataFrame, variant: str, config: V4Config, stop_mode: str = "v5_dynamic",
) -> pd.DataFrame:
    value = rows[
        rows.relative_gap.abs().between(
            config.min_relative_gap_bps, config.max_relative_gap_bps, inclusive="both",
        )
        & (rows.spy_gap.abs() <= config.max_spy_gap_bps)
        & (rows.relative_gap_rank >= config.min_cross_sectional_rank)
    ].copy()
    continuation = variant in CONTINUATION_VARIANTS
    if continuation:
        prior_confirmed = value.relative_gap * value.relative_previous >= 0
        hour_confirmed = value.relative_gap * value.relative_first_hour > 0
    else:
        prior_confirmed = value.relative_gap * value.relative_previous <= 0
        hour_confirmed = value.relative_gap * value.relative_first_hour < 0
    if variant.startswith("open_prior_"):
        value = value[prior_confirmed]
    elif variant.startswith("hour1_or_prior_"):
        value = value[hour_confirmed | prior_confirmed]
    else:
        value = value[hour_confirmed]
    value["stop_bps"] = (
        300.0 if stop_mode in {"fixed_300bp", "hour_close_300bp"}
        else value.atr_bps.map(lambda item: dynamic_stop_bps(float(item), config))
    )
    if value.empty:
        for horizon in HORIZONS:
            value[f"net_{horizon}"] = pd.Series(dtype=float)
            value[f"reason_{horizon}"] = pd.Series(dtype=str)
        return value
    for horizon in HORIZONS:
        outcomes = value.apply(
            lambda row: _hourly_outcome(row, horizon, config, stop_mode), axis=1,
        )
        value[f"net_{horizon}"] = [item[0] for item in outcomes]
        value[f"reason_{horizon}"] = [item[1] for item in outcomes]
    return value


def _side_choices(
    history: pd.DataFrame, config: AdaptiveHorizonConfig,
) -> dict[str, dict[str, float | int]]:
    result: dict[str, dict[str, float | int]] = {}
    for side in ("LONG", "SHORT"):
        prior = history[history.side == side]
        if len(prior) < config.min_side_samples:
            continue
        edges = {horizon: float(prior[f"net_{horizon}"].mean()) for horizon in HORIZONS}
        chosen = max(edges, key=edges.get)
        if edges[chosen] <= config.min_best_expectancy_pct:
            continue
        result[side] = {
            "horizon_minutes": chosen,
            "prior_side_samples": len(prior),
            "prior_expectancy_pct": edges[chosen],
        }
    return result


def causal_trades(
    base: pd.DataFrame, days: list[str], config: AdaptiveHorizonConfig,
) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    for index, day_name in enumerate(days):
        if index < config.lookback_sessions:
            continue
        prior_days = set(days[index - config.lookback_sessions:index])
        choices = _side_choices(base[base.date.isin(prior_days)], config)
        current = base[(base.date == day_name) & base.side.isin(choices)]
        ranked = current.sort_values(
            "relative_gap", key=lambda values: values.abs(), ascending=False,
        ).head(config.max_positions)
        for _, row in ranked.iterrows():
            choice = choices[str(row.side)]
            horizon = int(choice["horizon_minutes"])
            trades.append({
                "date": str(row.date), "symbol": str(row.symbol), "side": str(row.side),
                "relative_gap_bps": round(float(row.relative_gap), 2),
                "horizon_minutes": horizon,
                "prior_side_samples": int(choice["prior_side_samples"]),
                "prior_best_horizon_expectancy_pct": round(float(choice["prior_expectancy_pct"]), 6),
                "stop_bps": round(float(row.stop_bps), 3),
                "net_pct": round(float(row[f"net_{horizon}"]), 6),
                "exit_reason": str(row[f"reason_{horizon}"]),
            })
    return trades


def causal_symbol_edge_trades(
    base: pd.DataFrame,
    days: list[str],
    config: AdaptiveHorizonConfig,
    gate: SymbolEdgeGateConfig,
) -> list[dict[str, Any]]:
    """Apply a symbol-level gate using only completed signals before each date."""
    trades: list[dict[str, Any]] = []
    for index, day_name in enumerate(days):
        if index < config.lookback_sessions:
            continue
        side_days = set(days[index - config.lookback_sessions:index])
        symbol_days = set(days[max(0, index - gate.lookback_sessions):index])
        choices = _side_choices(base[base.date.isin(side_days)], config)
        current = base[(base.date == day_name) & base.side.isin(choices)]
        ranked = current.sort_values(
            "relative_gap", key=lambda values: values.abs(), ascending=False,
        ).head(config.max_positions)
        history = base[base.date.isin(symbol_days)]
        for _, row in ranked.iterrows():
            choice = choices[str(row.side)]
            horizon = int(choice["horizon_minutes"])
            prior = history[history.symbol == row.symbol]
            prior_values = prior[f"net_{horizon}"].astype(float)
            if len(prior_values) < gate.min_samples:
                continue
            symbol_edge = float(prior_values.mean())
            if symbol_edge <= gate.min_expectancy_pct:
                continue
            trades.append({
                "date": str(row.date), "symbol": str(row.symbol), "side": str(row.side),
                "relative_gap_bps": round(float(row.relative_gap), 2),
                "horizon_minutes": horizon,
                "prior_side_samples": int(choice["prior_side_samples"]),
                "prior_best_horizon_expectancy_pct": round(float(choice["prior_expectancy_pct"]), 6),
                "prior_symbol_samples": len(prior_values),
                "prior_symbol_expectancy_pct": round(symbol_edge, 6),
                "stop_bps": round(float(row.stop_bps), 3),
                "net_pct": round(float(row[f"net_{horizon}"]), 6),
                "exit_reason": str(row[f"reason_{horizon}"]),
            })
    return trades


def chronological_proxy_folds(days: list[str]) -> dict[str, set[str]]:
    # Preserve roughly 120-session blocks while leaving the latest period out
    # of selection.  For 494 sessions this yields 3x120 development + 114 diagnostic.
    return {
        "warmup": set(days[:20]),
        "development_a": set(days[20:140]),
        "development_b": set(days[140:260]),
        "development_c": set(days[260:380]),
        "latest_diagnostic": set(days[380:]),
    }


def _eligible(parts: dict[str, dict[str, Any]]) -> bool:
    return all(
        int(parts[name]["trades"]) >= 50
        and float(parts[name]["equal_weight_net_pct"]) > 0.0
        and float(parts[name]["profit_factor"] or 0.0) >= 1.10
        for name in SELECTION_FOLDS
    )


def assess(
    rows: pd.DataFrame, variant: str, days: list[str], stop_mode: str = "v5_dynamic",
) -> dict[str, Any]:
    trade_config = replace(V4Config(), skip_macro_days=False, skip_earnings_days=False)
    adaptive = AdaptiveHorizonConfig(horizons_minutes=HORIZONS)
    base = base_candidates(rows, variant, trade_config, stop_mode)
    trades = causal_trades(base, days, adaptive)
    folds = chronological_proxy_folds(days)
    parts = {
        name: metrics(item for item in trades if item["date"] in target)
        for name, target in folds.items() if name != "warmup"
    }
    years = sorted({str(item["date"])[:4] for item in trades})
    return {
        "base_candidates": len(base),
        "stop_mode": stop_mode,
        "all": metrics(trades),
        "folds": parts,
        "by_year": {
            year: metrics(item for item in trades if str(item["date"]).startswith(year))
            for year in years
        },
        "risk_weighted_portfolio": risk_weighted_portfolio_metrics(trades, adaptive),
        "robustness": robustness_report(
            trades, days, adaptive, trade_config.round_trip_cost_bps, bootstrap_samples=2_000,
        ),
        "development_eligible": _eligible(parts),
        "trades": trades,
    }


def _symbol_tail_stress(
    trades: list[dict[str, Any]], config: AdaptiveHorizonConfig,
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in trades:
        grouped.setdefault(str(item["symbol"]), []).append(item)
    by_symbol = [
        {"symbol": symbol, **metrics(values)} for symbol, values in grouped.items()
    ]
    by_symbol.sort(key=lambda item: float(item["equal_weight_net_pct"]), reverse=True)
    leave_one_out = []
    for symbol in grouped:
        value = [item for item in trades if item["symbol"] != symbol]
        portfolio = risk_weighted_portfolio_metrics(value, config)
        leave_one_out.append({
            "excluded_symbol": symbol,
            "return_pct": portfolio["return_pct"],
            "max_drawdown_pct": portfolio["max_drawdown_pct"],
        })
    leave_one_out.sort(key=lambda item: float(item["return_pct"]))
    ordered = sorted(
        range(len(trades)), key=lambda index: float(trades[index]["net_pct"]), reverse=True,
    )
    remove_winners = {}
    for count in (1, 3, 5, 10):
        excluded = set(ordered[:count])
        value = [item for index, item in enumerate(trades) if index not in excluded]
        remove_winners[str(count)] = {
            "trade_metrics": metrics(value),
            "risk_weighted_portfolio": risk_weighted_portfolio_metrics(value, config),
        }
    return {
        "by_symbol": by_symbol,
        "leave_one_symbol_out": leave_one_out,
        "worst_leave_one_symbol_return_pct": (
            leave_one_out[0]["return_pct"] if leave_one_out else None
        ),
        "remove_top_winners": remove_winners,
    }


def assess_symbol_challenger(
    trades: list[dict[str, Any]], days: list[str], config: AdaptiveHorizonConfig,
    trade_config: V4Config,
) -> dict[str, Any]:
    folds = chronological_proxy_folds(days)
    parts = {
        name: metrics(item for item in trades if item["date"] in target)
        for name, target in folds.items() if name != "warmup"
    }
    years = sorted({str(item["date"])[:4] for item in trades})
    return {
        "all": metrics(trades),
        "folds": parts,
        "by_year": {
            year: metrics(item for item in trades if str(item["date"]).startswith(year))
            for year in years
        },
        "risk_weighted_portfolio": risk_weighted_portfolio_metrics(trades, config),
        "robustness": robustness_report(
            trades, days, config, trade_config.round_trip_cost_bps, bootstrap_samples=5_000,
        ),
        "tail_stress": _symbol_tail_stress(trades, config),
        "all_folds_positive": all(
            int(value["trades"]) > 0 and float(value["equal_weight_net_pct"]) > 0.0
            for value in parts.values()
        ),
        "minimum_fold_trades": min((int(value["trades"]) for value in parts.values()), default=0),
        "trades": trades,
    }


def research(market: dict[str, dict[str, list[dict[str, float]]]]) -> dict[str, Any]:
    days = sorted(day for day, rows in market.items() if "SPY" in rows and "QQQ" in rows)[-500:]
    opening_tables = {variant: build_proxy_rows(market, variant) for variant in VARIANTS}
    results = {variant: assess(opening_tables[variant], variant, days) for variant in VARIANTS}
    outcome_diagnostics = {
        variant: {
            mode: assess(opening_tables[variant], variant, days, mode)
            for mode in ("fixed_300bp", "hour_close_dynamic", "hour_close_300bp", "close_only")
        }
        for variant in VARIANTS
    }
    symbol_gate = SymbolEdgeGateConfig()
    symbol_adaptive = AdaptiveHorizonConfig(horizons_minutes=HORIZONS)
    symbol_trade_config = replace(V4Config(), skip_macro_days=False, skip_earnings_days=False)
    symbol_base = base_candidates(
        opening_tables["open_prior_continuation"],
        "open_prior_continuation",
        symbol_trade_config,
        "hour_close_300bp",
    )
    symbol_trades = causal_symbol_edge_trades(
        symbol_base, days, symbol_adaptive, symbol_gate,
    )
    symbol_challenger = assess_symbol_challenger(
        symbol_trades, days, symbol_adaptive, symbol_trade_config,
    )
    eligible = [name for name, value in results.items() if value["development_eligible"]]

    def score(name: str) -> tuple[float, float]:
        parts = [results[name]["folds"][fold] for fold in SELECTION_FOLDS]
        total = sum(int(part["trades"]) for part in parts)
        return (
            min(float(part["expectancy_pct_per_trade"]) for part in parts),
            sum(float(part["equal_weight_net_pct"]) for part in parts) / total if total else float("-inf"),
        )

    selected = max(eligible, key=score, default=None)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": "Yahoo Finance 60m US-stock proxy; adjusted hourly cache",
        "sessions": {"count": len(days), "start": days[0], "end": days[-1]},
        "universe": list(SYMBOLS),
        "rule_scope": (
            "Tests cross-sectional overnight relative-gap fade and the predeclared continuation mirror, "
            "with prior-day or first-hour confirmation. "
            "Not an OKX fill/spread validation."
        ),
        "selection_folds": list(SELECTION_FOLDS),
        "latest_diagnostic_excluded_from_selection": True,
        "results": results,
        "outcome_diagnostics": outcome_diagnostics,
        "symbol_conditioned_challenger": {
            "strategy": "open_prior_continuation_hour_close_300bp",
            "gate": asdict(symbol_gate),
            "direction_pooling": (
                "Symbol history pools LONG and SHORT signals; current side and horizon remain causal."
            ),
            "retrospective_choice_after_inspecting_proxy_grid": True,
            "forward_eligible": False,
            **symbol_challenger,
        },
        "selected_by_development_folds": selected,
        "proxy_hypothesis_supported": bool(
            selected and results[selected]["folds"]["latest_diagnostic"]["equal_weight_net_pct"] > 0
            and float(results[selected]["folds"]["latest_diagnostic"]["profit_factor"] or 0) >= 1.0
        ),
        "promotion_passed": False,
        "warning": (
            "Current-constituent survivorship bias and hourly-bar ambiguity remain. "
            "Use this only as independent price-pattern evidence; OKX forward shadow still controls promotion."
        ),
    }


def run() -> dict[str, Any]:
    return research(sessions(download()))


def main() -> None:
    report = run()
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    compact = {
        "sessions": report["sessions"],
        "selected_by_development_folds": report["selected_by_development_folds"],
        "proxy_hypothesis_supported": report["proxy_hypothesis_supported"],
        "results": {
            name: {
                "all": value["all"], "folds": value["folds"], "by_year": value["by_year"],
                "risk_weighted_return_pct": value["risk_weighted_portfolio"]["return_pct"],
                "risk_weighted_max_drawdown_pct": value["risk_weighted_portfolio"]["max_drawdown_pct"],
                "development_eligible": value["development_eligible"],
            }
            for name, value in report["results"].items()
        },
        "outcome_diagnostics": {
            variant: {
                mode: {
                    "all": value["all"], "folds": value["folds"], "by_year": value["by_year"],
                    "risk_weighted_return_pct": value["risk_weighted_portfolio"]["return_pct"],
                    "risk_weighted_max_drawdown_pct": value["risk_weighted_portfolio"]["max_drawdown_pct"],
                }
                for mode, value in modes.items()
            }
            for variant, modes in report["outcome_diagnostics"].items()
        },
        "symbol_conditioned_challenger": {
            key: report["symbol_conditioned_challenger"][key]
            for key in (
                "strategy", "gate", "retrospective_choice_after_inspecting_proxy_grid",
                "forward_eligible", "all", "folds", "by_year", "risk_weighted_portfolio",
                "robustness", "tail_stress", "all_folds_positive", "minimum_fold_trades",
            )
        },
        "promotion_passed": report["promotion_passed"],
        "warning": report["warning"],
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
