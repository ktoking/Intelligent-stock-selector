#!/usr/bin/env python3
"""Replay frozen V5 entries with the mark-price stop trigger used in Demo.

V5 entry and horizon prices come from ordinary trade candles, while the actual
OKX protection order uses ``slTriggerPxType=mark``.  This parity audit fetches
the corresponding public five-minute mark-price candles, changes only the
stop-trigger path, and never reselects a trade, side, horizon or parameter.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_gap_strategy_v3 import build_features, metrics  # noqa: E402
from scripts.okx_gap_strategy_v5 import (  # noqa: E402
    AdaptiveHorizonConfig,
    risk_weighted_portfolio_metrics,
)
from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import load_market_data, weekday_sessions  # noqa: E402
from scripts.okx_research_universe import load_symbols  # noqa: E402

UTC = timezone.utc
OUTPUT = ROOT / "data" / "okx_gap_mark_stop_parity.json"
CACHE_DIR = ROOT / "data" / "okx_mark_stop_cache"
SOURCE_REPORT = ROOT / "data" / "okx_gap_strategy_v5_backtest.json"
SOURCE_V5_CODE = ROOT / "scripts" / "okx_gap_strategy_v5.py"
MARK_ENDPOINT = "/api/v5/market/history-mark-price-candles"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stable_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _key(trade: dict[str, Any]) -> tuple[str, str]:
    return str(trade["date"]), str(trade["symbol"])


def _cache_path(trade: dict[str, Any], cache_dir: Path) -> Path:
    day, symbol = _key(trade)
    return cache_dir / f"{day}__{symbol}.json"


def fetch_mark_path(client: OKX, trade: dict[str, Any], cache_dir: Path = CACHE_DIR) -> list[list[str]]:
    """Fetch and cache the exact mark bars covering one frozen trade."""
    path = _cache_path(trade, cache_dir)
    try:
        payload = json.loads(path.read_text())
        if payload.get("entry_time") == int(trade["entry_time"]):
            return list(payload.get("rows") or [])
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass

    horizon = int(trade["horizon_minutes"])
    end_ts = int(trade["entry_time"]) + horizon * 60_000
    rows = client.mark_price_candles_ending_at(
        str(trade["symbol"]), end_ts, limit=max(40, horizon // 5 + 10), bar="5m",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({
        "endpoint": MARK_ENDPOINT,
        "fetched_at": datetime.now(UTC).isoformat(),
        "date": trade["date"],
        "symbol": trade["symbol"],
        "entry_time": int(trade["entry_time"]),
        "horizon_minutes": horizon,
        "rows": rows,
    }, ensure_ascii=False, indent=2))
    temporary.replace(path)
    return rows


def normalized_mark_path(trade: dict[str, Any], rows: list[list[str]]) -> list[list[str]]:
    """Return the exact confirmed bars from entry through the frozen horizon."""
    entry = int(trade["entry_time"])
    horizon = int(trade["horizon_minutes"])
    expected = [entry + index * 300_000 for index in range(horizon // 5)]
    by_stamp = {
        int(row[0]): row
        for row in rows
        if len(row) >= 6 and str(row[5]) == "1"
    }
    return [by_stamp[stamp] for stamp in expected if stamp in by_stamp]


def replay_mark_stop(
    trade: dict[str, Any],
    feature_row: pd.Series,
    mark_rows: list[list[str]],
    *,
    round_trip_cost_bps: float,
) -> dict[str, Any] | None:
    path = normalized_mark_path(trade, mark_rows)
    horizon = int(trade["horizon_minutes"])
    if len(path) != horizon // 5:
        return None
    direction = 1 if trade["side"] == "LONG" else -1
    entry = float(feature_row.entry)
    stop_fraction = float(trade["stop_bps"]) / 10_000.0
    stop_price = entry * (1.0 - direction * stop_fraction)
    exit_bar: int | None = None
    for index, row in enumerate(path):
        high, low = float(row[2]), float(row[3])
        stop_hit = low <= stop_price if direction > 0 else high >= stop_price
        if stop_hit:
            exit_bar = index + 1
            break

    if exit_bar:
        exit_price = stop_price
        reason = "mark_price_atr_stop"
        basis = "first_triggering_mark_5m_bar_end"
    else:
        exit_price = float(feature_row[f"exit_{horizon}"])
        reason = "horizon"
        basis = "scheduled_horizon"
    net_pct = direction * (exit_price / entry - 1.0) * 100.0 - round_trip_cost_bps / 100.0
    result = copy.deepcopy(trade)
    result.update({
        "exit_time": int(trade["entry_time"]) + (exit_bar * 5 if exit_bar else horizon) * 60_000,
        "exit_time_basis": basis,
        "stop_bar_number": exit_bar,
        "net_pct": round(net_pct, 6),
        "exit_reason": reason,
        "label_start_time": int(trade["entry_time"]),
        "label_end_time": int(trade["entry_time"])
        + (exit_bar * 5 if exit_bar else horizon) * 60_000,
    })
    return result


def _assessment(trades: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "all": metrics(trades),
        "risk_weighted_portfolio": risk_weighted_portfolio_metrics(
            trades, AdaptiveHorizonConfig(),
        ),
        "trades": trades,
    }


def research(
    feature_rows: pd.DataFrame,
    source_report: dict[str, Any],
    mark_paths: dict[tuple[str, str], list[list[str]]],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if feature_rows.duplicated(["date", "symbol"]).any():
        raise ValueError("feature rows contain duplicate date/symbol keys")
    indexed = feature_rows.set_index(["date", "symbol"])
    frozen = copy.deepcopy(source_report.get("trades") or [])
    cost_bps = float((source_report.get("trade_filter") or {}).get("round_trip_cost_bps") or 14.0)
    covered_baseline: list[dict[str, Any]] = []
    mark_trades: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    normalized_sources: dict[str, list[list[str]]] = {}
    for trade in frozen:
        key = _key(trade)
        if key not in indexed.index:
            raise ValueError(f"frozen V5 trade has no matching feature row: {key}")
        rows = mark_paths.get(key) or []
        value = replay_mark_stop(
            trade, indexed.loc[key], rows, round_trip_cost_bps=cost_bps,
        )
        if value is None:
            missing.append({
                "date": key[0], "symbol": key[1],
                "expected_bars": int(trade["horizon_minutes"]) // 5,
                "available_bars": len(normalized_mark_path(trade, rows)),
            })
            continue
        covered_baseline.append(copy.deepcopy(trade))
        mark_trades.append(value)
        normalized_sources[f"{key[0]}::{key[1]}"] = normalized_mark_path(trade, rows)

    last_stop_rows = {
        _key(item): item for item in covered_baseline if item.get("exit_reason") == "atr_stop"
    }
    mark_stop_rows = {
        _key(item): item for item in mark_trades if item.get("exit_reason") == "mark_price_atr_stop"
    }
    last_stops, mark_stops = set(last_stop_rows), set(mark_stop_rows)
    both_stops = sorted(last_stops & mark_stops)
    timing_differences = [{
        "date": key[0],
        "symbol": key[1],
        "last_stop_bar_number": int(last_stop_rows[key]["stop_bar_number"]),
        "mark_stop_bar_number": int(mark_stop_rows[key]["stop_bar_number"]),
        "mark_minus_last_bars": (
            int(mark_stop_rows[key]["stop_bar_number"])
            - int(last_stop_rows[key]["stop_bar_number"])
        ),
    } for key in both_stops]
    days = sorted(str(value) for value in feature_rows.date.unique())
    return {
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "effective_sessions": {"count": len(days), "start": days[0], "end": days[-1]},
        "source_v5_generated_at": source_report.get("generated_at"),
        "research_governance": {
            "diagnostic_only": True,
            "explicit_trial_count": 1,
            "trial_count_method": "one_execution_parity_correction_no_parameter_search",
            "selection_eligible": False,
            "entries_sides_horizons_costs_and_stop_distances_frozen": True,
        },
        "source": {
            "mark_endpoint": MARK_ENDPOINT,
            "mark_bar": "5m",
            "mark_data_sha256": _stable_sha256(normalized_sources),
            "diagnostic_code_sha256": _sha256_file(Path(__file__)),
            "source_v5_code_sha256": _sha256_file(SOURCE_V5_CODE),
            "source_v5_report_sha256": _sha256_file(SOURCE_REPORT),
        },
        "coverage": {
            "source_trades": len(frozen),
            "covered_trades": len(mark_trades),
            "coverage_pct": round(len(mark_trades) / len(frozen) * 100.0, 2) if frozen else 0.0,
            "missing": missing,
        },
        "stop_concordance": {
            "last_price_stops": len(last_stops),
            "mark_price_stops": len(mark_stops),
            "both": len(both_stops),
            "last_only": len(last_stops - mark_stops),
            "mark_only": len(mark_stops - last_stops),
            "neither": len(covered_baseline) - len(last_stops | mark_stops),
            "same_trigger_bar": sum(item["mark_minus_last_bars"] == 0 for item in timing_differences),
            "mark_triggered_later": sum(item["mark_minus_last_bars"] > 0 for item in timing_differences),
            "mark_triggered_earlier": sum(item["mark_minus_last_bars"] < 0 for item in timing_differences),
            "last_only_trades": [
                {"date": key[0], "symbol": key[1]} for key in sorted(last_stops - mark_stops)
            ],
            "mark_only_trades": [
                {"date": key[0], "symbol": key[1]} for key in sorted(mark_stops - last_stops)
            ],
            "timing_differences": timing_differences,
        },
        "covered_last_price_baseline": _assessment(covered_baseline),
        "mark_price_stop_diagnostic": _assessment(mark_trades),
        "promotion_passed": False,
        "recommendation": (
            "use_mark_price_stop_as_the_execution_parity_research_baseline_after_independent_review"
            if len(mark_trades) == len(frozen) else "collect_missing_mark_paths_before_comparison"
        ),
        "promotion_blockers": [
            "retrospective_execution_parity_correction_not_a_forward_validation",
            "five_minute_mark_bars_do_not_reveal_intrabar_fill_slippage",
            "historical_books5_and_realized_stop_fill_data_are_unavailable",
        ],
    }


def run() -> dict[str, Any]:
    source = json.loads(SOURCE_REPORT.read_text())
    end = date.fromisoformat(source["effective_sessions"]["end"])
    sessions = weekday_sessions(end, int(source.get("requested_sessions") or 100))
    features = build_features(load_market_data(
        OKX(settings()), load_symbols("historical_90d"), sessions,
    ))
    client = OKX(settings())
    paths: dict[tuple[str, str], list[list[str]]] = {}
    trades = source.get("trades") or []
    for index, trade in enumerate(trades, 1):
        paths[_key(trade)] = fetch_mark_path(client, trade)
        print(f"[{index}/{len(trades)}] mark path {trade['date']} {trade['symbol']}", flush=True)
    return research(features, source, paths)


def main() -> None:
    report = run()
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({
        "coverage": report["coverage"],
        "stop_concordance": report["stop_concordance"],
        "last_price": {
            "all": report["covered_last_price_baseline"]["all"],
            "risk": {
                key: report["covered_last_price_baseline"]["risk_weighted_portfolio"][key]
                for key in ("return_pct", "max_drawdown_pct")
            },
        },
        "mark_price": {
            "all": report["mark_price_stop_diagnostic"]["all"],
            "risk": {
                key: report["mark_price_stop_diagnostic"]["risk_weighted_portfolio"][key]
                for key in ("return_pct", "max_drawdown_pct")
            },
        },
        "promotion_passed": report["promotion_passed"],
        "recommendation": report["recommendation"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
