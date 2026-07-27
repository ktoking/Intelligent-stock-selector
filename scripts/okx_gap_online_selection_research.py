#!/usr/bin/env python3
"""Causal online selection test for a small, predeclared opening-gap family.

Each session chooses at most one rule using only prior-session results.  It is
an abstaining selector: when the trailing evidence is weak it makes no trade.
This deliberately tests whether regime adaptation improves on a fixed gap fade
without looking at the selected session's outcome.
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

from scripts.okx_gap_research import opportunities  # noqa: E402
from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import CACHE_DIR, load_market_data, weekday_sessions  # noqa: E402
from scripts.okx_research_universe import load_symbols  # noqa: E402
from scripts.okx_return_model_research import metric  # noqa: E402

NY = ZoneInfo("America/New_York")
OUTPUT = ROOT / "data" / "okx_gap_online_selection_90d.json"


def daily_trades(rows: pd.DataFrame, *, horizon: int, threshold: int, confirm: str, cost_bps: int) -> list[dict]:
    relative = rows.gap_bps - rows.spy_gap
    first5 = rows.first5_bps - rows.spy_first5
    previous = rows.previous_day_bps - rows.spy_previous_day
    if confirm == "first5":
        confirmed = relative * first5 < 0
    elif confirm == "previous":
        confirmed = relative * previous <= 0
    else:
        confirmed = (relative * first5 < 0) | (relative * previous <= 0)
    selected = rows[confirmed & (relative.abs() >= threshold) & rows[f"exit_{horizon}"].notna()].copy()
    selected["relative"] = relative.loc[selected.index]
    trades = []
    for _, group in selected.groupby("entry_time", sort=True):
        for _, row in group.reindex(group.relative.abs().sort_values(ascending=False).index).head(5).iterrows():
            direction = -1 if row.relative > 0 else 1
            gross_bps = direction * (float(row[f"exit_{horizon}"]) / float(row.entry) - 1) * 10_000
            trades.append({"date": row.date, "symbol": row.symbol, "entry_time": int(row.entry_time),
                           "exit_time": int(row.entry_time + horizon * 60_000),
                           "rule": (horizon, threshold, confirm), "net_r": (gross_bps - cost_bps) / 100})
    return trades


def score(trades: list[dict]) -> tuple[float, float, int]:
    values = [float(trade["net_r"]) for trade in trades]
    if len(values) < 15:
        return (-float("inf"), -float("inf"), len(values))
    win = sum(value for value in values if value > 0)
    loss = -sum(value for value in values if value < 0)
    pf = win / loss if loss else 9.99
    # Prefer a positive, cost-adjusted expectation before a high PF from a
    # handful of oversized wins.  The count remains a hard eligibility gate.
    return (sum(values) / len(values), pf, len(values))


def simulate(rows: pd.DataFrame, days: list[str], *, lookback: int, cost_bps: int) -> list[dict]:
    rules = [(horizon, threshold, confirm)
             for horizon in (30, 60, 90, 120, 150)
             for threshold in (75, 100, 125)
             for confirm in ("first5", "previous", "either")]
    by_rule = {rule: daily_trades(rows, horizon=rule[0], threshold=rule[1], confirm=rule[2], cost_bps=cost_bps)
               for rule in rules}
    result = []
    for index, day in enumerate(days):
        if index < lookback:
            continue
        history_days = set(days[index - lookback:index])
        ranked = []
        for rule, trades in by_rule.items():
            history = [trade for trade in trades if trade["date"] in history_days]
            expectation, pf, count = score(history)
            if expectation > 0 and pf >= 1.08:
                ranked.append((expectation, pf, count, rule))
        if not ranked:
            continue
        # Tie-breaking is deterministic and does not inspect the current day.
        _, _, _, chosen = max(ranked, key=lambda item: (item[0], item[1], item[2], item[3]))
        result.extend({**trade, "lookback": lookback} for trade in by_rule[chosen] if trade["date"] == day)
    return result


def main() -> None:
    end = datetime.now(NY).date() - timedelta(days=1)
    sessions = weekday_sessions(end, 90)
    days = [day.isoformat() for day in sessions]
    symbols = tuple(symbol for symbol in load_symbols("historical_90d")
                    if len(list(CACHE_DIR.glob(f"{symbol}_*_1m.json"))) >= len(sessions))
    rows = opportunities(load_market_data(OKX(settings()), symbols, sessions)).dropna()
    splits = {"validation": set(days[40:50]), "development": set(days[50:60]), "final_diagnostic": set(days[60:90])}
    results = []
    for lookback in (10, 20, 30):
        for cost_bps in (14, 21):
            trades = simulate(rows, days, lookback=lookback, cost_bps=cost_bps)
            item = {"lookback_sessions": lookback, "round_trip_cost_bps": cost_bps,
                    **{name: metric([trade for trade in trades if trade["date"] in target])
                       for name, target in splits.items()}}
            item["eligible"] = all(item[split]["trades"] >= 25 and item[split]["net_r"] > 0
                                   and (item[split]["profit_factor"] or 0) >= 1.15
                                   for split in ("validation", "development"))
            results.append(item)
    eligible = [item for item in results if item["eligible"]]
    OUTPUT.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(), "symbols": list(symbols),
        "method": "causal daily rule selection; 45 fixed candidates; abstains without 15 prior trades and positive trailing expectancy",
        "results": results, "eligible": eligible,
        "warning": "Final diagnostic remains non-promotional because these dates have already been inspected.",
    }, ensure_ascii=False, indent=2))
    print(json.dumps({"eligible": eligible, "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
