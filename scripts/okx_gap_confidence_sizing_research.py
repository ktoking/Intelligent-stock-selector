#!/usr/bin/env python3
"""Reallocate frozen V5 daily gross exposure using causal edge confidence.

The signal set, stops, costs, horizons, 20% per-position cap, and each day's
total gross allocation remain unchanged.  Only the split between same-day
positions changes, using the prior-side horizon expectancy already available
before entry.  The experiment therefore cannot win merely by adding leverage.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_gap_strategy_v4 import SELECTION_FOLDS, chronological_folds  # noqa: E402
from scripts.okx_gap_strategy_v5 import AdaptiveHorizonConfig  # noqa: E402

UTC = timezone.utc
SOURCE = ROOT / "data" / "okx_gap_entry_delay_research.json"
OUTPUT = ROOT / "data" / "okx_gap_confidence_sizing_research.json"


def capped_normalized_weights(
    raw: list[float], target_gross: float, position_cap: float,
) -> list[float]:
    """Normalize non-negative scores to a target while respecting a hard cap."""
    if not raw or target_gross <= 0.0 or position_cap <= 0.0:
        return [0.0] * len(raw)
    target = min(float(target_gross), len(raw) * float(position_cap))
    weights = [0.0] * len(raw)
    active = {index for index, value in enumerate(raw) if value > 0.0}
    remaining = target
    while active and remaining > 1e-12:
        total_score = sum(float(raw[index]) for index in active)
        if total_score <= 0.0:
            break
        capped: list[int] = []
        for index in active:
            proposed = remaining * float(raw[index]) / total_score
            room = position_cap - weights[index]
            if proposed >= room - 1e-12:
                weights[index] += max(0.0, room)
                capped.append(index)
        if not capped:
            for index in active:
                weights[index] += remaining * float(raw[index]) / total_score
            remaining = 0.0
            break
        for index in capped:
            active.remove(index)
        remaining = target - sum(weights)
    return weights


def confidence_multiplier(prior_expectancy_pct: float) -> float:
    """Monotonic, bounded multiplier; 0% maps to one and +/-0.5% to caps."""
    return max(0.5, min(1.5, 1.0 + float(prior_expectancy_pct)))


def _baseline_weights(
    trades: list[dict[str, Any]], config: AdaptiveHorizonConfig,
) -> list[float]:
    desired = [
        min(
            config.max_position_equity_fraction,
            config.risk_fraction / max(float(item.get("stop_bps") or 0.0) / 10_000.0, 1e-9),
        )
        for item in trades
    ]
    scale = min(1.0, config.max_gross_equity_fraction / sum(desired)) if sum(desired) else 0.0
    return [value * scale for value in desired]


def portfolio_metrics(
    trades: list[dict[str, Any]],
    config: AdaptiveHorizonConfig,
    *,
    confidence_weighted: bool,
    confidence_field: str = "prior_best_horizon_expectancy_pct",
    initial_equity: float = 10_000.0,
) -> dict[str, Any]:
    equity = peak = float(initial_equity)
    max_drawdown = 0.0
    daily: list[dict[str, Any]] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        grouped.setdefault(str(trade["date"]), []).append(trade)
    for day_name in sorted(grouped):
        current = grouped[day_name]
        baseline = _baseline_weights(current, config)
        if confidence_weighted:
            raw = [
                weight * confidence_multiplier(float(item[confidence_field]))
                for weight, item in zip(baseline, current)
            ]
            weights = capped_normalized_weights(
                raw, sum(baseline), config.max_position_equity_fraction,
            )
        else:
            weights = baseline
        start = equity
        day_return = sum(
            weight * float(item["net_pct"]) / 100.0
            for weight, item in zip(weights, current)
        )
        equity *= 1.0 + day_return
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak * 100.0)
        daily.append({
            "date": day_name,
            "trades": len(current),
            "gross_allocation_pct": round(sum(weights) * 100.0, 8),
            "baseline_gross_allocation_pct": round(sum(baseline) * 100.0, 8),
            "allocation_pct_each": [round(weight * 100.0, 6) for weight in weights],
            "pnl": round(equity - start, 2),
            "equity": round(equity, 2),
        })
    return {
        "initial_equity": round(initial_equity, 2),
        "final_equity": round(equity, 2),
        "net_pnl": round(equity - initial_equity, 2),
        "return_pct": round((equity / initial_equity - 1.0) * 100.0, 3),
        "max_drawdown_pct": round(max_drawdown, 3),
        "daily": daily,
    }


def _variant_report(
    trades: list[dict[str, Any]], days: list[str], confidence_weighted: bool,
) -> dict[str, Any]:
    config = AdaptiveHorizonConfig()
    folds = chronological_folds(days)
    recent_month = set(days[-20:])
    recent_week = set(days[-5:])
    all_metrics = portfolio_metrics(
        trades, config, confidence_weighted=confidence_weighted,
    )
    return {
        "all": all_metrics,
        "folds": {
            name: portfolio_metrics(
                [item for item in trades if item["date"] in target],
                config,
                confidence_weighted=confidence_weighted,
            )
            for name, target in folds.items() if name != "warmup"
        },
        "recent_month": portfolio_metrics(
            [item for item in trades if item["date"] in recent_month],
            config,
            confidence_weighted=confidence_weighted,
        ),
        "recent_week": portfolio_metrics(
            [item for item in trades if item["date"] in recent_week],
            config,
            confidence_weighted=confidence_weighted,
        ),
        "gross_match_max_abs_pct_points": round(max(
            (
                abs(float(item["gross_allocation_pct"]) - float(item["baseline_gross_allocation_pct"]))
                for item in all_metrics["daily"]
            ),
            default=0.0,
        ), 10),
    }


def _cost_sensitivity(
    trades: list[dict[str, Any]], days: list[str], confidence_weighted: bool,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for cost_bps in (14, 25, 40, 50):
        extra_pct = (cost_bps - 14.0) / 100.0
        adjusted = [{**item, "net_pct": float(item["net_pct"]) - extra_pct} for item in trades]
        value = _variant_report(adjusted, days, confidence_weighted)["all"]
        result[str(cost_bps)] = {
            "return_pct": value["return_pct"],
            "max_drawdown_pct": value["max_drawdown_pct"],
        }
    return result


def research(source: dict[str, Any]) -> dict[str, Any]:
    frozen = source["results"]["5"]
    trades = list(frozen["trades"])
    days = list(frozen["effective_dates"])
    baseline = _variant_report(trades, days, False)
    confidence = _variant_report(trades, days, True)
    results = {
        "baseline_inverse_stop": {
            **baseline,
            "cost_sensitivity": _cost_sensitivity(trades, days, False),
        },
        "same_gross_confidence_weighted": {
            **confidence,
            "cost_sensitivity": _cost_sensitivity(trades, days, True),
        },
    }
    improves_each_fold = all(
        float(confidence["folds"][fold]["return_pct"])
        > float(baseline["folds"][fold]["return_pct"])
        for fold in SELECTION_FOLDS
    )
    improves_full = (
        float(confidence["all"]["return_pct"]) > float(baseline["all"]["return_pct"])
        and float(confidence["all"]["max_drawdown_pct"])
        <= float(baseline["all"]["max_drawdown_pct"])
    )
    gross_match = float(confidence["gross_match_max_abs_pct_points"]) <= 1e-8
    passed = bool(improves_each_fold and improves_full and gross_match)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": str(SOURCE.relative_to(ROOT)),
        "experiment": "same_daily_gross_causal_prior_edge_reallocation",
        "adaptive_config": asdict(AdaptiveHorizonConfig()),
        "confidence_multiplier": "clip(1 + prior_best_horizon_expectancy_pct, 0.5, 1.5)",
        "selection_folds": list(SELECTION_FOLDS),
        "results": results,
        "strict_challenger_passed": passed,
        "forward_shadow_recommendation": (
            "freeze_confidence_sizing_as_shadow_challenger"
            if passed else "retain_frozen_v5_inverse_stop_sizing"
        ),
        "promotion_passed": False,
        "warning": (
            "Retrospective sizing research on an already inspected signal cohort; "
            "same gross exposure is enforced but forward validation is still required."
        ),
    }


def run(path: Path = SOURCE) -> dict[str, Any]:
    return research(json.loads(path.read_text()))


def main() -> None:
    report = run()
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    compact = {
        "strict_challenger_passed": report["strict_challenger_passed"],
        "forward_shadow_recommendation": report["forward_shadow_recommendation"],
        "results": {
            name: {
                "all": {
                    key: value["all"][key]
                    for key in ("final_equity", "net_pnl", "return_pct", "max_drawdown_pct")
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
            for name, value in report["results"].items()
        },
        "promotion_passed": report["promotion_passed"],
        "warning": report["warning"],
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
