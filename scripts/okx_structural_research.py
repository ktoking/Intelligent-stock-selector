#!/usr/bin/env python3
"""Research non-breakout structures: trend pullback and failed-breakout reversal."""
from __future__ import annotations

import json
import sys
from bisect import bisect_left
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import (  # noqa: E402
    DEFAULT_SYMBOLS, StrategyConfig, _metrics, aggregate_bars, load_market_data,
    simulate_exit, stats, weekday_sessions,
)
from scripts.okx_trade_replay import closed_chronological  # noqa: E402

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
OUTPUT = ROOT / "data" / "okx_structural_research_90d.json"


def allowed(stamp: int) -> bool:
    local = datetime.fromtimestamp(stamp / 1000, UTC).astimezone(NY)
    return local.weekday() < 5 and (local.hour, local.minute) >= (10, 0) and (local.hour, local.minute) < (15, 0)


def bases_for_symbol(inst_id: str, source: list[list[str]]) -> tuple[list[dict[str, Any]], list[list[str]], list[list[str]]]:
    one = closed_chronological(source)
    five, ten = aggregate_bars(one, 5), aggregate_bars(one, 10)
    one_stamps = [int(row[0]) for row in one]
    five_stamps = [int(row[0]) for row in five]
    bases = []
    last_by_structure = {"failed_breakout": 0, "trend_pullback": 0}
    for ten_index in range(22, len(ten)):
        decision = int(ten[ten_index][0]) + 600_000
        if not allowed(decision):
            continue
        m10 = _metrics(ten, ten_index)
        if not m10:
            continue
        previous = ten[ten_index - 6:ten_index]
        prior_high = max(float(row[2]) for row in previous)
        prior_low = min(float(row[3]) for row in previous)
        current = ten[ten_index]
        high, low, close = float(current[2]), float(current[3]), float(current[4])
        structures: list[tuple[str, str, float]] = []
        if high > prior_high and close < prior_high:
            structures.append(("failed_breakout", "SHORT", (high - prior_high) / m10["atr14"]))
        if low < prior_low and close > prior_low:
            structures.append(("failed_breakout", "LONG", (prior_low - low) / m10["atr14"]))
        trend_side = "LONG" if m10["ema9"] > m10["ema21"] and close > m10["vwap20"] else (
            "SHORT" if m10["ema9"] < m10["ema21"] and close < m10["vwap20"] else ""
        )
        five_index = bisect_left(five_stamps, decision)
        if five_index >= len(five):
            continue
        m5 = _metrics(five, five_index)
        if not m5:
            continue
        row5 = five[five_index]
        open5, high5, low5, close5 = map(float, row5[1:5])
        if trend_side == "LONG" and low5 <= m5["ema9"] + 0.25 * m5["atr14"] and close5 > m5["ema9"] and close5 > open5:
            structures.append(("trend_pullback", "LONG", (close5 - low5) / m5["atr14"]))
        if trend_side == "SHORT" and high5 >= m5["ema9"] - 0.25 * m5["atr14"] and close5 < m5["ema9"] and close5 < open5:
            structures.append(("trend_pullback", "SHORT", (high5 - close5) / m5["atr14"]))
        confirm_time = int(row5[0]) + 300_000
        confirm_start = bisect_left(one_stamps, confirm_time)
        for structure, side, quality in structures:
            if decision - last_by_structure[structure] < 30 * 60_000:
                continue
            confirmation = None
            for index in range(confirm_start, min(confirm_start + 5, len(one) - 1)):
                row = one[index]
                directional = float(row[4]) > float(row[1]) if side == "LONG" else float(row[4]) < float(row[1])
                trigger = float(row[4]) > high5 if side == "LONG" else float(row[4]) < low5
                if directional and (trigger or structure == "failed_breakout"):
                    confirmation = index
                    break
            if confirmation is None or confirmation + 1 >= len(one):
                continue
            entry_index = confirmation + 1
            entry_time = int(one[entry_index][0])
            if not allowed(entry_time):
                continue
            local = datetime.fromtimestamp(entry_time / 1000, UTC).astimezone(NY)
            bases.append({
                "inst_id": inst_id, "structure": structure, "side": side,
                "date": local.date().isoformat(), "entry_time": entry_time,
                "entry_index": entry_index, "entry_price": float(one[entry_index][1]),
                "atr5": m5["atr14"], "volume10": m10["volume_ratio"],
                "volume5": m5["volume_ratio"], "quality": quality,
            })
            last_by_structure[structure] = entry_time
    return bases, one, five


def select_portfolio(rows: list[dict[str, Any]], days: list[str], minimum_volume: float) -> list[dict[str, Any]]:
    chosen = []
    for day in days:
        daily = [row for row in rows if row["date"] == day and row["volume10"] >= minimum_volume]
        daily.sort(key=lambda row: row["volume10"] + row["volume5"] + row["quality"], reverse=True)
        chosen.extend(sorted(daily[:5], key=lambda row: row["entry_time"]))
    return chosen


def main() -> None:
    end = datetime.now(NY).date() - timedelta(days=1)
    sessions = weekday_sessions(end, 90)
    days = [day.isoformat() for day in sessions]
    data = load_market_data(OKX(settings()), DEFAULT_SYMBOLS, sessions)
    bases, prepared = [], {}
    for index, (symbol, rows) in enumerate(data.items(), 1):
        symbol_bases, one, five = bases_for_symbol(symbol, rows)
        bases.extend(symbol_bases)
        prepared[symbol] = (one, five)
        print(f"prepared {index}/{len(data)} {symbol}: {len(symbol_bases)}", flush=True)
    train_days, validation_days = days[:40], days[40:50]
    development_days, final_days = days[50:60], days[60:90]
    leaderboard = []
    for structure in ("failed_breakout", "trend_pullback"):
        structural = [row for row in bases if row["structure"] == structure]
        for minimum_volume in (0.3, 0.7, 1.0, 1.5):
            for stop_multiplier in (1.0, 1.5, 2.0):
                for target_r in (1.5, 2.0, 3.0):
                    for holding in (30, 60, 120):
                        cfg = StrategyConfig(
                            f"{structure}_{minimum_volume}_{stop_multiplier}_{target_r}_{holding}",
                            round_trip_cost_bps=14, max_holding_minutes=holding,
                            stop_atr_multiplier=stop_multiplier, min_stop_bps=25,
                            breakeven_r=100, scale_target_r=target_r, scale_fraction=1.0,
                            trail_stop_r=100, enable_5m_reversal=False,
                        )
                        priced = []
                        for base in structural:
                            one, five = prepared[base["inst_id"]]
                            stop_distance = max(base["atr5"] * stop_multiplier, base["entry_price"] * 0.0025)
                            result = simulate_exit(base["side"], base["entry_price"], base["entry_index"],
                                                   one, five, stop_distance, cfg)
                            priced.append({**base, **result})
                        train = stats(select_portfolio(priced, train_days, minimum_volume))
                        validation = stats(select_portfolio(priced, validation_days, minimum_volume))
                        development = stats(select_portfolio(priced, development_days, minimum_volume))
                        if (train["trades"] >= 100 and train["net_r"] > 0 and (train["profit_factor"] or 0) > 1
                                and validation["trades"] >= 25 and validation["net_r"] > 0 and (validation["profit_factor"] or 0) > 1.1
                                and development["trades"] >= 25 and development["net_r"] > 0 and (development["profit_factor"] or 0) > 1.1):
                            leaderboard.append({"config": asdict(cfg), "structure": structure,
                                                "minimum_volume": minimum_volume, "train": train,
                                                "validation": validation, "development": development,
                                                "priced": priced})
    leaderboard.sort(key=lambda row: (min(row["validation"]["profit_factor"], row["development"]["profit_factor"]),
                                      row["validation"]["net_r"] + row["development"]["net_r"]), reverse=True)
    selected = leaderboard[0] if leaderboard else None
    final = stats(select_portfolio(selected["priced"], final_days, selected["minimum_volume"])) if selected else None
    compact = []
    for row in leaderboard[:20]:
        compact.append({key: value for key, value in row.items() if key != "priced"})
    report = {
        "generated_at": datetime.now(UTC).isoformat(), "base_candidates": len(bases),
        "split": {"train": [train_days[0], train_days[-1]], "validation": [validation_days[0], validation_days[-1]],
                  "development": [development_days[0], development_days[-1]],
                  "contaminated_diagnostic": [final_days[0], final_days[-1]]},
        "selection_gate": "train n>=100 PF>1; validation/development each n>=25 PF>1.1 and net positive",
        "selected": ({key: value for key, value in selected.items() if key != "priced"} if selected else None),
        "final_diagnostic": final,
        "warning": "last 30 days were already inspected; this is structural diagnosis, not deployment evidence",
        "leaderboard": compact,
    }
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
