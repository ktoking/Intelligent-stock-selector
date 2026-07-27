#!/usr/bin/env python3
"""Train-only symbol selection for the relative opening-gap fade.

The symbol list is selected solely from the first 40 sessions.  It is frozen
for validation, development and final diagnostics to avoid choosing stocks
because they happened to win later in the sample.
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

from scripts.okx_gap_feature_research import opening_features  # noqa: E402
from scripts.okx_gap_research import opportunities  # noqa: E402
from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import CACHE_DIR, load_market_data, weekday_sessions  # noqa: E402
from scripts.okx_research_universe import load_symbols  # noqa: E402
from scripts.okx_return_model_research import metric  # noqa: E402

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
OUTPUT = ROOT / "data" / "okx_gap_symbol_selection_90d.json"


def trade_rows(rows: pd.DataFrame, horizon: int) -> list[dict]:
    result = []
    for _, group in rows.groupby("entry_time", sort=True):
        for _, row in group.reindex(group.relative_gap.abs().sort_values(ascending=False).index).head(5).iterrows():
            side = -1 if row.relative_gap > 0 else 1
            bps = side * (float(row[f"exit_{horizon}"]) / float(row.entry) - 1) * 10_000 - 14
            result.append({"date": row.date, "symbol": row.symbol, "entry_time": int(row.entry_time),
                           "exit_time": int(row.entry_time + horizon * 60_000), "net_r": bps / 100})
    return result


def main() -> None:
    end = datetime.now(NY).date() - timedelta(days=1)
    sessions = weekday_sessions(end, 90); days = [day.isoformat() for day in sessions]
    symbols = tuple(symbol for symbol in load_symbols("historical_90d") if len(list(CACHE_DIR.glob(f"{symbol}_*_1m.json"))) >= len(days))
    raw = load_market_data(OKX(settings()), symbols, sessions)
    rows = opportunities(raw).merge(opening_features(raw), on=["symbol", "date"], how="left").dropna().copy()
    rows["relative_gap"] = rows.gap_bps - rows.spy_gap
    rows["relative_first5"] = rows.first5_bps - rows.spy_first5
    rows["relative_previous"] = rows.previous_day_bps - rows.spy_previous_day
    base = (rows.relative_gap.abs() >= 100) & ((rows.relative_gap * rows.relative_first5 < 0) |
                                                 (rows.relative_gap * rows.relative_previous <= 0))
    results = []
    for horizon in (60, 90, 150):
        subset = rows[base & rows[f"exit_{horizon}"].notna()]
        trades = trade_rows(subset, horizon)
        train = [row for row in trades if row["date"] in days[:40]]
        per_symbol = []
        for symbol in sorted({row["symbol"] for row in train}):
            stats = metric([row for row in train if row["symbol"] == symbol])
            per_symbol.append({"symbol": symbol, **stats})
        # Prespecified conservative selection: enough observations plus PF>1.
        selected = [row["symbol"] for row in sorted(per_symbol, key=lambda row: ((row["profit_factor"] or 0), row["expectancy_r"]), reverse=True)
                    if row["trades"] >= 10 and (row["profit_factor"] or 0) >= 1.1 and row["expectancy_r"] > 0][:6]
        if not selected:
            selected = []
        periods = {"validation": days[40:50], "development": days[50:60], "final_diagnostic": days[60:90]}
        chosen = [row for row in trades if row["symbol"] in selected]
        values = {name: metric([row for row in chosen if row["date"] in target]) for name, target in periods.items()}
        passes = all(values[name]["trades"] >= 25 and values[name]["win_rate"] >= 50 and
                     (values[name]["profit_factor"] or 0) >= 1.2 and values[name]["expectancy_r"] >= .08
                     for name in ("validation", "development"))
        results.append({"horizon_minutes": horizon, "selected_symbols": selected, "train_symbol_stats": per_symbol,
                        **values, "forward_demo_eligible": passes})
    OUTPUT.write_text(json.dumps({"generated_at": datetime.now(UTC).isoformat(), "symbols": list(symbols),
        "selection_rule": "first 40 sessions only; per-symbol n>=10, PF>=1.1, expectancy>0; top six max",
        "promotion_gate": "validation/development each n>=25, WR>=50%, PF>=1.2, expectancy>=0.08R",
        "results": results, "warning": "Final diagnostic is not used for selection."}, ensure_ascii=False, indent=2))
    print(json.dumps({"results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
