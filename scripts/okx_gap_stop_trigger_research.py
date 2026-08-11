#!/usr/bin/env python3
"""Diagnose V5 stop-trigger behavior without changing frozen entries.

The V5 backtest treats any five-minute high/low crossing the ATR stop as a
stop-market fill.  That is correct for a last-price trigger, but the cached
history cannot show whether an exchange mark-price trigger would also fire.
This script therefore does two bounded retrospective diagnostics only:

* require a completed five-minute close beyond the original stop;
* the same close confirmation plus a synthetic 2R intrabar emergency stop.

Both variants keep the exact frozen V5 trades, sides, horizons and costs.  They
cannot authorize execution; their purpose is to decide what forward trigger
prices and order-book evidence must be collected.
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
from scripts.okx_gap_strategy_v5 import (  # noqa: E402
    AdaptiveHorizonConfig,
    risk_weighted_portfolio_metrics,
)
from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import load_market_data, weekday_sessions  # noqa: E402
from scripts.okx_research_universe import load_symbols  # noqa: E402

UTC = timezone.utc
OUTPUT = ROOT / "data" / "okx_gap_stop_trigger_research.json"
SOURCE_REPORT = ROOT / "data" / "okx_gap_strategy_v5_backtest.json"
SOURCE_V5_CODE = ROOT / "scripts" / "okx_gap_strategy_v5.py"
DIAGNOSTIC_NAMES = (
    "five_minute_close_confirmed_diagnostic",
    "five_minute_close_confirmed_2r_emergency_diagnostic",
)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_fingerprints() -> dict[str, str]:
    return {
        "diagnostic_code_sha256": _sha256_file(Path(__file__)),
        "source_v5_code_sha256": _sha256_file(SOURCE_V5_CODE),
        "source_v5_report_sha256": _sha256_file(SOURCE_REPORT),
    }


def _direction(trade: dict[str, Any]) -> int:
    side = str(trade["side"])
    if side not in {"LONG", "SHORT"}:
        raise ValueError(f"unsupported side: {side}")
    return 1 if side == "LONG" else -1


def replay_close_trigger(
    row: pd.Series,
    trade: dict[str, Any],
    *,
    round_trip_cost_bps: float,
    emergency_multiple: float | None = None,
) -> dict[str, Any]:
    """Replay one frozen trade using completed-bar stop confirmation.

    A synthetic emergency exit is priced exactly at its threshold because only
    OHLC extremes are available.  The report flags this as an optimistic
    execution assumption; it is never treated as a promotable result.
    """
    result = copy.deepcopy(trade)
    direction = _direction(trade)
    entry = float(row.entry)
    stop_fraction = float(trade["stop_bps"]) / 10_000.0
    horizon = int(trade["horizon_minutes"])
    if entry <= 0 or stop_fraction <= 0 or horizon <= 0:
        raise ValueError("trade is missing a usable entry, stop or horizon")
    path = row.path_150
    if not isinstance(path, (list, tuple)) or len(path) < horizon // 5:
        raise ValueError("feature row is missing the frozen horizon path")

    exit_price = float(row[f"exit_{horizon}"])
    exit_bar: int | None = None
    exit_reason = "horizon"
    exit_basis = "scheduled_horizon"
    for index, (high, low, close) in enumerate(path[: horizon // 5]):
        high, low, close = float(high), float(low), float(close)
        adverse_extreme = (
            (entry - low) / entry if direction > 0 else (high - entry) / entry
        )
        close_adverse = (
            (entry - close) / entry if direction > 0 else (close - entry) / entry
        )
        if emergency_multiple is not None and adverse_extreme >= stop_fraction * emergency_multiple:
            exit_price = entry * (1.0 - direction * stop_fraction * emergency_multiple)
            exit_bar = index + 1
            exit_reason = "synthetic_2r_intrabar_emergency"
            exit_basis = "five_minute_extreme_threshold_assumed_fill"
            break
        if close_adverse >= stop_fraction:
            exit_price = close
            exit_bar = index + 1
            exit_reason = "five_minute_close_confirmed_stop"
            exit_basis = "completed_five_minute_close"
            break

    net_pct = direction * (exit_price / entry - 1.0) * 100.0 - round_trip_cost_bps / 100.0
    result.update({
        "exit_time": int(trade["entry_time"]) + (exit_bar * 5 if exit_bar else horizon) * 60_000,
        "exit_time_basis": exit_basis,
        "stop_bar_number": exit_bar,
        "net_pct": round(net_pct, 6),
        "exit_reason": exit_reason,
        "label_start_time": int(trade["entry_time"]),
        "label_end_time": int(trade["entry_time"])
        + (exit_bar * 5 if exit_bar else horizon) * 60_000,
    })
    return result


def _indexed_rows(rows: pd.DataFrame) -> pd.DataFrame:
    required = {"date", "symbol", "entry", "path_150"}
    missing = required.difference(rows.columns)
    if missing:
        raise ValueError(f"feature rows missing columns: {sorted(missing)}")
    if rows.duplicated(["date", "symbol"]).any():
        raise ValueError("feature rows contain duplicate date/symbol keys")
    return rows.set_index(["date", "symbol"])


def replay_variant(
    frozen_trades: Iterable[dict[str, Any]],
    rows: pd.DataFrame,
    *,
    round_trip_cost_bps: float,
    emergency_multiple: float | None,
) -> list[dict[str, Any]]:
    indexed = _indexed_rows(rows)
    result: list[dict[str, Any]] = []
    for trade in frozen_trades:
        key = (str(trade["date"]), str(trade["symbol"]))
        if key not in indexed.index:
            raise ValueError(f"frozen V5 trade has no matching causal features: {key}")
        result.append(replay_close_trigger(
            indexed.loc[key], trade,
            round_trip_cost_bps=round_trip_cost_bps,
            emergency_multiple=emergency_multiple,
        ))
    return result


def _stop_rows(
    frozen_trades: Iterable[dict[str, Any]],
    rows: pd.DataFrame,
    *,
    round_trip_cost_bps: float,
) -> list[dict[str, Any]]:
    indexed = _indexed_rows(rows)
    result: list[dict[str, Any]] = []
    for trade in frozen_trades:
        if trade.get("exit_reason") != "atr_stop":
            continue
        key = (str(trade["date"]), str(trade["symbol"]))
        row = indexed.loc[key]
        direction = _direction(trade)
        entry = float(row.entry)
        stop_fraction = float(trade["stop_bps"]) / 10_000.0
        stop_price = entry * (1.0 - direction * stop_fraction)
        index = int(trade["stop_bar_number"] or 0) - 1
        if index < 0 or index >= len(row.path_150):
            raise ValueError(f"invalid stop bar for {key}")
        high, low, close = (float(value) for value in row.path_150[index])
        close_beyond = close <= stop_price if direction > 0 else close >= stop_price
        next_close = (
            float(row.path_150[index + 1][2]) if index + 1 < len(row.path_150) else close
        )
        next_recovered = next_close > stop_price if direction > 0 else next_close < stop_price
        horizon = int(trade["horizon_minutes"])
        no_stop_net = (
            direction * (float(row[f"exit_{horizon}"]) / entry - 1.0) * 100.0
            - round_trip_cost_bps / 100.0
        )
        result.append({
            "date": key[0],
            "symbol": key[1],
            "side": trade["side"],
            "stop_bar_number": index + 1,
            "within_first_15_minutes": index < 3,
            "wick_only_trigger_bar": not close_beyond,
            "next_bar_close_recovered": next_recovered,
            "would_win_at_frozen_horizon_without_stop": no_stop_net > 0.0,
            "frozen_horizon_no_stop_net_pct": round(no_stop_net, 6),
        })
    return result


def stop_path_diagnostics(
    frozen_trades: Iterable[dict[str, Any]],
    rows: pd.DataFrame,
    *,
    round_trip_cost_bps: float,
) -> dict[str, Any]:
    details = _stop_rows(frozen_trades, rows, round_trip_cost_bps=round_trip_cost_bps)

    def summarize(values: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "stops": len(values),
            "within_first_15_minutes": sum(bool(item["within_first_15_minutes"]) for item in values),
            "wick_only_trigger_bars": sum(bool(item["wick_only_trigger_bar"]) for item in values),
            "next_bar_close_recovered": sum(bool(item["next_bar_close_recovered"]) for item in values),
            "frozen_horizon_without_stop_winners": sum(
                bool(item["would_win_at_frozen_horizon_without_stop"]) for item in values
            ),
            "frozen_horizon_without_stop_net_pct": round(
                sum(float(item["frozen_horizon_no_stop_net_pct"]) for item in values), 6,
            ),
        }

    return {
        "all": summarize(details),
        "by_side": {
            side: summarize([item for item in details if item["side"] == side])
            for side in ("LONG", "SHORT")
        },
        "details": details,
    }


def _assessment(trades: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "all": metrics(trades),
        "risk_weighted_portfolio": risk_weighted_portfolio_metrics(
            trades, AdaptiveHorizonConfig(),
        ),
        "trades": trades,
    }


def research(
    rows: pd.DataFrame,
    source_report: dict[str, Any],
    *,
    generated_at: str | None = None,
    fingerprints: dict[str, str] | None = None,
) -> dict[str, Any]:
    days = sorted(str(value) for value in rows.date.unique())
    expected = source_report.get("effective_sessions") or {}
    observed = {"count": len(days), "start": days[0], "end": days[-1]}
    if observed != expected:
        raise ValueError(f"source V5 session mismatch: expected={expected}, observed={observed}")
    frozen = copy.deepcopy(source_report.get("trades") or [])
    baseline = _assessment(frozen)
    reported_metrics = source_report.get("metrics") or {}
    reported_all = reported_metrics.get("all", reported_metrics)
    if baseline["all"] != reported_all:
        raise ValueError("frozen V5 trade metrics do not match source report")
    cost_bps = float((source_report.get("trade_filter") or {}).get("round_trip_cost_bps") or 14.0)
    close_confirmed = replay_variant(
        frozen, rows, round_trip_cost_bps=cost_bps, emergency_multiple=None,
    )
    emergency = replay_variant(
        frozen, rows, round_trip_cost_bps=cost_bps, emergency_multiple=2.0,
    )
    diagnostics = {
        DIAGNOSTIC_NAMES[0]: _assessment(close_confirmed),
        DIAGNOSTIC_NAMES[1]: _assessment(emergency),
    }
    return {
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "effective_sessions": observed,
        "source_v5_generated_at": source_report.get("generated_at"),
        "fingerprints": fingerprints or source_fingerprints(),
        "research_governance": {
            "diagnostic_only": True,
            "explicit_trial_count": 2,
            "trial_count_method": "baseline_fixed_plus_two_predeclared_diagnostics",
            "selection_eligible": False,
            "entries_sides_horizons_and_costs_frozen": True,
        },
        "trigger_evidence_limit": (
            "historical mark price and intrabar ordering are unavailable; "
            "the 2R emergency fill is a synthetic threshold assumption"
        ),
        "baseline": baseline,
        "stop_path_diagnostics": stop_path_diagnostics(
            frozen, rows, round_trip_cost_bps=cost_bps,
        ),
        "diagnostics": diagnostics,
        "promotion_passed": False,
        "recommendation": "retain_last_price_stop_and_collect_forward_last_vs_mark_trigger_evidence",
        "promotion_blockers": [
            "all_dates_are_retrospective_and_previously_inspected",
            "close_confirmed_variants_do_not_improve_pf_return_and_drawdown_together",
            "historical_mark_price_trigger_path_is_unavailable",
            "synthetic_2r_emergency_fill_does_not_model_gap_slippage",
        ],
    }


def run() -> dict[str, Any]:
    source = json.loads(SOURCE_REPORT.read_text())
    end = date.fromisoformat(source["effective_sessions"]["end"])
    sessions = weekday_sessions(end, int(source.get("requested_sessions") or 100))
    raw = load_market_data(OKX(settings()), load_symbols("historical_90d"), sessions)
    return research(build_features(raw), source)


def main() -> None:
    report = run()
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({
        "effective_sessions": report["effective_sessions"],
        "stop_path_diagnostics": report["stop_path_diagnostics"]["all"],
        "baseline": {
            "all": report["baseline"]["all"],
            "risk": report["baseline"]["risk_weighted_portfolio"],
        },
        "diagnostics": {
            name: {"all": value["all"], "risk": value["risk_weighted_portfolio"]}
            for name, value in report["diagnostics"].items()
        },
        "promotion_passed": report["promotion_passed"],
        "recommendation": report["recommendation"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
