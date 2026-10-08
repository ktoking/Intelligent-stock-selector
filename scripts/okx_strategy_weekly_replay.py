#!/usr/bin/env python3
"""Build the rolling weekly strategy snapshot consumed by the OKX dashboard.

The frozen research artifact remains immutable.  This job replays the current
calendar week with the same causal V5 selector, strict-first5 no-backfill
overlay and OKX mark-price stop trigger, then writes a small presentation
artifact.  It never places an order or changes a promotion decision.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_gap_confirmation_overlay_research import confirmation_overlay_trades  # noqa: E402
from scripts.okx_gap_mark_stop_parity import replay_mark_stop  # noqa: E402
from scripts.okx_gap_strategy_v3 import build_features, metrics  # noqa: E402
from scripts.okx_gap_strategy_v5 import (  # noqa: E402
    AdaptiveHorizonConfig,
    research,
    risk_weighted_portfolio_metrics,
)
from scripts.okx_gap_strategy_v4 import latest_completed_us_session  # noqa: E402
from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import load_market_data, weekday_sessions  # noqa: E402
from scripts.okx_research_universe import load_symbols  # noqa: E402

UTC = timezone.utc
OUTPUT = ROOT / "data" / "okx_strategy_weekly_latest.json"


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _assessment(trades: list[dict[str, Any]]) -> dict[str, Any]:
    config = AdaptiveHorizonConfig()
    return {
        "metrics": metrics(trades),
        "risk_weighted": risk_weighted_portfolio_metrics(trades, config),
        "daily": {
            value: metrics([trade for trade in trades if trade["date"] == value])
            for value in sorted({str(trade["date"]) for trade in trades})
        },
        "trades": trades,
    }


def package_weekly_report(
    rows: pd.DataFrame,
    v5_report: dict[str, Any],
    mark_paths: dict[tuple[str, str], list[list[str]]],
    *,
    end: date,
    generated_at: str | None = None,
) -> dict[str, Any]:
    start = week_start(end).isoformat()
    end_name = end.isoformat()
    v5_trades = [
        trade for trade in v5_report.get("trades") or []
        if start <= str(trade["date"]) <= end_name
    ]
    strict_all = confirmation_overlay_trades(v5_report.get("trades") or [], rows)
    strict_trades = [
        trade for trade in strict_all if start <= str(trade["date"]) <= end_name
    ]
    indexed = rows.set_index(["date", "symbol"])
    mark_trades: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    cost_bps = float((v5_report.get("trade_filter") or {}).get("round_trip_cost_bps") or 14.0)
    for trade in strict_trades:
        key = (str(trade["date"]), str(trade["symbol"]))
        if key not in indexed.index:
            missing.append({"date": key[0], "symbol": key[1], "reason": "feature_row_missing"})
            continue
        value = replay_mark_stop(
            trade, indexed.loc[key], mark_paths.get(key) or [],
            round_trip_cost_bps=cost_bps,
        )
        if value is None:
            missing.append({"date": key[0], "symbol": key[1], "reason": "mark_path_incomplete"})
            continue
        mark_trades.append(value)

    stressed = [
        {**trade, "net_pct": float(trade["net_pct"]) - (25.0 - cost_bps) / 100.0}
        for trade in mark_trades
    ]
    return {
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "effective_sessions": v5_report.get("effective_sessions"),
        "week": {"start": start, "end": end_name},
        "universe_size": int(v5_report.get("universe_size") or 0),
        "method": "causal V5 -> strict-first5 no-backfill -> mark-price ATR stop",
        "base_round_trip_cost_bps": cost_bps,
        "v5": _assessment(v5_trades),
        "strict_last_price": _assessment(strict_trades),
        "strict_mark_price": _assessment(mark_trades),
        "strict_mark_price_25bp": _assessment(stressed),
        "mark_path_missing": missing,
        "promotion_passed": False,
        "execution_enabled": False,
        "warning": (
            "Retrospective weekly replay, not exchange fills. Decision-time books5 "
            "and realized slippage are not backfilled."
        ),
    }


def run(end: date | None = None, sessions_count: int = 100) -> dict[str, Any]:
    end = end or latest_completed_us_session()
    client = OKX(settings())
    symbols = load_symbols("historical_90d")
    raw = load_market_data(client, symbols, weekday_sessions(end, sessions_count))
    rows = build_features(raw)
    v5_report = research(rows)
    v5_report["universe_size"] = len(symbols)
    strict = [
        trade for trade in confirmation_overlay_trades(v5_report["trades"], rows)
        if week_start(end).isoformat() <= str(trade["date"]) <= end.isoformat()
    ]
    mark_paths: dict[tuple[str, str], list[list[str]]] = {}
    for trade in strict:
        horizon = int(trade["horizon_minutes"])
        due = int(trade["entry_time"]) + horizon * 60_000
        mark_paths[(str(trade["date"]), str(trade["symbol"]))] = (
            client.mark_price_candles_ending_at(
                str(trade["symbol"]), due,
                limit=max(40, horizon // 5 + 10), bar="5m",
            )
        )
    return package_weekly_report(rows, v5_report, mark_paths, end=end)


def main() -> None:
    report = run()
    temporary = OUTPUT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    temporary.replace(OUTPUT)
    print(json.dumps({
        "week": report["week"],
        "v5": report["v5"]["metrics"],
        "strict_mark_price": report["strict_mark_price"]["metrics"],
        "promotion_passed": report["promotion_passed"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
