#!/usr/bin/env python3
"""Causal 90-session study of high-volatility 5-minute turning points.

This is deliberately descriptive before it is prescriptive.  It tests whether
closed 5m candles with unusual movement and volume more often *continue* or
*reverse* over the next 15/30 minutes.  All features are known at bar close;
the entry is the following bar's open and every outcome includes 14bp
round-trip costs.  Cached OKX candles contain volume but not historical
aggressor/large-order flow, so volume is only a flow *proxy* here.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import CACHE_DIR, aggregate_bars, load_market_data, weekday_sessions  # noqa: E402
from scripts.okx_research_universe import load_symbols  # noqa: E402
from scripts.okx_return_model_research import metric  # noqa: E402

NY = ZoneInfo("America/New_York")
OUTPUT = ROOT / "data" / "okx_high_vol_turning_point_90d.json"
COST_BPS = 14.0


def build_bars(raw: dict[str, list[list[str]]]) -> pd.DataFrame:
    """Build closed regular-session 5m bars and causal event features."""
    parts: list[pd.DataFrame] = []
    # The baseline deliberately includes pre-market bars.  A 09:45 event may
    # therefore compare its movement and volume with information already
    # available from 06:00 onward, while signals themselves remain cash-hours.
    start, end = datetime.strptime("06:00", "%H:%M").time(), datetime.strptime("15:15", "%H:%M").time()
    for symbol, rows in raw.items():
        bars = aggregate_bars(rows, 5)
        if not bars:
            continue
        frame = pd.DataFrame(bars, columns=["ts", "open", "high", "low", "close", "volume", "v1", "v2", "confirm"])
        frame = frame[frame.confirm.astype(str) == "1"].copy()
        for col in ("open", "high", "low", "close", "volume"):
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
        frame["stamp"] = pd.to_datetime(frame.ts.astype("int64"), unit="ms", utc=True)
        frame["local"] = frame.stamp.dt.tz_convert(NY)
        frame = frame[(frame.local.dt.weekday < 5) & (frame.local.dt.time >= start) & (frame.local.dt.time <= end)].copy()
        if frame.empty:
            continue
        frame["symbol"] = symbol
        frame["date"] = frame.local.dt.date.astype(str)
        group = frame.groupby("date", sort=False)
        frame["ret_bps"] = group.close.pct_change() * 10_000
        # Both normalizers only see fully closed earlier bars of the same day.
        frame["prior_return_std"] = group.ret_bps.transform(lambda x: x.shift(1).rolling(20, min_periods=15).std())
        frame["prior_volume"] = group.volume.transform(lambda x: x.shift(1).rolling(20, min_periods=12).mean())
        frame["volume_ratio"] = frame.volume / frame.prior_volume
        frame["zscore"] = frame.ret_bps / frame.prior_return_std
        span = (frame.high - frame.low).clip(lower=1e-12)
        frame["close_position"] = (frame.close - frame.low) / span
        frame["upper_wick"] = (frame.high - frame.close) / span
        frame["lower_wick"] = (frame.close - frame.low) / span
        frame["entry"] = group.open.shift(-1)
        for horizon in (3, 6):
            frame[f"exit_{horizon}"] = group.close.shift(-horizon - 1)
        parts.append(frame)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def enrich_relative(bars: pd.DataFrame) -> pd.DataFrame:
    spy = bars[bars.symbol == "SPY-USDT-SWAP"][["date", "ts", "ret_bps"]].rename(columns={"ret_bps": "spy_ret_bps"})
    work = bars[~bars.symbol.isin(("SPY-USDT-SWAP", "QQQ-USDT-SWAP", "SMH-USDT-SWAP"))].merge(spy, on=["date", "ts"], how="inner")
    work["relative_ret_bps"] = work.ret_bps - work.spy_ret_bps
    work["rejection"] = ((work.ret_bps > 0) & (work.upper_wick >= .35)) | ((work.ret_bps < 0) & (work.lower_wick >= .35))
    work["acceptance"] = ((work.ret_bps > 0) & (work.close_position >= .75)) | ((work.ret_bps < 0) & (work.close_position <= .25))
    work["volume_bucket"] = pd.cut(work.volume_ratio, [-float("inf"), 1, 2, 4, float("inf")], labels=["<1x", "1-2x", "2-4x", ">=4x"])
    work["shape"] = "mixed"
    work.loc[work.rejection, "shape"] = "rejection_wick"
    work.loc[work.acceptance, "shape"] = "acceptance_close"
    clock = pd.to_datetime(work["ts"].astype("int64"), unit="ms", utc=True).dt.tz_convert(NY).dt.time
    work["session_phase"] = "midday"
    work.loc[clock < datetime.strptime("10:30", "%H:%M").time(), "session_phase"] = "opening"
    work.loc[clock >= datetime.strptime("14:30", "%H:%M").time(), "session_phase"] = "closing"
    cash_start = datetime.strptime("09:35", "%H:%M").time()
    return work[(clock >= cash_start)].dropna(subset=["zscore", "volume_ratio", "entry", "exit_3", "exit_6"])


def outcome(row: pd.Series, direction: int, horizon: int) -> float:
    return (direction * (float(row[f"exit_{horizon}"]) / float(row.entry) - 1) * 10_000 - COST_BPS) / 100


def ranked_portfolio(rows: pd.DataFrame, direction_style: str, horizon: int) -> list[dict]:
    """Apply a realisable maximum-five-concurrent-position envelope."""
    selected: list[dict] = []
    active: list[dict] = []
    for ts, group in rows.groupby("ts", sort=True):
        entry_time = int(ts) + 300_000
        active = [trade for trade in active if trade["exit_time"] > entry_time]
        slots = max(0, 5 - len(active))
        if not slots:
            continue
        score = group.zscore.abs() * group.volume_ratio.clip(upper=8)
        for _, row in group.loc[score.sort_values(ascending=False).index].head(slots).iterrows():
            impulse = 1 if row.ret_bps > 0 else -1
            direction = impulse if direction_style == "continuation" else -impulse
            trade = {"date": row.date, "symbol": row.symbol, "entry_time": entry_time,
                     "exit_time": int(ts) + (horizon + 1) * 300_000,
                     "side": "LONG" if direction > 0 else "SHORT", "net_r": outcome(row, direction, horizon)}
            active.append(trade)
            selected.append(trade)
    return selected


def descriptive(events: pd.DataFrame) -> list[dict]:
    output = []
    for (shape, volume_bucket), group in events.groupby(["shape", "volume_bucket"], observed=True):
        impulse = group.ret_bps.apply(lambda v: 1 if v > 0 else -1)
        for horizon in (3, 6):
            gross_bps = impulse * (group[f"exit_{horizon}"] / group.entry - 1) * 10_000
            output.append({"shape": str(shape), "volume_bucket": str(volume_bucket), "horizon_minutes": horizon * 5,
                           "events": len(group), "continuation_rate_before_cost_pct": round(float((gross_bps > 0).mean() * 100), 2),
                           "continuation_mean_bps_before_cost": round(float(gross_bps.mean()), 3),
                           "reversal_rate_before_cost_pct": round(float((gross_bps < 0).mean() * 100), 2),
                           "reversal_mean_bps_before_cost": round(float((-gross_bps).mean()), 3)})
    return output


def phase_summary(events: pd.DataFrame) -> list[dict]:
    """A small, non-optimised time-of-day view for the pivot question."""
    output = []
    for (phase, shape), group in events.groupby(["session_phase", "shape"], observed=True):
        impulse = group.ret_bps.apply(lambda v: 1 if v > 0 else -1)
        gross_bps = impulse * (group.exit_6 / group.entry - 1) * 10_000
        output.append({"phase": phase, "shape": shape, "events": len(group),
                       "continuation_rate_30m_before_cost_pct": round(float((gross_bps > 0).mean() * 100), 2),
                       "continuation_mean_30m_before_cost_bps": round(float(gross_bps.mean()), 3)})
    return output


def main() -> None:
    # Use only symbols fully cached through this research window, so no symbol
    # gains an accidental shorter, easier sample.
    candidates = tuple(load_symbols("historical_90d"))
    newest: dict[str, str] = {}
    for symbol in candidates:
        dates = sorted(path.name[len(symbol) + 1:-8] for path in CACHE_DIR.glob(f"{symbol}_*_1m.json"))
        if dates:
            newest[symbol] = dates[-1]
    # Research must be repeatable even when a live proxy is unavailable.  Use
    # the newest common cache date rather than silently downloading a partial
    # new day.  The report makes its end date explicit.
    cached_end = min(newest.values()) if newest else None
    if not cached_end:
        raise RuntimeError("No cached 1m research data available")
    sessions = weekday_sessions(datetime.strptime(cached_end, "%Y-%m-%d").date(), 90)
    days = [day.isoformat() for day in sessions]
    required = set(days)
    symbols = tuple(symbol for symbol in candidates if required.issubset({
        path.name[len(symbol) + 1:-8] for path in CACHE_DIR.glob(f"{symbol}_*_1m.json")
    }))
    bars = enrich_relative(build_bars(load_market_data(OKX(settings()), symbols, sessions)))
    events = bars[(bars.zscore.abs() >= 2.0) & (bars.volume_ratio >= 1.5)].copy()
    splits = {"train": set(days[:40]), "validation": set(days[40:50]), "development": set(days[50:60]),
              "final_diagnostic": set(days[60:90])}
    # Rules are set before inspecting output.  They test the economically
    # different hypotheses: failed impulse (fade) versus accepted impulse.
    variants = [
        ("exhaustion_reversal_15m", events[events.rejection], "reversion", 3),
        ("exhaustion_reversal_30m", events[events.rejection], "reversion", 6),
        ("acceptance_continuation_15m", events[events.acceptance], "continuation", 3),
        ("acceptance_continuation_30m", events[events.acceptance], "continuation", 6),
    ]
    rules = []
    for name, rows, style, horizon in variants:
        trades = ranked_portfolio(rows, style, horizon)
        result = {"name": name, "hypothesis": style, "events_before_cap": len(rows), "features": "abs(5m return z-score)>=2, volume>=1.5x prior 20 bars; candle-shape gate",
                  **{split: metric([trade for trade in trades if trade["date"] in selected_days]) for split, selected_days in splits.items()}}
        result["forward_shadow_eligible"] = all(result[split]["trades"] >= 30 and result[split]["net_r"] > 0 and (result[split]["profit_factor"] or 0) >= 1.15
                                                      for split in ("train", "validation", "development"))
        rules.append(result)
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "sessions": {"count": len(days), "start": days[0], "end": days[-1]},
              "symbols": list(symbols), "event_definition": "regular-session closed 5m bar, abs return z-score >=2 using prior 20 bars and volume >=1.5x prior 20 bars",
              "execution": "next 5m open; fixed 15m/30m close; 14bp round-trip cost; max five concurrent positions; no slippage/order-book reconstruction",
              "data_limit": "90-day candle cache proves price and volume patterns only. It cannot identify historical large orders, aggressor buy/sell flow, or hidden liquidity.",
              "descriptive_all_events": descriptive(events), "time_of_day_summary": phase_summary(events), "rules": rules,
              "promotion_gate": "train, validation and development each need >=30 trades, positive net R and PF >=1.15; final 30 sessions are diagnostic only."}
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    concise = {"sessions": report["sessions"], "symbols": len(symbols), "high_vol_events": len(events),
               "rules": [{"name": r["name"], "eligible": r["forward_shadow_eligible"], "train": r["train"], "validation": r["validation"], "development": r["development"], "final": r["final_diagnostic"]} for r in rules]}
    print(json.dumps(concise, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
