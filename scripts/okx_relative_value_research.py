#!/usr/bin/env python3
"""Rolling relative-value research: equity token versus SPY token hedge."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import DEFAULT_SYMBOLS, load_market_data, weekday_sessions  # noqa: E402
from scripts.okx_return_model_research import BASE_FEATURES, dataset, frame, metric, models  # noqa: E402

NY = ZoneInfo("America/New_York")
OUTPUT = ROOT / "data" / "okx_relative_value_90d.json"


def rolling_predictions(rows, days, targets, features, lookback=40):
    frames, values = [], []
    for day in targets:
        index = days.index(day)
        history = days[max(0, index - lookback):index]
        train, target = rows[rows.date.isin(history)], rows[rows.date == day]
        if len(train) < 1000 or target.empty:
            continue
        model = models()["hist7"]
        model.fit(train[features].astype(float), train["relative_bps"].astype(float))
        frames.append(target)
        values.append(model.predict(target[features].astype(float)))
    if not frames:
        return rows.iloc[:0], np.array([])
    import pandas as pd
    return pd.concat(frames, ignore_index=True), np.concatenate(values)


def pairs(rows, prediction, threshold, per_time=2):
    candidates = rows.copy()
    candidates["prediction"] = prediction
    candidates = candidates[abs(candidates.prediction) >= threshold]
    active, trades = [], []
    for entry_time, group in candidates.groupby("entry_time", sort=True):
        active = [x for x in active if x["exit_time"] > entry_time]
        slots = max(0, per_time - len(active))
        for _, row in group.reindex(group.prediction.abs().sort_values(ascending=False).index).head(slots).iterrows():
            direction = 1 if row.prediction > 0 else -1
            # Two round trips: token and SPY hedge. Twenty bps is deliberately
            # above observed liquid-contract spreads plus fee allowance.
            trade = {"symbol": row.symbol, "side": "LONG_SPREAD" if direction > 0 else "SHORT_SPREAD",
                     "entry_time": int(entry_time), "exit_time": int(row.exit_time_12),
                     "net_r": (direction * float(row.relative_bps) - 20) / 100}
            active.append(trade); trades.append(trade)
    return trades


def main():
    end = datetime.now(NY).date() - timedelta(days=1)
    sessions = weekday_sessions(end, 90); days = [x.isoformat() for x in sessions]
    raw = load_market_data(OKX(settings()), DEFAULT_SYMBOLS, sessions)
    rows = dataset(raw)
    spy = frame(raw["SPY-USDT-SWAP"], "SPY-USDT-SWAP")[["entry_time", "return_bps_12"]].rename(
        columns={"return_bps_12": "spy_forward_bps"})
    rows = rows.merge(spy, on="entry_time", how="left").dropna(subset=["spy_forward_bps"])
    rows["relative_bps"] = rows.return_bps_12 - rows.spy_forward_bps
    features = [*BASE_FEATURES, *sorted(c for c in rows if c.startswith("symbol_"))]
    results = []
    for split, target in (("validation", days[40:50]), ("development", days[50:60]), ("final", days[60:90])):
        scored, prediction = rolling_predictions(rows, days, target, features)
        for threshold in (15, 25, 40, 60):
            result = pairs(scored, prediction, threshold)
            results.append({"split": split, "threshold": threshold, **metric(result)})
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "results": results,
              "cost": "20 bps per hedged pair", "warning": "diagnostic only; previously inspected dates"}
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2)); print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
