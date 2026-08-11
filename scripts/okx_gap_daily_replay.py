#!/usr/bin/env python3
"""Replay the exact Demo-gap entry rule against one completed US cash session.

This is a public-candle reconstruction, not an exchange fill report: it uses
the 09:36 one-minute open as an executable-price proxy, a conservative 75bp
same-bar stop precedence, and 14bp round-trip costs.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_gap_shadow import gap_context  # noqa: E402
from scripts.okx_intraday_agent import DB_PATH, OKX, settings  # noqa: E402
from scripts.okx_trade_replay import candle_window  # noqa: E402

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
MICROSTRUCTURE_STATE_PATH = ROOT / "data" / "okx_microstructure.json"
FALLBACK_DEMO_SYMBOLS = ("AAOI-USDT-SWAP", "CRCL-USDT-SWAP", "CRWV-USDT-SWAP", "GOOGL-USDT-SWAP",
                         "HOOD-USDT-SWAP", "MRVL-USDT-SWAP", "NVDA-USDT-SWAP", "ORCL-USDT-SWAP", "TSLA-USDT-SWAP")
STOP_BPS, COST_BPS = 75.0, 14.0


def demo_symbols() -> tuple[str, ...]:
    """Replay the same private-Demo-compatible universe observed by the runtime."""
    try:
        state = json.loads(MICROSTRUCTURE_STATE_PATH.read_text())
        symbols = tuple(symbol for symbol in state.get("demo_tradeable_symbols") or ()
                        if symbol not in {"BTC-USDT-SWAP", "SPY-USDT-SWAP", "QQQ-USDT-SWAP", "SMH-USDT-SWAP"})
        return symbols or FALLBACK_DEMO_SYMBOLS
    except (OSError, ValueError, TypeError):
        return FALLBACK_DEMO_SYMBOLS


def stamp(day: date, value: time) -> int:
    return int(datetime.combine(day, value, NY).astimezone(UTC).timestamp() * 1000)


def at(rows: list[list[str]], day: date, value: time) -> list[str] | None:
    for row in rows:
        local = datetime.fromtimestamp(int(row[0]) / 1000, UTC).astimezone(NY)
        if local.date() == day and local.time().replace(second=0, microsecond=0) == value:
            return row
    return None


def replay_path(rows: list[list[str]], day: date, side: str) -> dict[str, float | str] | None:
    entry_row = at(rows, day, time(9, 36))
    if not entry_row:
        return None
    entry = float(entry_row[1])
    direction = 1 if side == "LONG" else -1
    stop = entry * (1 - direction * STOP_BPS / 10_000)
    due = stamp(day, time(9, 36)) + 150 * 60_000
    holding = [row for row in rows if stamp(day, time(9, 36)) <= int(row[0]) <= due]
    for row in holding:
        if (direction > 0 and float(row[3]) <= stop) or (direction < 0 and float(row[2]) >= stop):
            return {"entry": entry, "exit": stop, "exit_reason": "75bp hard stop", "net_r": (-STOP_BPS - COST_BPS) / 100}
    if not holding:
        return None
    exit_px = float(holding[-1][4])
    net_bps = direction * (exit_px / entry - 1) * 10_000 - COST_BPS
    return {"entry": entry, "exit": exit_px, "exit_reason": "150m fixed time exit", "net_r": net_bps / 100}


def historical_liquidity(inst_id: str, entry_ts: int, side: str, entry: float) -> dict[str, object]:
    """Reconstruct the same pre-trade microstructure gate from stored snapshots."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("""SELECT spread_bps,bid_depth,ask_depth,book_imbalance,aggressive_imbalance,
                                      capture_complete,book_age_seconds,trade_count,ct_val
                               FROM okx_microstructure_minute WHERE inst_id=? AND minute_ts=?""",
                           (inst_id, entry_ts // 1000)).fetchone()
    if not row:
        return {"available": False, "qualified": False, "reason": "historical microstructure snapshot unavailable"}
    spread, bid_depth, ask_depth, book, aggressive, complete, age, count, ct_val = row
    direction = 1 if side == "LONG" else -1
    available = bool(complete and float(age or 999) <= 120 and int(count or 0) > 0)
    aligned = bool(available and float(book or 0) * direction > 0 and float(aggressive or 0) * direction > 0)
    side_depth = float(ask_depth if side == "LONG" else bid_depth) * entry * float(ct_val or 0)
    qualified = bool(aligned and float(spread or 999) <= 5 and side_depth >= 5_000)
    return {"available": available, "aligned": aligned, "spread_bps": round(float(spread or 0), 3),
            "side_depth_usdt": round(side_depth, 2), "qualified": qualified}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=(datetime.now(NY).date() - timedelta(days=1)).isoformat())
    args = parser.parse_args()
    day = date.fromisoformat(args.date)
    symbols = demo_symbols()
    previous = day - timedelta(days=1)
    while previous.weekday() >= 5:
        previous -= timedelta(days=1)
    client = OKX(settings())
    start_5m, end_5m = stamp(previous, time(9, 30)), stamp(day, time(12, 10))
    start_1m, end_1m = stamp(day, time(9, 36)), stamp(day, time(12, 6))
    contexts: dict[str, dict[str, float]] = {}
    one_minute: dict[str, list[list[str]]] = {}
    for symbol in ("SPY-USDT-SWAP", *symbols):
        rows5 = candle_window(client, symbol, "5m", start_5m, end_5m)
        context = gap_context(rows5, rows5, day)
        if context:
            contexts[symbol] = context
        if symbol != "SPY-USDT-SWAP":
            one_minute[symbol] = candle_window(client, symbol, "1m", start_1m, end_1m)
    spy = contexts.get("SPY-USDT-SWAP")
    if not spy:
        raise RuntimeError("SPY opening context unavailable")
    candidates = []
    for symbol in symbols:
        value = contexts.get(symbol)
        if not value:
            continue
        relative = value["gap_bps"] - spy["gap_bps"]
        first5 = value["first5_bps"] - spy["first5_bps"]
        previous_day = value["previous_day_bps"] - spy["previous_day_bps"]
        confirmed = relative * first5 < 0 or relative * previous_day <= 0
        if abs(relative) >= 100 and confirmed:
            candidates.append({"symbol": symbol, "side": "SHORT" if relative > 0 else "LONG",
                               "relative_gap_bps": round(relative, 2), "relative_first5_bps": round(first5, 2),
                               "relative_previous_day_bps": round(previous_day, 2)})
    candidates.sort(key=lambda item: abs(float(item["relative_gap_bps"])), reverse=True)
    output = []
    for candidate in candidates[:5]:
        path = replay_path(one_minute.get(candidate["symbol"], []), day, str(candidate["side"]))
        entry_ts = stamp(day, time(9, 36))
        entry = float((path or {}).get("entry") or 0)
        liquidity = historical_liquidity(candidate["symbol"], entry_ts, str(candidate["side"]), entry)
        output.append({**candidate, "execution_liquidity": liquidity,
                       **(path or {"exit_reason": "1m replay data unavailable"})})
    executable = [item for item in output if item["execution_liquidity"].get("qualified") and "net_r" in item]
    net_r = sum(float(item.get("net_r") or 0) for item in executable)
    report = {"trade_day": day.isoformat(), "generated_at": datetime.now(UTC).isoformat(),
              "method": "public 1m candle replay; 09:36 open entry proxy; 75bp stop takes same-bar precedence; 14bp cost",
              "demo_universe": list(symbols), "candidates": output,
              "summary": {"signal_candidates": len(output), "executable_candidates": len(executable),
                          "net_r": round(net_r, 4), "wins": sum(float(item.get("net_r") or 0) > 0 for item in executable)},
              "warning": "Not a Demo exchange-fill record; historical bid/ask, depth and exact market fill are unavailable."}
    path = ROOT / "data" / f"okx_gap_daily_replay_{day.isoformat()}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
