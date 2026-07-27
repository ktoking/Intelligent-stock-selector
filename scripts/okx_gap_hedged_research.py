#!/usr/bin/env python3
"""Test whether relative-gap fade survives as an equal-notional SPY-hedged spread."""
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

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
OUTPUT = ROOT / "data" / "okx_gap_hedged_90d.json"


def portfolio(rows: pd.DataFrame) -> list[dict]:
    output = []
    for _, group in rows.groupby("entry_time", sort=True):
        for _, row in group.reindex(group.relative_gap.abs().sort_values(ascending=False).index).head(5).iterrows():
            side = -1 if row.relative_gap > 0 else 1  # fade stock; opposite SPY hedge
            stock = (float(row.exit_150) / float(row.entry) - 1) * 10_000
            spy = (float(row.spy_exit_150) / float(row.spy_entry) - 1) * 10_000
            # Two market-order round trips: deliberately charge twice the base cost.
            net_bps = side * stock - side * spy - 28
            output.append({"date": row.date, "entry_time": int(row.entry_time),
                           "exit_time": int(row.entry_time + 150 * 60_000), "net_r": net_bps / 100})
    return output


def report(rows: pd.DataFrame, days: list[str], name: str, mask: pd.Series) -> dict:
    value = portfolio(rows[mask])
    periods = {"train": days[:40], "validation": days[40:50], "development": days[50:60], "final_diagnostic": days[60:90]}
    return {"name": name, **{key: metric([trade for trade in value if trade["date"] in target]) for key, target in periods.items()}}


def main() -> None:
    end = datetime.now(NY).date() - timedelta(days=1)
    sessions = weekday_sessions(end, 90); days = [day.isoformat() for day in sessions]
    requested = load_symbols("historical_90d")
    symbols = tuple(symbol for symbol in requested if len(list(CACHE_DIR.glob(f"{symbol}_*_1m.json"))) >= len(days))
    raw = load_market_data(OKX(settings()), symbols, sessions)
    rows = opportunities(raw).merge(opening_features(raw), on=["symbol", "date"], how="left").dropna().copy()
    # The source attaches SPY gap fields but not its entry/exit prices; recover
    # those from the same timestamped opportunity records before filtering them.
    spy_rows = []
    # Reconstruct SPY's 09:35-to-12:00 return with the exact same function by
    # looking at raw 5m opportunities via an explicit temporary call.
    from scripts.okx_gap_research import aggregate_bars  # local import avoids public API changes
    bars = aggregate_bars(raw["SPY-USDT-SWAP"], 5)
    frame = pd.DataFrame(bars, columns=["ts","open","high","low","close","volume","v1","v2","confirm"])
    frame["stamp"] = pd.to_datetime(frame.ts.astype("int64"), unit="ms", utc=True)
    frame["date"] = frame.stamp.dt.tz_convert(NY).dt.date.astype(str); frame["clock"] = frame.stamp.dt.tz_convert(NY).dt.strftime("%H:%M")
    for date, part in frame.groupby("date"):
        part = part.set_index("clock")
        if "09:35" in part.index and "12:00" in part.index:
            spy_rows.append({"date": date, "spy_entry": float(part.loc["09:35"].open), "spy_exit_150": float(part.loc["12:00"].close)})
    rows = rows.merge(pd.DataFrame(spy_rows), on="date", how="inner")
    rows["relative_gap"] = rows.gap_bps - rows.spy_gap
    rows["relative_first5"] = rows.first5_bps - rows.spy_first5
    rows["relative_previous"] = rows.previous_day_bps - rows.spy_previous_day
    rows["relative_gap_rank"] = rows.groupby("date").relative_gap.transform(lambda value: value.abs().rank(pct=True))
    base = (rows.relative_gap.abs() >= 100) & ((rows.relative_gap * rows.relative_first5 < 0) |
                                                 (rows.relative_gap * rows.relative_previous <= 0)) & rows.exit_150.notna()
    results = [report(rows, days, name, mask) for name, mask in (
        ("hedged_base", base), ("hedged_top_rank", base & (rows.relative_gap_rank >= .75)),
        ("hedged_top_rank_liquid", base & (rows.relative_gap_rank >= .75) & (rows.open_volume_ratio >= 1.0)),
    )]
    selected = max(results, key=lambda value: ((value["train"]["profit_factor"] or 0), value["train"]["expectancy_r"]))
    OUTPUT.write_text(json.dumps({"generated_at": datetime.now(UTC).isoformat(), "symbols": list(symbols),
        "cost_model": "28bp round trip: stock leg + equal-notional SPY hedge", "selected_by_train": selected,
        "results": results, "warning": "research only; final diagnostic dates were previously inspected"}, ensure_ascii=False, indent=2))
    print(json.dumps({"selected_by_train": selected, "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
