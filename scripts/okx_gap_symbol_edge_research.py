#!/usr/bin/env python3
"""Test causal, hierarchically-shrunk symbol quality on frozen V5 candidates.

The experiment keeps V5's entry rule, side-specific adaptive horizon, stops,
costs, and risk sizing.  Before each session, a symbol's prior-40-session
outcomes are shrunk toward the prior-20 global side/horizon expectancy.  The
current session is never included in its own score.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_gap_strategy_v3 import build_features, metrics  # noqa: E402
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
    side_horizon_choices,
)
from scripts.okx_gap_confidence_sizing_research import portfolio_metrics as sizing_portfolio_metrics  # noqa: E402
from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import load_market_data, weekday_sessions  # noqa: E402
from scripts.okx_research_universe import load_symbols  # noqa: E402

UTC = timezone.utc
OUTPUT = ROOT / "data" / "okx_gap_symbol_edge_research.json"


def _development_eligible(parts: dict[str, dict[str, Any]]) -> bool:
    return all(
        int(parts[name]["trades"]) >= 15
        and float(parts[name]["equal_weight_net_pct"]) > 0.0
        and float(parts[name]["profit_factor"] or 0.0) >= 1.10
        for name in SELECTION_FOLDS
    )


def causal_symbol_edge_trades(
    base: pd.DataFrame,
    days: list[str],
    config: AdaptiveHorizonConfig,
    *,
    symbol_lookback_sessions: int = 40,
    prior_strength: float = 5.0,
    require_positive_edge: bool,
    rank_by_edge: bool,
) -> list[dict[str, Any]]:
    """Score current candidates from completed prior symbol outcomes only."""
    trades: list[dict[str, Any]] = []
    for index, day_name in enumerate(days):
        if index < config.lookback_sessions:
            continue
        side_days = set(days[index - config.lookback_sessions:index])
        symbol_days = set(days[max(0, index - symbol_lookback_sessions):index])
        choices = side_horizon_choices(base[base.date.isin(side_days)], config)
        current = base[base.date == day_name]
        scored: list[dict[str, Any]] = []
        for _, row in current.iterrows():
            side = str(row.side)
            if side not in choices:
                continue
            choice = choices[side]
            horizon = int(choice["horizon_minutes"])
            prior = base[
                (base.symbol == row.symbol)
                & base.date.isin(symbol_days)
            ]
            symbol_values = prior[f"net_{horizon}"].astype(float).tolist()
            global_edge = float(choice[f"edge_{horizon}_pct"])
            shrunk_edge = (
                sum(symbol_values) + prior_strength * global_edge
            ) / (len(symbol_values) + prior_strength)
            if require_positive_edge and shrunk_edge <= 0.0:
                continue
            scored.append({
                "date": str(row.date),
                "symbol": str(row.symbol),
                "entry_time": int(row.entry_time),
                "exit_time": int(row.entry_time + horizon * 60_000),
                "side": side,
                "relative_gap_bps": round(float(row.relative_gap), 2),
                "horizon_minutes": horizon,
                "prior_side_samples": int(choice["prior_side_samples"]),
                "prior_best_horizon_expectancy_pct": float(
                    choice["prior_best_horizon_expectancy_pct"]
                ),
                "prior_symbol_samples": len(symbol_values),
                "prior_symbol_expectancy_pct": (
                    round(sum(symbol_values) / len(symbol_values), 6)
                    if symbol_values else None
                ),
                "shrunk_symbol_edge_pct": round(shrunk_edge, 6),
                "stop_bps": round(float(row.get("stop_bps") or 0.0), 3),
                "net_pct": round(float(row[f"net_{horizon}"]), 6),
                "exit_reason": str(row[f"reason_{horizon}"]),
            })
        scored.sort(
            key=(
                (lambda item: (float(item["shrunk_symbol_edge_pct"]), abs(float(item["relative_gap_bps"]))))
                if rank_by_edge else
                (lambda item: (abs(float(item["relative_gap_bps"])), float(item["shrunk_symbol_edge_pct"])))
            ),
            reverse=True,
        )
        trades.extend(scored[:config.max_positions])
    return trades


def _assess(
    trades: list[dict[str, Any]], days: list[str], config: AdaptiveHorizonConfig,
    trade_config: V4Config,
) -> dict[str, Any]:
    folds = chronological_folds(days)
    parts = {
        name: metrics(item for item in trades if item["date"] in target)
        for name, target in folds.items() if name != "warmup"
    }
    recent_month = set(days[-20:])
    recent_week = set(days[-5:])
    return {
        "all": metrics(trades),
        "folds": parts,
        "recent_month": metrics(item for item in trades if item["date"] in recent_month),
        "recent_week": metrics(item for item in trades if item["date"] in recent_week),
        "risk_weighted_portfolio": risk_weighted_portfolio_metrics(trades, config),
        "robustness": robustness_report(
            trades, days, config, trade_config.round_trip_cost_bps, bootstrap_samples=2_000,
        ),
        "development_eligible": _development_eligible(parts),
        "trades": trades,
    }


def _selection_score(value: dict[str, Any]) -> tuple[float, float]:
    parts = [value["folds"][name] for name in SELECTION_FOLDS]
    total = sum(int(part["trades"]) for part in parts)
    return (
        min(float(part["expectancy_pct_per_trade"]) for part in parts),
        sum(float(part["equal_weight_net_pct"]) for part in parts) / total if total else float("-inf"),
    )


def _sizing_assess(
    trades: list[dict[str, Any]], days: list[str], *, weighted: bool,
) -> dict[str, Any]:
    config = AdaptiveHorizonConfig()
    folds = chronological_folds(days)
    recent_month = set(days[-20:])
    value = sizing_portfolio_metrics(
        trades, config, confidence_weighted=weighted,
        confidence_field="shrunk_symbol_edge_pct",
    )
    return {
        "all": value,
        "folds": {
            name: sizing_portfolio_metrics(
                [item for item in trades if item["date"] in target],
                config, confidence_weighted=weighted,
                confidence_field="shrunk_symbol_edge_pct",
            )
            for name, target in folds.items() if name != "warmup"
        },
        "recent_month": sizing_portfolio_metrics(
            [item for item in trades if item["date"] in recent_month],
            config, confidence_weighted=weighted,
            confidence_field="shrunk_symbol_edge_pct",
        ),
        "gross_match_max_abs_pct_points": round(max(
            (
                abs(float(item["gross_allocation_pct"]) - float(item["baseline_gross_allocation_pct"]))
                for item in value["daily"]
            ),
            default=0.0,
        ), 10),
        "cost_sensitivity": {
            str(cost_bps): {
                key: adjusted[key]
                for key in ("return_pct", "max_drawdown_pct")
            }
            for cost_bps in (14, 25, 40, 50)
            for adjusted in [sizing_portfolio_metrics(
                [
                    {
                        **item,
                        "net_pct": float(item["net_pct"]) - (cost_bps - 14.0) / 100.0,
                    }
                    for item in trades
                ],
                config, confidence_weighted=weighted,
                confidence_field="shrunk_symbol_edge_pct",
            )]
        },
    }


def tail_robustness(
    trades: list[dict[str, Any]], config: AdaptiveHorizonConfig,
) -> dict[str, Any]:
    """Stress positive-tail and single-symbol concentration without refitting."""
    winners = sorted(
        range(len(trades)), key=lambda index: float(trades[index]["net_pct"]), reverse=True,
    )
    remove_top: dict[str, Any] = {}
    for count in (1, 3, 5, 10):
        excluded = set(winners[:count])
        value = [item for index, item in enumerate(trades) if index not in excluded]
        remove_top[str(count)] = {
            "trade_metrics": metrics(value),
            "risk_weighted_portfolio": risk_weighted_portfolio_metrics(value, config),
        }
    capped: dict[str, Any] = {}
    for cap_pct in (3.0, 5.0):
        value = [{**item, "net_pct": min(float(item["net_pct"]), cap_pct)} for item in trades]
        capped[str(cap_pct)] = {
            "trade_metrics": metrics(value),
            "risk_weighted_portfolio": risk_weighted_portfolio_metrics(value, config),
        }
    symbols = sorted({str(item["symbol"]) for item in trades})
    leave_one_symbol_out = []
    for symbol in symbols:
        value = [item for item in trades if item["symbol"] != symbol]
        portfolio = risk_weighted_portfolio_metrics(value, config)
        leave_one_symbol_out.append({
            "excluded_symbol": symbol,
            "trades": len(value),
            "return_pct": portfolio["return_pct"],
            "max_drawdown_pct": portfolio["max_drawdown_pct"],
        })
    leave_one_symbol_out.sort(key=lambda item: float(item["return_pct"]))
    return {
        "remove_top_winners": remove_top,
        "cap_single_trade_profit_pct": capped,
        "leave_one_symbol_out": leave_one_symbol_out,
        "worst_leave_one_symbol_return_pct": (
            leave_one_symbol_out[0]["return_pct"] if leave_one_symbol_out else None
        ),
    }


def _symbol_concentration(trades: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[float]] = {}
    for item in trades:
        grouped.setdefault(str(item["symbol"]), []).append(float(item["net_pct"]))
    rows = [
        {
            "symbol": symbol,
            "trades": len(values),
            "net_pct": round(sum(values), 4),
            "win_rate_pct": round(sum(value > 0 for value in values) / len(values) * 100.0, 2),
        }
        for symbol, values in grouped.items()
    ]
    rows.sort(key=lambda item: float(item["net_pct"]), reverse=True)
    total = sum(float(item["net_pct"]) for item in trades)
    winners = sorted(
        (float(item["net_pct"]) for item in trades if float(item["net_pct"]) > 0.0),
        reverse=True,
    )
    return {
        "by_symbol": rows,
        "top_1_winner_share_of_net_pct": round(winners[0] / total * 100.0, 2) if winners and total else None,
        "top_3_winner_share_of_net_pct": round(sum(winners[:3]) / total * 100.0, 2) if total else None,
        "top_10_winner_share_of_net_pct": round(sum(winners[:10]) / total * 100.0, 2) if total else None,
    }


def research(rows: pd.DataFrame) -> dict[str, Any]:
    days = sorted(str(value) for value in rows.date.unique())
    trade_config = replace(V4Config(), skip_macro_days=False, skip_earnings_days=False)
    adaptive = AdaptiveHorizonConfig()
    base = add_horizon_outcomes(label_base_candidates(rows, trade_config), trade_config)
    variants = {
        "frozen_v5_gap_rank": causal_adaptive_horizon_trades(base, days, adaptive),
        "positive_symbol_edge_gap_rank": causal_symbol_edge_trades(
            base, days, adaptive, require_positive_edge=True, rank_by_edge=False,
        ),
        "positive_symbol_edge_edge_rank": causal_symbol_edge_trades(
            base, days, adaptive, require_positive_edge=True, rank_by_edge=True,
        ),
    }
    edge_scored_baseline = causal_symbol_edge_trades(
        base, days, adaptive, require_positive_edge=False, rank_by_edge=False,
    )
    results = {
        name: _assess(trades, days, adaptive, trade_config)
        for name, trades in variants.items()
    }
    eligible = [name for name, value in results.items() if value["development_eligible"]]
    selected = max(eligible, key=lambda name: _selection_score(results[name]), default=None)
    baseline = results["frozen_v5_gap_rank"]
    challenger_passed = False
    challenger_name = None
    for name in ("positive_symbol_edge_gap_rank", "positive_symbol_edge_edge_rank"):
        value = results[name]
        improves_each_fold = all(
            float(value["folds"][fold]["expectancy_pct_per_trade"])
            > float(baseline["folds"][fold]["expectancy_pct_per_trade"])
            for fold in SELECTION_FOLDS
        )
        improves_portfolio = (
            float(value["risk_weighted_portfolio"]["return_pct"])
            > float(baseline["risk_weighted_portfolio"]["return_pct"])
            and float(value["risk_weighted_portfolio"]["max_drawdown_pct"])
            <= float(baseline["risk_weighted_portfolio"]["max_drawdown_pct"])
        )
        if value["development_eligible"] and improves_each_fold and improves_portfolio:
            if challenger_name is None or _selection_score(value) > _selection_score(results[challenger_name]):
                challenger_name = name
                challenger_passed = True
    sizing = {
        "baseline_inverse_stop": _sizing_assess(edge_scored_baseline, days, weighted=False),
        "same_gross_symbol_edge_weighted": _sizing_assess(edge_scored_baseline, days, weighted=True),
    }
    sizing_baseline = sizing["baseline_inverse_stop"]
    sizing_challenger = sizing["same_gross_symbol_edge_weighted"]
    sizing_passed = bool(
        all(
            float(sizing_challenger["folds"][fold]["return_pct"])
            > float(sizing_baseline["folds"][fold]["return_pct"])
            for fold in SELECTION_FOLDS
        )
        and float(sizing_challenger["all"]["return_pct"])
        > float(sizing_baseline["all"]["return_pct"])
        and float(sizing_challenger["all"]["max_drawdown_pct"])
        <= float(sizing_baseline["all"]["max_drawdown_pct"])
        and float(sizing_challenger["gross_match_max_abs_pct_points"]) <= 1e-8
    )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "effective_sessions": {"count": len(days), "start": days[0], "end": days[-1]},
        "experiment": "causal_prior40_symbol_edge_shrunk_to_prior20_side_horizon_edge",
        "symbol_edge_config": {
            "lookback_sessions": 40, "prior_strength_equivalent_trades": 5,
            "requires_positive_shrunk_edge": True, "uses_current_session": False,
        },
        "trade_config": asdict(trade_config),
        "adaptive_config": asdict(adaptive),
        "selection_folds": list(SELECTION_FOLDS),
        "baseline_concentration": _symbol_concentration(variants["frozen_v5_gap_rank"]),
        "baseline_tail_robustness": tail_robustness(
            variants["frozen_v5_gap_rank"], adaptive,
        ),
        "results": results,
        "same_gross_symbol_edge_sizing": sizing,
        "selected_by_development_folds": selected,
        "strict_challenger_passed": challenger_passed,
        "strict_challenger_name": challenger_name,
        "strict_sizing_challenger_passed": sizing_passed,
        "forward_shadow_recommendation": (
            f"freeze_{challenger_name}_as_separate_shadow_challenger"
            if challenger_passed else "retain_frozen_v5_gap_rank"
        ),
        "promotion_passed": False,
        "warning": (
            "Retrospective symbol-selection research on an inspected cohort. "
            "Any challenger requires new forward sessions and spread/fill validation."
        ),
    }


def run(end: date | None = None, sessions_count: int = 100) -> dict[str, Any]:
    sessions = weekday_sessions(end or latest_completed_us_session(), sessions_count)
    symbols = load_symbols("historical_90d")
    raw = load_market_data(OKX(settings()), symbols, sessions)
    report = research(build_features(raw))
    report["requested_sessions"] = sessions_count
    report["universe_size"] = len(symbols)
    return report


def main() -> None:
    report = run()
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    compact = {
        "effective_sessions": report["effective_sessions"],
        "selected_by_development_folds": report["selected_by_development_folds"],
        "strict_challenger_passed": report["strict_challenger_passed"],
        "strict_challenger_name": report["strict_challenger_name"],
        "strict_sizing_challenger_passed": report["strict_sizing_challenger_passed"],
        "forward_shadow_recommendation": report["forward_shadow_recommendation"],
        "baseline_concentration": {
            key: report["baseline_concentration"][key]
            for key in (
                "top_1_winner_share_of_net_pct", "top_3_winner_share_of_net_pct",
                "top_10_winner_share_of_net_pct",
            )
        },
        "baseline_tail_robustness": {
            "worst_leave_one_symbol_return_pct": report["baseline_tail_robustness"]["worst_leave_one_symbol_return_pct"],
            "remove_top_winners": {
                count: {
                    "net_pct": value["trade_metrics"]["equal_weight_net_pct"],
                    "pf": value["trade_metrics"]["profit_factor"],
                    "risk_return_pct": value["risk_weighted_portfolio"]["return_pct"],
                    "risk_max_drawdown_pct": value["risk_weighted_portfolio"]["max_drawdown_pct"],
                }
                for count, value in report["baseline_tail_robustness"]["remove_top_winners"].items()
            },
            "cap_single_trade_profit_pct": {
                cap: {
                    "net_pct": value["trade_metrics"]["equal_weight_net_pct"],
                    "pf": value["trade_metrics"]["profit_factor"],
                    "risk_return_pct": value["risk_weighted_portfolio"]["return_pct"],
                }
                for cap, value in report["baseline_tail_robustness"]["cap_single_trade_profit_pct"].items()
            },
        },
        "results": {
            name: {
                "all": value["all"], "folds": value["folds"],
                "recent_month": value["recent_month"],
                "risk_weighted_return_pct": value["risk_weighted_portfolio"]["return_pct"],
                "risk_weighted_max_drawdown_pct": value["risk_weighted_portfolio"]["max_drawdown_pct"],
                "development_eligible": value["development_eligible"],
            }
            for name, value in report["results"].items()
        },
        "same_gross_symbol_edge_sizing": {
            name: {
                "all": {
                    key: value["all"][key]
                    for key in ("return_pct", "max_drawdown_pct")
                },
                "folds": {
                    fold: {
                        key: part[key]
                        for key in ("return_pct", "max_drawdown_pct")
                    }
                    for fold, part in value["folds"].items()
                },
                "recent_month": {
                    key: value["recent_month"][key]
                    for key in ("return_pct", "max_drawdown_pct")
                },
                "gross_match_max_abs_pct_points": value["gross_match_max_abs_pct_points"],
                "cost_sensitivity": value["cost_sensitivity"],
            }
            for name, value in report["same_gross_symbol_edge_sizing"].items()
        },
        "promotion_passed": report["promotion_passed"],
        "warning": report["warning"],
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
