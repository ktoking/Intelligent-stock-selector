#!/usr/bin/env python3
"""Reconstruct OKX demo trades with 1m/5m/synthetic-10m candle context."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_intraday_agent import DB_PATH, OKX, atr, ema, settings, vwap  # noqa: E402

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
BAR_MS = {"1m": 60_000, "5m": 300_000}


def closed_chronological(rows: list[list[str]]) -> list[list[str]]:
    unique = {int(row[0]): row for row in rows if len(row) >= 9 and row[8] == "1"}
    return [unique[key] for key in sorted(unique)]


def aggregate_10m(rows_5m: list[list[str]]) -> list[list[str]]:
    """Build UTC-aligned 10m bars from two closed 5m bars."""
    buckets: dict[int, list[list[str]]] = {}
    for row in closed_chronological(rows_5m):
        bucket = int(row[0]) // 600_000 * 600_000
        buckets.setdefault(bucket, []).append(row)
    result: list[list[str]] = []
    for bucket, rows in sorted(buckets.items()):
        rows.sort(key=lambda item: int(item[0]))
        expected = {bucket, bucket + 300_000}
        if {int(item[0]) for item in rows} != expected:
            continue
        result.append([
            str(bucket), rows[0][1], str(max(float(item[2]) for item in rows)),
            str(min(float(item[3]) for item in rows)), rows[-1][4],
            str(sum(float(item[5]) for item in rows)),
            str(sum(float(item[6]) for item in rows)),
            str(sum(float(item[7]) for item in rows)), "1",
        ])
    return result


def candle_window(client: OKX, inst_id: str, bar: str, start_ms: int, end_ms: int) -> list[list[str]]:
    """Page backwards through OKX history-candles and return a closed window."""
    if bar not in BAR_MS:
        raise ValueError(f"unsupported replay bar: {bar}")
    cursor = end_ms + BAR_MS[bar]
    collected: dict[int, list[str]] = {}
    for _ in range(12):
        page = client.request("GET", "/api/v5/market/history-candles", {
            "instId": inst_id, "bar": bar, "after": str(cursor), "limit": "300",
        })["data"]
        if not page:
            break
        stamps = []
        for row in page:
            stamp = int(row[0])
            stamps.append(stamp)
            if start_ms <= stamp <= end_ms and len(row) >= 9 and row[8] == "1":
                collected[stamp] = row
        oldest = min(stamps)
        if oldest <= start_ms or oldest >= cursor:
            break
        cursor = oldest
        time.sleep(0.11)
    return [collected[key] for key in sorted(collected)]


def _close_at_or_before(rows: list[list[str]], stamp_ms: int) -> float | None:
    eligible = [row for row in rows if int(row[0]) <= stamp_ms]
    return float(eligible[-1][4]) if eligible else None


def _path_return(side: str, entry: float, price: float | None) -> float | None:
    if price is None or entry <= 0:
        return None
    raw = (price - entry) / entry
    return raw if side == "long" else -raw


def analyze_path(position: dict[str, Any], rows_1m: list[list[str]], rows_5m: list[list[str]]) -> dict[str, Any]:
    entry_ms, exit_ms = int(position["cTime"]), int(position["uTime"])
    entry = float(position.get("openAvgPx") or 0)
    side = str(position.get("posSide") or "")
    holding = [row for row in rows_1m if entry_ms <= int(row[0]) <= exit_ms]
    pre = [row for row in rows_1m if int(row[0]) < entry_ms]
    post = [row for row in rows_1m if int(row[0]) > exit_ms]
    if not holding or entry <= 0:
        return {"available": False, "reason": "holding-period 1m candles unavailable"}
    highs = [float(row[2]) for row in holding]
    lows = [float(row[3]) for row in holding]
    # Minute bars are an approximation around exchange fill timestamps.  The
    # entry itself is always a zero-excursion observation, so neither MFE nor
    # MAE can be negative even when the first closed bar is already beyond it.
    mfe = max(0.0, (max(highs) - entry) / entry if side == "long" else (entry - min(lows)) / entry)
    mae = max(0.0, (entry - min(lows)) / entry if side == "long" else (max(highs) - entry) / entry)
    pre_5m = [row for row in rows_5m if int(row[0]) < entry_ms]
    atr14 = atr(pre_5m) if len(pre_5m) >= 15 else 0.0
    stop_distance = max(atr14 * 1.2, entry * 0.002)
    risk_pct = stop_distance / entry if entry else 0.0
    pre_high = max((float(row[2]) for row in pre[-12:]), default=entry)
    pre_low = min((float(row[3]) for row in pre[-12:]), default=entry)
    first_three = holding[:3]
    failed_breakout = (
        any(float(row[4]) < pre_high for row in first_three)
        if side == "long" else any(float(row[4]) > pre_low for row in first_three)
    )
    horizon_returns = {}
    for minutes in (5, 15, 30, 60):
        price = _close_at_or_before(holding, entry_ms + minutes * 60_000)
        value = _path_return(side, entry, price)
        horizon_returns[f"return_{minutes}m_bps"] = round(value * 10_000, 2) if value is not None else None
    post_30_price = _close_at_or_before(post, exit_ms + 30 * 60_000)
    post_return = _path_return(side, float(position.get("closeAvgPx") or entry), post_30_price)
    return {
        "available": True,
        "measurement": "closed 1m candle approximation; entry and exit bars may contain sub-minute path ambiguity",
        "holding_minutes": round((exit_ms - entry_ms) / 60_000, 2),
        "mfe_bps": round(mfe * 10_000, 2), "mae_bps": round(mae * 10_000, 2),
        "mfe_r": round(mfe / risk_pct, 3) if risk_pct else None,
        "mae_r": round(mae / risk_pct, 3) if risk_pct else None,
        "atr14_5m_at_entry": atr14, "theoretical_stop_distance": stop_distance,
        "failed_breakout_first_3m": failed_breakout,
        "post_exit_30m_directional_bps": round(post_return * 10_000, 2) if post_return is not None else None,
        **horizon_returns,
    }


def replay_trade(client: OKX, position: dict[str, Any], pre_minutes: int = 60,
                 post_minutes: int = 30) -> dict[str, Any]:
    entry_ms, exit_ms = int(position["cTime"]), int(position["uTime"])
    start_ms = entry_ms - pre_minutes * 60_000
    end_ms = exit_ms + post_minutes * 60_000
    rows_1m = candle_window(client, position["instId"], "1m", start_ms, end_ms)
    rows_5m = candle_window(client, position["instId"], "5m", start_ms - 120 * 60_000, end_ms)
    rows_10m = aggregate_10m(rows_5m)
    analysis = analyze_path(position, rows_1m, rows_5m)
    return {
        # OKX reuses posId across repeated openings of the same instrument and
        # side, so timestamps are required for a stable per-trade audit key.
        "trade_key": f"{position.get('posId') or position['instId']}:{position['cTime']}:{position['uTime']}",
        "inst_id": position["instId"], "side": position.get("posSide"),
        "entry_time": datetime.fromtimestamp(entry_ms / 1000, UTC).isoformat(),
        "exit_time": datetime.fromtimestamp(exit_ms / 1000, UTC).isoformat(),
        "entry_price": float(position.get("openAvgPx") or 0),
        "exit_price": float(position.get("closeAvgPx") or 0),
        "gross_pnl": float(position.get("pnl") or 0), "fee": float(position.get("fee") or 0),
        "funding_fee": float(position.get("fundingFee") or 0),
        "realized_pnl": float(position.get("realizedPnl") or 0),
        "leverage": float(position.get("lever") or 0), "analysis": analysis,
        "candles": {"1m": rows_1m, "5m": rows_5m, "10m": rows_10m},
    }


def summarize(replays: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [row for row in replays if row["analysis"].get("available")]
    def avg(field: str) -> float | None:
        values = [float(row["analysis"][field]) for row in usable if row["analysis"].get(field) is not None]
        return round(sum(values) / len(values), 3) if values else None
    false_breakouts = sum(bool(row["analysis"].get("failed_breakout_first_3m")) for row in usable)
    had_one_r = sum(float(row["analysis"].get("mfe_r") or 0) >= 1 for row in usable)
    returned_after_exit = sum(float(row["analysis"].get("post_exit_30m_directional_bps") or 0) > 0 for row in usable)
    return {
        "trades": len(replays), "analyzed": len(usable),
        "net_pnl": round(sum(row["realized_pnl"] for row in replays), 4),
        "fees": round(sum(row["fee"] for row in replays), 4),
        "failed_breakout_first_3m": false_breakouts,
        "failed_breakout_rate": round(false_breakouts / len(usable), 4) if usable else None,
        "reached_1r_before_exit": had_one_r,
        "reached_1r_rate": round(had_one_r / len(usable), 4) if usable else None,
        "direction_continued_30m_after_exit": returned_after_exit,
        "avg_mfe_bps": avg("mfe_bps"), "avg_mae_bps": avg("mae_bps"),
        "avg_mfe_r": avg("mfe_r"), "avg_mae_r": avg("mae_r"),
        "avg_return_5m_bps": avg("return_5m_bps"),
        "avg_return_15m_bps": avg("return_15m_bps"),
        "avg_return_30m_bps": avg("return_30m_bps"),
        "avg_return_60m_bps": avg("return_60m_bps"),
    }


def persist_replays(trade_day: str, replays: list[dict[str, Any]]) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS okx_trade_replays (
                trade_key TEXT PRIMARY KEY, trade_day TEXT NOT NULL, inst_id TEXT NOT NULL,
                side TEXT, entry_time TEXT, exit_time TEXT, realized_pnl REAL,
                analysis_json TEXT NOT NULL, candles_json TEXT NOT NULL, updated_at TEXT NOT NULL
            )
        """)
        conn.execute("DELETE FROM okx_trade_replays WHERE trade_day = ?", (trade_day,))
        conn.executemany("""
            INSERT INTO okx_trade_replays
            (trade_key,trade_day,inst_id,side,entry_time,exit_time,realized_pnl,analysis_json,candles_json,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(trade_key) DO UPDATE SET
              exit_time=excluded.exit_time,realized_pnl=excluded.realized_pnl,
              analysis_json=excluded.analysis_json,candles_json=excluded.candles_json,updated_at=excluded.updated_at
        """, [(
            row["trade_key"], trade_day, row["inst_id"], row["side"], row["entry_time"], row["exit_time"],
            row["realized_pnl"], json.dumps(row["analysis"], ensure_ascii=False),
            json.dumps(row["candles"], ensure_ascii=False), datetime.now(UTC).isoformat(),
        ) for row in replays])


def replay_day(client: OKX, trade_day: date, pre_minutes: int = 60,
               post_minutes: int = 30) -> dict[str, Any]:
    history = client.request("GET", "/api/v5/account/positions-history", {
        "instType": "SWAP", "limit": "100",
    }, private=True)["data"]
    positions = [row for row in history if datetime.fromtimestamp(
        int(row.get("uTime") or 0) / 1000, UTC
    ).astimezone(NY).date() == trade_day]
    positions.sort(key=lambda row: int(row["cTime"]))
    replays = []
    for index, position in enumerate(positions, 1):
        print(f"[{index}/{len(positions)}] replay {position['instId']} {position.get('posSide')}", file=sys.stderr)
        replays.append(replay_trade(client, position, pre_minutes, post_minutes))
    payload = {
        "trade_day": trade_day.isoformat(), "generated_at": datetime.now(UTC).isoformat(),
        "window": {"pre_minutes": pre_minutes, "post_minutes": post_minutes},
        "summary": summarize(replays), "trades": replays,
    }
    persist_replays(trade_day.isoformat(), replays)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="New York trade date, YYYY-MM-DD")
    parser.add_argument("--pre-minutes", type=int, default=60)
    parser.add_argument("--post-minutes", type=int, default=30)
    parser.add_argument("--output")
    args = parser.parse_args()
    trade_day = date.fromisoformat(args.date)
    result = replay_day(OKX(settings()), trade_day, args.pre_minutes, args.post_minutes)
    output = Path(args.output) if args.output else ROOT / "data" / f"okx_trade_replay_{trade_day.isoformat()}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({"output": str(output), "summary": result["summary"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
