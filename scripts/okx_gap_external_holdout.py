#!/usr/bin/env python3
"""Evaluate frozen gap-shadow rules on the extra 10-session historical cohort.

The first ten sessions of the 100-session cache were not used by the previous
90-session research.  This is an external historical check, not parameter
selection: horizons, confirmation, ranking and costs are frozen.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_gap_research import opportunities  # noqa: E402
from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import CACHE_DIR, load_market_data, weekday_sessions  # noqa: E402
from scripts.okx_research_universe import load_symbols  # noqa: E402
from scripts.okx_return_model_research import metric  # noqa: E402

NY = ZoneInfo("America/New_York")
OUTPUT = ROOT / "data" / "okx_gap_external_holdout_100d.json"


def frozen_trades(rows, *, horizon: int, cost_bps: int) -> list[dict]:
    relative = rows.gap_bps - rows.spy_gap
    first5 = rows.first5_bps - rows.spy_first5
    previous = rows.previous_day_bps - rows.spy_previous_day
    confirmed = (relative * first5 < 0) | (relative * previous <= 0)
    selected = rows[confirmed & (relative.abs() >= 100) & rows[f"exit_{horizon}"].notna()].copy()
    selected["relative"] = relative.loc[selected.index]
    output = []
    for _, group in selected.groupby("entry_time", sort=True):
        for _, row in group.reindex(group.relative.abs().sort_values(ascending=False).index).head(5).iterrows():
            direction = -1 if row.relative > 0 else 1
            gross_bps = direction * (float(row[f"exit_{horizon}"]) / float(row.entry) - 1) * 10_000
            output.append({"date": row.date, "symbol": row.symbol, "entry_time": int(row.entry_time),
                           "exit_time": int(row.entry_time + horizon * 60_000), "net_r": (gross_bps - cost_bps) / 100})
    return output


def main() -> None:
    end = datetime.now(NY).date() - timedelta(days=1)
    sessions = weekday_sessions(end, 100)
    days = [day.isoformat() for day in sessions]
    symbols = tuple(symbol for symbol in load_symbols("historical_100d")
                    if len(list(CACHE_DIR.glob(f"{symbol}_*_1m.json"))) >= len(sessions))
    rows = opportunities(load_market_data(OKX(settings()), symbols, sessions)).dropna()
    holdout = set(days[:10])
    results = []
    for horizon, experiment in ((150, "gap_relative_fade_confirm_e0936_quote_h150_t100_cost14_v3"),
                                (60, "gap_relative_fade_confirm_e0936_quote_h60_t100_cost14_v1")):
        for cost in (14, 21):
            trades = frozen_trades(rows, horizon=horizon, cost_bps=cost)
            results.append({"experiment_id": experiment, "horizon_minutes": horizon,
                            "round_trip_cost_bps": cost,
                            "external_holdout": metric([trade for trade in trades if trade["date"] in holdout]),
                            "prior_90d_diagnostic": metric([trade for trade in trades if trade["date"] not in holdout])})
    OUTPUT.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(), "symbols": list(symbols),
        "holdout_sessions": sorted(holdout), "method": "Frozen rule; no parameter selection on holdout.",
        "results": results,
        "warning": "Historical external cohort is supplemental evidence; fresh forward labels remain required for promotion.",
    }, ensure_ascii=False, indent=2))
    print(json.dumps({"holdout_sessions": sorted(holdout), "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
