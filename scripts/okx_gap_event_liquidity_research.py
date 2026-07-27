#!/usr/bin/env python3
"""Event- and liquidity-stratified walk-forward research for relative gap fade."""
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

from scripts.okx_gap_feature_research import opening_features  # noqa: E402
from scripts.okx_gap_research import opportunities  # noqa: E402
from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import CACHE_DIR, load_market_data, weekday_sessions  # noqa: E402
from scripts.okx_research_universe import load_symbols  # noqa: E402
from scripts.okx_return_model_research import metric  # noqa: E402

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
EVENT_PATH = ROOT / "data" / "okx_event_calendar.json"
OUTPUT = ROOT / "data" / "okx_gap_event_liquidity_90d.json"


def event_sets() -> tuple[set[str], set[tuple[str, str]]]:
    data = json.loads(EVENT_PATH.read_text())
    macro = set()
    earnings: set[tuple[str, str]] = set()
    for event in data.get("macro_events", []):
        try:
            macro.add(datetime.fromisoformat(event["scheduled_at"]).astimezone(NY).date().isoformat())
        except (KeyError, ValueError):
            pass
    for event in data.get("earnings", []):
        if event.get("event_type") != "EARNINGS_REPORTED":
            continue
        date = str(event.get("scheduled_at", ""))[:10]
        symbol = str(event.get("symbol", ""))
        if len(date) == 10 and symbol:
            for offset in (-1, 0, 1):
                value = datetime.fromisoformat(date).date() + timedelta(days=offset)
                earnings.add((symbol, value.isoformat()))
    return macro, earnings


def trades(rows: pd.DataFrame) -> list[dict]:
    result = []
    for _, group in rows.groupby("entry_time", sort=True):
        for _, row in group.reindex(group.relative_gap.abs().sort_values(ascending=False).index).head(5).iterrows():
            direction = -1 if row.relative_gap > 0 else 1
            ret = direction * (float(row.exit_150) / float(row.entry) - 1) * 10_000 - 14
            result.append({"date": row.date, "entry_time": int(row.entry_time),
                           "exit_time": int(row.entry_time + 150 * 60_000), "net_r": ret / 100})
    return result


def evaluate(rows: pd.DataFrame, days: list[str], name: str, mask: pd.Series) -> dict:
    value = trades(rows[mask])
    periods = {"train": days[:40], "validation": days[40:50], "development": days[50:60], "final_diagnostic": days[60:90]}
    return {"name": name, **{key: metric([trade for trade in value if trade["date"] in target]) for key, target in periods.items()}}


def main() -> None:
    end = datetime.now(NY).date() - timedelta(days=1)
    days = [date.isoformat() for date in weekday_sessions(end, 90)]
    requested = load_symbols("historical_90d")
    # Do not silently treat a newly listed token's missing pre-listing history
    # as a string of zero-signal sessions.  The resumable collector will add it
    # in later reruns once coverage reaches this same threshold.
    symbols = tuple(symbol for symbol in requested if len(list(CACHE_DIR.glob(f"{symbol}_*_1m.json"))) >= len(days))
    if "SPY-USDT-SWAP" not in symbols or "QQQ-USDT-SWAP" not in symbols:
        raise RuntimeError("benchmark history is incomplete")
    raw = load_market_data(OKX(settings()), symbols, [datetime.fromisoformat(day).date() for day in days])
    rows = opportunities(raw).merge(opening_features(raw), on=["symbol", "date"], how="left").dropna().copy()
    rows["relative_gap"] = rows.gap_bps - rows.spy_gap
    rows["relative_first5"] = rows.first5_bps - rows.spy_first5
    rows["relative_previous"] = rows.previous_day_bps - rows.spy_previous_day
    rows["relative_gap_rank"] = rows.groupby("date").relative_gap.transform(lambda x: x.abs().rank(pct=True))
    rows["ticker"] = rows.symbol.str.split("-", n=1).str[0]
    macro, earnings = event_sets()
    rows["macro_day"] = rows.date.isin(macro)
    rows["earnings_window"] = [(ticker, date) in earnings for ticker, date in zip(rows.ticker, rows.date)]
    base = (rows.relative_gap.abs() >= 100) & ((rows.relative_gap * rows.relative_first5 < 0) |
                                                 (rows.relative_gap * rows.relative_previous <= 0)) & rows.exit_150.notna()
    variants = [
        ("base", base),
        ("top_relative_quartile", base & (rows.relative_gap_rank >= .75)),
        ("no_macro", base & ~rows.macro_day),
        ("no_earnings_window", base & ~rows.earnings_window),
        ("top_rank_no_macro", base & (rows.relative_gap_rank >= .75) & ~rows.macro_day),
        ("top_rank_no_event", base & (rows.relative_gap_rank >= .75) & ~rows.macro_day & ~rows.earnings_window),
        ("top_rank_liquid_open", base & (rows.relative_gap_rank >= .75) & (rows.open_volume_ratio >= 1.0)),
    ]
    results = [evaluate(rows, days, name, mask) for name, mask in variants]
    selected = max(results, key=lambda value: ((value["train"]["profit_factor"] or 0), value["train"]["expectancy_r"]))
    report = {"generated_at": datetime.now(UTC).isoformat(), "symbols": list(symbols), "opportunities": len(rows),
              "event_counts": {"macro_dates": len(macro), "earnings_windows": len(earnings)},
              "cost_model": "14bp round trip, 09:35 entry / 12:00 exit", "selection": "selected only by first 40 sessions",
              "selected_by_train": selected, "results": results,
              "warning": "Final segment is diagnostic only; event labels are risk filters, not a directional signal."}
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
