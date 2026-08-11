#!/usr/bin/env python3
"""Frozen no-backfill confirmation overlay for the retrospective V5 report.

This artifact deliberately starts from the exact trades already selected in
``okx_gap_strategy_v5_backtest.json``.  It never rebuilds the candidate pool,
refits the adaptive horizon, or fills a skipped slot with another symbol.  The
single predeclared predicate is evaluated from information completed at 09:35:

    relative_gap * relative_first5 < 0

The report is retrospective and can only open a new forward-shadow cohort.  It
is not an execution promotion artifact.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_gap_strategy_v3 import build_features, metrics  # noqa: E402
from scripts.okx_gap_strategy_v4 import chronological_folds  # noqa: E402
from scripts.okx_gap_strategy_v5 import (  # noqa: E402
    AdaptiveHorizonConfig,
    risk_weighted_portfolio_metrics,
)
from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import load_market_data, weekday_sessions  # noqa: E402
from scripts.okx_research_universe import load_symbols  # noqa: E402

UTC = timezone.utc
OUTPUT = ROOT / "data" / "okx_gap_confirmation_overlay_research.json"
SOURCE_V5_REPORT = ROOT / "data" / "okx_gap_strategy_v5_backtest.json"
SOURCE_V5_CODE = ROOT / "scripts" / "okx_gap_strategy_v5.py"
SOURCE_SHADOW_CODE = ROOT / "scripts" / "okx_gap_shadow.py"
SOURCE_LABELER_CODE = ROOT / "scripts" / "okx_shadow_labeler.py"
SOURCE_UNIVERSE = ROOT / "data" / "okx_research_universe.json"
FROZEN_AT = "2026-08-08T04:15:28+00:00"
EXPERIMENT_ID = "gap_v5_strict_first5_no_backfill_shadow_20260808"
COSTS_BPS = (14, 25, 40, 50, 100)
FORWARD_MIN_BASELINE_TRADES = 25
RULE: dict[str, Any] = {
    "source_stage": "after_frozen_v5_selection_and_ranking",
    "predicate": "relative_gap * relative_first5 < 0",
    "decision_time": "09:35 America/New_York after the 09:30-09:35 bar completes",
    "no_backfill": True,
    "adaptive_horizon_refit": False,
    "source_v5_paper_history_unchanged": True,
}


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stable_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _historical_universe_symbols() -> list[str]:
    """Return the stable strategy universe, excluding volatile quote metadata."""
    payload = json.loads(SOURCE_UNIVERSE.read_text())
    symbols = sorted(
        str(item["inst_id"])
        for item in payload.get("historical_90d", [])
    )
    if not symbols or len(symbols) != len(set(symbols)):
        raise ValueError("historical_90d universe must contain unique symbols")
    return symbols


def source_fingerprints() -> dict[str, Any]:
    """Bind the forward experiment to its immutable rule and local sources."""
    universe_symbols = _historical_universe_symbols()
    return {
        "frozen_rule_sha256": _stable_sha256(RULE),
        "overlay_code_sha256": _sha256_file(Path(__file__)),
        "source_v5_code_sha256": _sha256_file(SOURCE_V5_CODE),
        "forward_shadow_code_sha256": _sha256_file(SOURCE_SHADOW_CODE),
        "forward_labeler_code_sha256": _sha256_file(SOURCE_LABELER_CODE),
        "source_v5_report_sha256": _sha256_file(SOURCE_V5_REPORT),
        "source_universe_symbol_set_sha256": _stable_sha256(universe_symbols),
        "source_universe_symbols": universe_symbols,
        "source_universe_snapshot_sha256": _sha256_file(SOURCE_UNIVERSE),
        "source_paths": {
            "overlay_code": str(Path(__file__).relative_to(ROOT)),
            "source_v5_code": str(SOURCE_V5_CODE.relative_to(ROOT)),
            "forward_shadow_code": str(SOURCE_SHADOW_CODE.relative_to(ROOT)),
            "forward_labeler_code": str(SOURCE_LABELER_CODE.relative_to(ROOT)),
            "source_v5_report": str(SOURCE_V5_REPORT.relative_to(ROOT)),
            "source_universe": str(SOURCE_UNIVERSE.relative_to(ROOT)),
        },
    }


def confirmation_overlay_trades(
    frozen_trades: Iterable[dict[str, Any]],
    feature_rows: pd.DataFrame,
) -> list[dict[str, Any]]:
    """Filter only frozen trades; never rank or introduce a replacement row."""
    required = {"date", "symbol", "relative_gap", "relative_first5"}
    missing = required.difference(feature_rows.columns)
    if missing:
        raise ValueError(f"feature rows missing columns: {sorted(missing)}")
    keys = ["date", "symbol"]
    if feature_rows.duplicated(keys).any():
        raise ValueError("feature rows contain duplicate date/symbol keys")
    indexed = feature_rows.set_index(keys)
    result: list[dict[str, Any]] = []
    for source in frozen_trades:
        key = (str(source["date"]), str(source["symbol"]))
        if key not in indexed.index:
            raise ValueError(f"frozen V5 trade has no matching causal features: {key}")
        row = indexed.loc[key]
        relative_gap = float(row.relative_gap)
        relative_first5 = float(row.relative_first5)
        if relative_gap * relative_first5 >= 0.0:
            continue
        item = copy.deepcopy(source)
        item["overlay_relative_gap_bps"] = round(relative_gap, 6)
        item["overlay_relative_first5_bps"] = round(relative_first5, 6)
        item["overlay_confirmation"] = "strict_first5_reversal"
        result.append(item)
    return result


def _adjusted_cost_trades(
    trades: Iterable[dict[str, Any]],
    cost_bps: float,
    base_cost_bps: float = 14.0,
) -> list[dict[str, Any]]:
    incremental_pct = (float(cost_bps) - float(base_cost_bps)) / 100.0
    return [
        {**item, "net_pct": float(item["net_pct"]) - incremental_pct}
        for item in trades
    ]


def cost_sensitivity(
    trades: list[dict[str, Any]],
    config: AdaptiveHorizonConfig,
) -> dict[str, Any]:
    return {
        str(cost): {
            "trade_metrics": metrics(adjusted),
            "risk_weighted_portfolio": risk_weighted_portfolio_metrics(adjusted, config),
        }
        for cost in COSTS_BPS
        for adjusted in [_adjusted_cost_trades(trades, cost)]
    }


def symbol_concentration(trades: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in trades:
        grouped.setdefault(str(item["symbol"]), []).append(item)
    rows = [
        {"symbol": symbol, **metrics(values)}
        for symbol, values in grouped.items()
    ]
    rows.sort(key=lambda item: float(item["equal_weight_net_pct"]), reverse=True)
    total = sum(float(item["net_pct"]) for item in trades)
    positive = [item for item in rows if float(item["equal_weight_net_pct"]) > 0.0]
    return {
        "by_symbol": rows,
        "best_symbol": rows[0]["symbol"] if rows else None,
        "best_symbol_share_of_total_net_pct": (
            round(float(rows[0]["equal_weight_net_pct"]) / total * 100.0, 2)
            if rows and total else None
        ),
        "top_3_positive_symbols_share_of_total_net_pct": (
            round(sum(float(item["equal_weight_net_pct"]) for item in positive[:3]) / total * 100.0, 2)
            if total else None
        ),
    }


def _stress_case(
    trades: list[dict[str, Any]],
    config: AdaptiveHorizonConfig,
) -> dict[str, Any]:
    return {
        "trade_metrics": metrics(trades),
        "risk_weighted_portfolio": risk_weighted_portfolio_metrics(trades, config),
    }


def tail_robustness(
    trades: list[dict[str, Any]],
    days: list[str],
    config: AdaptiveHorizonConfig,
) -> dict[str, Any]:
    """Run fixed concentration stresses; results never feed rule selection."""
    folds = chronological_folds(days)
    latest_days = folds["latest_unseen"]
    if not trades:
        empty = _stress_case([], config)
        return {
            "remove_best_symbol": {"excluded_symbol": None, **empty},
            "remove_best_trade": {"excluded_trade": None, **empty},
            "remove_latest_fold": {"excluded_dates": sorted(latest_days), **empty},
        }

    by_symbol: dict[str, float] = {}
    for item in trades:
        by_symbol[str(item["symbol"])] = by_symbol.get(str(item["symbol"]), 0.0) + float(item["net_pct"])
    best_symbol = max(by_symbol, key=by_symbol.get)
    best_index = max(range(len(trades)), key=lambda index: float(trades[index]["net_pct"]))
    best_trade = trades[best_index]
    return {
        "remove_best_symbol": {
            "excluded_symbol": best_symbol,
            **_stress_case([item for item in trades if item["symbol"] != best_symbol], config),
        },
        "remove_best_trade": {
            "excluded_trade": {
                key: best_trade.get(key)
                for key in ("date", "symbol", "side", "net_pct", "horizon_minutes")
            },
            **_stress_case([item for index, item in enumerate(trades) if index != best_index], config),
        },
        "remove_latest_fold": {
            "excluded_dates": sorted(latest_days),
            **_stress_case([item for item in trades if item["date"] not in latest_days], config),
        },
    }


def _fold_definition(days: list[str]) -> dict[str, Any]:
    return {
        name: {
            "sessions": len(target),
            "start": min(target) if target else None,
            "end": max(target) if target else None,
        }
        for name, target in chronological_folds(days).items()
    }


def assess_trades(
    trades: list[dict[str, Any]],
    days: list[str],
    config: AdaptiveHorizonConfig | None = None,
) -> dict[str, Any]:
    config = config or AdaptiveHorizonConfig()
    folds = chronological_folds(days)
    fold_trades = {
        name: [item for item in trades if item["date"] in target]
        for name, target in folds.items() if name != "warmup"
    }
    return {
        "all": metrics(trades),
        "folds": {name: metrics(values) for name, values in fold_trades.items()},
        "risk_weighted_portfolio": {
            "all": risk_weighted_portfolio_metrics(trades, config),
            "folds": {
                name: risk_weighted_portfolio_metrics(values, config)
                for name, values in fold_trades.items()
            },
        },
        "cost_sensitivity": cost_sensitivity(trades, config),
        "symbol_concentration": symbol_concentration(trades),
        "tail_robustness": tail_robustness(trades, days, config),
        "trades": trades,
    }


def _comparison(baseline: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    def delta(left: Any, right: Any) -> float:
        return round(float(right or 0.0) - float(left or 0.0), 6)

    fold_names = baseline["folds"].keys()
    return {
        "all": {
            "trades_removed": int(baseline["all"]["trades"]) - int(overlay["all"]["trades"]),
            "expectancy_delta_pct": delta(
                baseline["all"]["expectancy_pct_per_trade"], overlay["all"]["expectancy_pct_per_trade"],
            ),
            "profit_factor_delta": delta(
                baseline["all"]["profit_factor"], overlay["all"]["profit_factor"],
            ),
            "risk_return_delta_pct": delta(
                baseline["risk_weighted_portfolio"]["all"]["return_pct"],
                overlay["risk_weighted_portfolio"]["all"]["return_pct"],
            ),
            "risk_drawdown_delta_pct": delta(
                baseline["risk_weighted_portfolio"]["all"]["max_drawdown_pct"],
                overlay["risk_weighted_portfolio"]["all"]["max_drawdown_pct"],
            ),
        },
        "folds": {
            name: {
                "trades_removed": int(baseline["folds"][name]["trades"])
                - int(overlay["folds"][name]["trades"]),
                "expectancy_delta_pct": delta(
                    baseline["folds"][name]["expectancy_pct_per_trade"],
                    overlay["folds"][name]["expectancy_pct_per_trade"],
                ),
                "profit_factor_delta": delta(
                    baseline["folds"][name]["profit_factor"],
                    overlay["folds"][name]["profit_factor"],
                ),
                "risk_return_delta_pct": delta(
                    baseline["risk_weighted_portfolio"]["folds"][name]["return_pct"],
                    overlay["risk_weighted_portfolio"]["folds"][name]["return_pct"],
                ),
                "risk_drawdown_delta_pct": delta(
                    baseline["risk_weighted_portfolio"]["folds"][name]["max_drawdown_pct"],
                    overlay["risk_weighted_portfolio"]["folds"][name]["max_drawdown_pct"],
                ),
            }
            for name in fold_names
        },
    }


def research(
    rows: pd.DataFrame,
    source_report: dict[str, Any],
    *,
    fingerprints: dict[str, Any] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    days = sorted(str(value) for value in rows.date.unique())
    effective = source_report.get("effective_sessions") or {}
    expected = (int(effective.get("count") or 0), effective.get("start"), effective.get("end"))
    observed = (len(days), days[0] if days else None, days[-1] if days else None)
    if observed != expected:
        raise ValueError(f"source V5 session mismatch: expected={expected}, observed={observed}")

    baseline_trades = copy.deepcopy(source_report.get("trades") or [])
    overlay_trades = confirmation_overlay_trades(baseline_trades, rows)
    baseline_ids = {
        (item["date"], item["symbol"], item["entry_time"], item["horizon_minutes"])
        for item in baseline_trades
    }
    overlay_ids = {
        (item["date"], item["symbol"], item["entry_time"], item["horizon_minutes"])
        for item in overlay_trades
    }
    if not overlay_ids.issubset(baseline_ids):
        raise AssertionError("overlay introduced a trade outside frozen V5")

    config = AdaptiveHorizonConfig()
    baseline = assess_trades(baseline_trades, days, config)
    overlay = assess_trades(overlay_trades, days, config)
    return {
        "experiment_id": EXPERIMENT_ID,
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "frozen_at": FROZEN_AT,
        "source_v5_generated_at": source_report.get("generated_at"),
        "source_v5_warning": source_report.get("warning"),
        "rule": RULE,
        "fingerprints": fingerprints or source_fingerprints(),
        "effective_sessions": effective,
        "fixed_folds": _fold_definition(days),
        "invariants": {
            "source_trade_count": len(baseline_trades),
            "overlay_trade_count": len(overlay_trades),
            "overlay_is_exact_subset_of_frozen_v5": True,
            "replacement_trades": 0,
            "adaptive_horizon_refit": False,
            "exact_stop_fill_time_claimed": False,
            "stop_exit_time_resolution": "end of first triggering completed 5m path bar",
        },
        "baseline": baseline,
        "overlay": overlay,
        "comparison": _comparison(baseline, overlay),
        "promotion_passed": False,
        "promotion_blockers": [
            "source_v5_and_all_overlay_dates_are_retrospective_and_previously_inspected",
            f"requires_at_least_{FORWARD_MIN_BASELINE_TRADES}_new_paired_forward_baseline_trades_after_frozen_at",
            "requires_forward_overlay_to_improve_expectancy_pf_and_risk_drawdown_without_refit",
            "requires_historical_or_forward_l2_spread_slippage_and_fill_validation",
            "tail_and_symbol_concentration_stresses_are_diagnostics_not_rule_tuning_inputs",
        ],
    }


def run() -> dict[str, Any]:
    source_report = json.loads(SOURCE_V5_REPORT.read_text())
    end = date.fromisoformat(source_report["effective_sessions"]["end"])
    sessions = weekday_sessions(end, int(source_report.get("requested_sessions") or 100))
    symbols = load_symbols("historical_90d")
    if len(symbols) != int(source_report.get("universe_size") or 0):
        raise ValueError("current historical universe does not match frozen V5 report")
    raw = load_market_data(OKX(settings()), symbols, sessions)
    return research(build_features(raw), source_report)


def main() -> None:
    report = run()
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    compact = {
        "experiment_id": report["experiment_id"],
        "generated_at": report["generated_at"],
        "frozen_at": report["frozen_at"],
        "rule": report["rule"],
        "fingerprints": report["fingerprints"],
        "baseline": {
            "all": report["baseline"]["all"],
            "risk_weighted_portfolio": {
                key: report["baseline"]["risk_weighted_portfolio"]["all"][key]
                for key in ("return_pct", "max_drawdown_pct")
            },
        },
        "overlay": {
            "all": report["overlay"]["all"],
            "risk_weighted_portfolio": {
                key: report["overlay"]["risk_weighted_portfolio"]["all"][key]
                for key in ("return_pct", "max_drawdown_pct")
            },
        },
        "comparison": report["comparison"],
        "promotion_passed": report["promotion_passed"],
        "promotion_blockers": report["promotion_blockers"],
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
