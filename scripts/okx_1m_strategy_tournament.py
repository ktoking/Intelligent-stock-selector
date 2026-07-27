#!/usr/bin/env python3
"""Compare pre-specified one-minute signal families on the same 90-session data.

This is deliberately a *research-only* tournament.  A result can qualify a
family for a forward Demo shadow period, never for direct execution.  All
families pay the same conservative 14 bp round-trip cost and use the same
chronological validation/development/final partitions.
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import DEFAULT_SYMBOLS, load_market_data, weekday_sessions  # noqa: E402
from scripts.okx_return_model_research import metric  # noqa: E402
from scripts.okx_trade_replay import closed_chronological  # noqa: E402

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
OUTPUT = ROOT / "data" / "okx_1m_strategy_tournament_90d.json"
TRADE_SYMBOLS = tuple(x for x in DEFAULT_SYMBOLS if not x.startswith(("SPY-", "QQQ-")))
COST_BPS = 14.0


def local(row: list[str]) -> datetime:
    return datetime.fromtimestamp(int(row[0]) / 1000, UTC).astimezone(NY)


def session_rows(rows: list[list[str]]) -> dict[str, list[list[str]]]:
    """Regular-session bars only; an entry always uses the next bar's open."""
    result: dict[str, list[list[str]]] = defaultdict(list)
    for row in closed_chronological(rows):
        stamp = local(row)
        if stamp.weekday() < 5 and (stamp.hour, stamp.minute) >= (9, 30) and (stamp.hour, stamp.minute) < (15, 30):
            result[stamp.date().isoformat()].append(row)
    return result


def vwap(values: list[list[str]], end: int) -> float:
    window = values[: end + 1]
    volume = sum(float(row[5]) for row in window)
    if volume <= 0:
        return 0.0
    return sum(((float(row[2]) + float(row[3]) + float(row[4])) / 3) * float(row[5]) for row in window) / volume


def rsi(closes: list[float], end: int, period: int = 14) -> float | None:
    if end < period:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(end - period + 1, end + 1)]
    up, down = sum(max(x, 0) for x in changes) / period, sum(max(-x, 0) for x in changes) / period
    if down == 0:
        return 100.0
    return 100 - 100 / (1 + up / down)


def kdj(closes: list[float], highs: list[float], lows: list[float], end: int) -> tuple[float, float] | None:
    if end < 10:
        return None
    k = d = 50.0
    for i in range(max(8, end - 20), end + 1):
        hi, lo = max(highs[i - 8:i + 1]), min(lows[i - 8:i + 1])
        rsv = 50.0 if hi == lo else 100 * (closes[i] - lo) / (hi - lo)
        k = 2 * k / 3 + rsv / 3
        d = 2 * d / 3 + k / 3
    return k, d


def exit_trade(day: list[list[str]], side: int, entry_index: int, stop_bps: float, horizon: int) -> dict:
    entry = float(day[entry_index][1])
    last = min(len(day) - 1, entry_index + horizon)
    stop = entry * (1 - side * stop_bps / 10_000)
    price, reason, exit_index = float(day[last][4]), "time", last
    for index in range(entry_index, last + 1):
        high, low = float(day[index][2]), float(day[index][3])
        if (side > 0 and low <= stop) or (side < 0 and high >= stop):
            price, reason, exit_index = stop, "stop", index
            break
    ret = side * (price / entry - 1) * 10_000 - COST_BPS
    return {"entry_time": int(day[entry_index][0]), "exit_time": int(day[exit_index][0]) + 60_000,
            "side": "LONG" if side > 0 else "SHORT", "net_r": ret / stop_bps, "exit_reason": reason}


def generate(day: list[list[str]], family: str) -> list[dict]:
    closes = [float(x[4]) for x in day]; highs = [float(x[2]) for x in day]; lows = [float(x[3]) for x in day]
    volumes = [float(x[5]) for x in day]; trades: list[dict] = []; next_allowed = 20
    cumulative_volume = cumulative_value = 0.0
    vwaps: list[float] = []
    for row, volume in zip(day, volumes):
        cumulative_volume += volume
        cumulative_value += ((float(row[2]) + float(row[3]) + float(row[4])) / 3) * volume
        vwaps.append(cumulative_value / cumulative_volume if cumulative_volume > 0 else 0.0)
    for i in range(20, len(day) - 31):
        if i < next_allowed:
            continue
        stamp = local(day[i])
        side = 0
        avg_vol = sum(volumes[i - 20:i]) / 20
        if family == "orb_vwap":
            # First 15 minutes form the opening range; use 1m close to confirm a break.
            if (stamp.hour, stamp.minute) < (9, 45) or (stamp.hour, stamp.minute) >= (10, 30):
                continue
            opening_high, opening_low = max(highs[:15]), min(lows[:15])
            price_vwap = vwaps[i]
            if closes[i] > opening_high and closes[i] > price_vwap and volumes[i] >= 1.5 * avg_vol:
                side = 1
            elif closes[i] < opening_low and closes[i] < price_vwap and volumes[i] >= 1.5 * avg_vol:
                side = -1
            stop_bps, horizon = 45, 30
        elif family == "vwap_reversion":
            # Enter only after an extreme move starts reverting toward VWAP.
            price_vwap = vwaps[i]
            if price_vwap <= 0:
                continue
            past_vwaps = vwaps[i - 20:i]
            if any(value <= 0 for value in past_vwaps):
                continue
            deviations = [closes[j] / value - 1 for j, value in zip(range(i - 20, i), past_vwaps)]
            sigma = math.sqrt(sum((x - sum(deviations) / len(deviations)) ** 2 for x in deviations) / len(deviations))
            z = (closes[i] / price_vwap - 1) / max(sigma, 1e-6)
            value_rsi = rsi(closes, i)
            if z <= -2.0 and closes[i] > closes[i - 1] and value_rsi is not None and value_rsi < 42:
                side = 1
            elif z >= 2.0 and closes[i] < closes[i - 1] and value_rsi is not None and value_rsi > 58:
                side = -1
            stop_bps, horizon = 38, 20
        elif family == "kdj_vwap_reversal":
            now, prev = kdj(closes, highs, lows, i), kdj(closes, highs, lows, i - 1)
            price_vwap = vwaps[i]
            if now and prev and prev[0] <= prev[1] and now[0] > now[1] and now[0] < 30 and closes[i] < price_vwap:
                side = 1
            elif now and prev and prev[0] >= prev[1] and now[0] < now[1] and now[0] > 70 and closes[i] > price_vwap:
                side = -1
            stop_bps, horizon = 42, 25
        elif family == "volume_breakout":
            high20, low20 = max(highs[i - 20:i]), min(lows[i - 20:i])
            if closes[i] > high20 and volumes[i] >= 2.0 * avg_vol:
                side = 1
            elif closes[i] < low20 and volumes[i] >= 2.0 * avg_vol:
                side = -1
            stop_bps, horizon = 50, 30
        else:
            raise ValueError(family)
        if not side:
            continue
        trade = exit_trade(day, side, i + 1, stop_bps, horizon)
        trade["family"] = family
        trades.append(trade)
        next_allowed = i + 6  # same symbol cooldown; portfolio cap is applied below
    return trades


def portfolio(trades: list[dict]) -> list[dict]:
    """No more than five concurrent positions and never duplicate one symbol."""
    active: list[dict] = []; chosen: list[dict] = []
    for row in sorted(trades, key=lambda x: x["entry_time"]):
        active = [x for x in active if x["exit_time"] > row["entry_time"]]
        if len(active) >= 5 or any(x["symbol"] == row["symbol"] for x in active):
            continue
        active.append(row); chosen.append(row)
    return chosen


def gate(parts: dict[str, dict], style: str) -> tuple[bool, list[str]]:
    # Win rate is explicitly part of the gate, but it cannot override costs or drawdown.
    minimum_win = 42.0 if style == "trend" else 52.0
    notes = []
    for split in ("validation", "development"):
        item = parts[split]
        if item["trades"] < 35: notes.append(f"{split}:样本<{35}")
        if item["win_rate"] < minimum_win: notes.append(f"{split}:胜率<{minimum_win}%")
        if (item["profit_factor"] or 0) < 1.2: notes.append(f"{split}:PF<1.2")
        if item["expectancy_r"] < .08: notes.append(f"{split}:期望<0.08R")
    return not notes, notes


def main() -> None:
    end = datetime.now(NY).date() - timedelta(days=1)
    sessions = weekday_sessions(end, 90); days = [x.isoformat() for x in sessions]
    raw = load_market_data(OKX(settings()), DEFAULT_SYMBOLS, sessions)
    families = {"orb_vwap": "trend", "vwap_reversion": "reversion", "kdj_vwap_reversal": "reversion", "volume_breakout": "trend"}
    results = []
    for family, style in families.items():
        all_trades = []
        for symbol in TRADE_SYMBOLS:
            for date, bars in session_rows(raw[symbol]).items():
                for trade in generate(bars, family):
                    trade.update(symbol=symbol, date=date)
                    all_trades.append(trade)
        all_trades = portfolio(all_trades)
        splits = {"validation": days[40:50], "development": days[50:60], "final_diagnostic": days[60:90]}
        parts = {name: metric([x for x in all_trades if x["date"] in target]) for name, target in splits.items()}
        passed, reasons = gate(parts, style)
        results.append({"family": family, "style": style, "parameters": "pre-specified; no per-family optimization",
                        "validation": parts["validation"], "development": parts["development"],
                        "final_diagnostic": parts["final_diagnostic"], "forward_demo_eligible": passed,
                        "rejection_reasons": reasons})
    results.sort(key=lambda x: (x["forward_demo_eligible"], min(x["validation"]["profit_factor"] or 0, x["development"]["profit_factor"] or 0), x["validation"]["expectancy_r"] + x["development"]["expectancy_r"]), reverse=True)
    report = {"generated_at": datetime.now(UTC).isoformat(), "sessions": {"count": len(days), "start": days[0], "end": days[-1]},
              "cost_model": "14 bps round trip (fees plus slippage proxy)", "portfolio": "max five concurrent; one per symbol",
              "gate": "validation and development: n>=35; PF>=1.2; expectancy>=0.08R; trend WR>=42%, reversion WR>=52%",
              "promotion": "eligibility means Demo shadow only; require 10 new sessions with positive net R and PF>=1.15 before Demo execution",
              "results": results}
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
