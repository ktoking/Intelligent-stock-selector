#!/usr/bin/env python3
"""Thirty-session replay of the predeclared VWAP pullback long template.

This is a transparent interpretation of a discretionary template, not a
parameter search.  Conditions are evaluated only after a closed 5m bar; fills
use the next 5m open and include 14bp round-trip costs.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import CACHE_DIR, DEFAULT_SYMBOLS, aggregate_bars, load_market_data, weekday_sessions  # noqa: E402
from scripts.okx_return_model_research import metric  # noqa: E402

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
OUTPUT = ROOT / "data" / "okx_vwap_pullback_template_30d.json"
COST_BPS = 14.0
MAX_OPEN = 5


def session_frame(symbol: str, rows: list[list[str]]) -> pd.DataFrame:
    bars = aggregate_bars(rows, 5)
    frame = pd.DataFrame(bars, columns=["ts", "open", "high", "low", "close", "volume", "v1", "v2", "confirm"])
    frame = frame[frame.confirm.astype(str) == "1"].copy()
    for col in ("open", "high", "low", "close", "volume"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame["stamp"] = pd.to_datetime(frame.ts.astype("int64"), unit="ms", utc=True)
    frame["local"] = frame.stamp.dt.tz_convert(NY)
    frame = frame[(frame.local.dt.weekday < 5) & (frame.local.dt.time >= time(9, 30)) & (frame.local.dt.time <= time(15, 55))].copy()
    frame["date"] = frame.local.dt.date.astype(str)
    frame["symbol"] = symbol
    # Intraday session VWAP, not a rolling substitute.
    typical = (frame.high + frame.low + frame.close) / 3
    frame["vwap"] = (typical * frame.volume).groupby(frame.date).cumsum() / frame.volume.groupby(frame.date).cumsum().clip(lower=1e-12)
    frame["ema20"] = frame.groupby("date", sort=False).close.transform(lambda x: x.ewm(span=20, adjust=False).mean())
    frame["vol20"] = frame.groupby("date", sort=False).volume.transform(lambda x: x.shift(1).rolling(20, min_periods=12).mean())
    frame["vol_ratio"] = frame.volume / frame.vol20
    return frame.reset_index(drop=True)


def entry_candidates(frame: pd.DataFrame) -> list[dict]:
    """Return signals where every one of the five supplied buy conditions holds."""
    results: list[dict] = []
    for _, day in frame.groupby("date", sort=False):
        day = day.reset_index(drop=True)
        for index in range(23, len(day) - 1):
            now = day.iloc[index]
            if now.local.time() < time(11, 10) or now.local.time() >= time(15, 20):
                continue
            pullback = day.iloc[index - 3:index]
            prior = day.iloc[index - 6:index - 3]
            # (1)+(2): currently above session VWAP and the 5m EMA20.
            above = now.close > now.vwap and now.close > now.ema20
            # (3): a real touch/retest, but no 30bp effective VWAP break.
            retest = pullback.low.min() <= pullback.vwap.max() * 1.0015 and pullback.low.min() >= pullback.vwap.min() * .997
            # (4): the pullback contracts versus the preceding three bars.
            pullback_quiet = pullback.volume.mean() <= prior.volume.mean() * .80 if prior.volume.mean() > 0 else False
            # (5): current bar closes through the pullback high and expands.
            reacceleration = now.close > pullback.high.max() and now.volume >= pullback.volume.mean() * 1.30
            if not (above and retest and pullback_quiet and reacceleration):
                continue
            entry = day.iloc[index + 1]
            results.append({"symbol": now.symbol, "date": now.date, "signal_time": int(now.ts), "entry_time": int(entry.ts),
                            "entry": float(entry.open), "score": float(now.vol_ratio or 0) * (float(now.close / now.vwap - 1) * 10_000 + 1),
                            "day": day, "entry_index": index + 1})
    return results


def exit_trade(candidate: dict, spy: pd.DataFrame) -> dict | None:
    day, start = candidate["day"], int(candidate["entry_index"])
    for index in range(start, len(day) - 1):
        row = day.iloc[index]
        if row.local.time() >= time(15, 50):
            break
        previous = day.iloc[max(0, index - 3):index]
        if len(previous) < 3:
            continue
        span = max(float(row.high - row.low), 1e-12)
        upper_wick = float(row.high - row.close) / span
        prev_span = max(float(day.iloc[index - 1].high - day.iloc[index - 1].low), 1e-12)
        prev_upper_wick = float(day.iloc[index - 1].high - day.iloc[index - 1].close) / prev_span
        long_upper_wicks = upper_wick >= .45 and prev_upper_wick >= .45
        high_volume_no_high = bool(float(row.vol_ratio or 0) >= 1.5 and row.high <= previous.high.max())
        below_vwap = bool(row.close < row.vwap)
        below_15m_low = bool(row.close < previous.low.min())
        spy_row = spy[(spy.date == row.date) & (spy.ts == row.ts)]
        market_drop = bool(not spy_row.empty and float(spy_row.iloc[0].close / spy_row.iloc[0].open - 1) <= -.004)
        reasons = [name for name, hit in (("连续长上影", long_upper_wicks), ("放量不创新高", high_volume_no_high),
                                         ("跌破VWAP", below_vwap), ("跌破前15分钟低点", below_15m_low), ("大盘5m跳水", market_drop)) if hit]
        if len(reasons) >= 2:
            exit_row = day.iloc[index + 1]
            return {"exit_time": int(exit_row.ts), "exit": float(exit_row.open), "exit_reason": " + ".join(reasons)}
    exit_row = day.iloc[-1]
    return {"exit_time": int(exit_row.ts), "exit": float(exit_row.close), "exit_reason": "收盘前退出"}


def portfolio(candidates: list[dict], spy: pd.DataFrame) -> list[dict]:
    active: list[dict] = []
    output: list[dict] = []
    for entry_time, group in pd.DataFrame(candidates).groupby("entry_time", sort=True):
        active = [trade for trade in active if trade["exit_time"] > int(entry_time)]
        active_symbols = {trade["symbol"] for trade in active}
        for candidate in sorted(group.to_dict("records"), key=lambda x: x["score"], reverse=True):
            if len(active) >= MAX_OPEN:
                break
            if candidate["symbol"] in active_symbols:
                continue
            exit_info = exit_trade(candidate, spy)
            if not exit_info:
                continue
            net_r = ((float(exit_info["exit"]) / float(candidate["entry"]) - 1) * 10_000 - COST_BPS) / 100
            trade = {key: candidate[key] for key in ("symbol", "date", "signal_time", "entry_time", "entry", "score")}
            trade.update(exit_info, side="LONG", net_r=net_r)
            active.append(trade); active_symbols.add(trade["symbol"]); output.append(trade)
    return output


def main() -> None:
    # The analysis is cache-first: a partially downloaded new day must never
    # create a different 30-day sample per symbol.  It will advance once every
    # instrument has that complete session locally.
    common_dates: set[str] | None = None
    for symbol in DEFAULT_SYMBOLS:
        dates = {value for path in CACHE_DIR.glob(f"{symbol}_*_1m.json")
                 if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value := path.name[len(symbol) + 1:-8])}
        if not dates:
            raise RuntimeError(f"missing cached history for {symbol}")
        common_dates = dates if common_dates is None else common_dates & dates
    selected_dates = sorted(common_dates or [])[-30:]
    if len(selected_dates) < 30:
        raise RuntimeError("fewer than 30 complete common cached sessions")
    days = [date.fromisoformat(value) for value in selected_dates]
    raw = load_market_data(OKX(settings()), DEFAULT_SYMBOLS, days)
    frames = {symbol: session_frame(symbol, rows) for symbol, rows in raw.items()}
    spy = frames["SPY-USDT-SWAP"]
    candidates = [candidate for symbol, frame in frames.items() if symbol not in ("SPY-USDT-SWAP", "QQQ-USDT-SWAP") for candidate in entry_candidates(frame)]
    trades = portfolio(candidates, spy)
    report = {"generated_at": datetime.now(UTC).isoformat(), "sessions": {"count": len(days), "start": days[0].isoformat(), "end": days[-1].isoformat()},
              "universe": list(DEFAULT_SYMBOLS), "cost_model": "14bp round trip; next 5m open fills; max five concurrent positions",
              "entry_template": "close > session VWAP and EMA20; prior 3 bars touch but do not effectively break VWAP; pullback volume <=80% prior three bars; close breaks pullback high with volume >=130% pullback mean",
              "exit_template": "next 5m open after any two of: consecutive long upper wicks, high volume/no new high, below VWAP, below prior 15m low, SPY 5m drop >=0.4%; otherwise cash-session close",
              "warning": "This formalises a discretionary template. It is a K-line replay, not a fill, L2-book or news replay.",
              "signals_before_portfolio_cap": len(candidates), "summary": metric(trades), "trades": trades}
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({"output": str(OUTPUT), "signals": len(candidates), "summary": report["summary"],
                      "first_five": [{key: row[key] for key in ("symbol", "date", "entry", "exit", "exit_reason", "net_r")} for row in trades[:5]]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
