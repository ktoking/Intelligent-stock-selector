#!/usr/bin/env python3
"""Continuously report forward outcomes of preregistered microstructure rules."""
from __future__ import annotations

import json
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_intraday_agent import DB_PATH  # noqa: E402

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
OUTPUT = ROOT / "data" / "okx_microstructure_forward.json"


def metric(values: list[float]) -> dict:
    gains = sum(x for x in values if x > 0); losses = -sum(x for x in values if x < 0)
    return {"samples": len(values), "win_rate": round(sum(x > 0 for x in values) / len(values) * 100, 2) if values else 0,
            "expectancy_r": round(sum(values) / len(values), 4) if values else 0,
            "profit_factor": round(gains / losses, 3) if losses else None}


def executable_values(rows: list[sqlite3.Row], horizon: int, predicate) -> tuple[list[float], int, int]:
    column = f"forward_{horizon}m_bps"
    grouped = {}
    for row in rows:
        available = set(row.keys())
        if not {"capture_complete", "book_age_seconds", "ct_val"}.issubset(available):
            continue
        if (row[column] is None or not predicate(row) or int(row["trade_count"] or 0) <= 0
                or not int(row["capture_complete"] or 0)
                or float(row["book_age_seconds"] or 999) > 10
                or int(row["minute_ts"]) % (horizon * 60) != 0):
            continue
        book, aggressive = float(row["book_imbalance"] or 0), float(row["aggressive_imbalance"] or 0)
        if not book or not aggressive or book * aggressive <= 0:
            continue
        direction = 1 if book > 0 else -1
        side_depth = float((row["ask_depth"] if direction > 0 else row["bid_depth"]) or 0)
        mid = (float(row["bid_px"] or 0) + float(row["ask_px"] or 0)) / 2
        spread, ct_val = float(row["spread_bps"] or 0), float(row["ct_val"] or 0)
        if spread > 5 or side_depth * mid * ct_val < 5_000:
            continue
        strength = (abs(book) + abs(aggressive)) * max(1, int(row["trade_count"] or 0))
        grouped.setdefault(int(row["minute_ts"]), []).append((strength, row, direction, spread))
    active, values, opportunities, days = [], [], 0, set()
    for stamp, candidates in sorted(grouped.items()):
        active = [item for item in active if item["exit"] > stamp]
        slots = max(0, 5 - len(active))
        selected = []
        for _, row, direction, spread in sorted(candidates, reverse=True, key=lambda item: item[0]):
            if not slots or any(item["inst_id"] == row["inst_id"] for item in active):
                continue
            selected.append((row, direction, spread)); slots -= 1
        if selected:
            opportunities += 1
            days.add(datetime.fromtimestamp(stamp, UTC).astimezone(NY).date().isoformat())
        for row, direction, spread in selected:
            cost_bps = 8 + max(6, spread)
            values.append((direction * float(row[column]) - cost_bps) / 100)
            active.append({"inst_id": row["inst_id"], "exit": stamp + horizon * 60})
    return values, opportunities, len(days)


def is_cash(stamp: int) -> bool:
    local = datetime.fromtimestamp(stamp, UTC).astimezone(NY)
    return local.weekday() < 5 and (local.hour, local.minute) >= (9, 30) and (local.hour, local.minute) < (16, 0)


def report() -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM okx_microstructure_minute ORDER BY minute_ts").fetchall()
        except sqlite3.OperationalError:
            rows = []
    results = {}
    complete_rows = [row for row in rows if "capture_complete" in row.keys() and int(row["capture_complete"] or 0)]
    cash_development_days = sorted({
        datetime.fromtimestamp(int(row["minute_ts"]), UTC).astimezone(NY).date().isoformat()
        for row in complete_rows
        if row["inst_id"] != "BTC-USDT-SWAP" and is_cash(int(row["minute_ts"]))
        and int(row["trade_count"] or 0) > 0
    })
    for horizon in (5, 15, 60):
        column = f"forward_{horizon}m_bps"
        for scope, predicate in (
            ("all", lambda row: True),
            ("cash_equity", lambda row: row["inst_id"] != "BTC-USDT-SWAP" and is_cash(int(row["minute_ts"]))),
            ("outside_equity", lambda row: row["inst_id"] != "BTC-USDT-SWAP" and not is_cash(int(row["minute_ts"]))),
        ):
            values, opportunities, trading_days = executable_values(rows, horizon, predicate)
            results[f"{scope}_{horizon}m"] = {
                **metric(values), "opportunities": opportunities, "trading_days": trading_days,
            }
    result = {"updated_at": datetime.now(UTC).isoformat(), "mode": "research_only",
              "research_phase": "collect_cash_development" if len(cash_development_days) < 10 else "development_ready_for_frozen_selection",
              "cash_development_days": len(cash_development_days),
              "cash_development_day_list": cash_development_days,
              "freeze_after_cash_days": 10,
              "forward_samples_start_only_after_freeze": True,
              "integrity": {"complete_rows": len(complete_rows), "total_rows": len(rows)},
              "rule": "fixed wall-clock opportunities; max5 concurrent/no symbol overlap; complete fresh book+flow; spread<=5bps; contract-adjusted side depth>=5000 USDT; 14bps minimum cost",
              "results": results}
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main() -> None:
    running = True
    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    while running:
        report()
        for _ in range(60):
            if not running: break
            time.sleep(1)


if __name__ == "__main__":
    main()
