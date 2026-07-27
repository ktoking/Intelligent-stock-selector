#!/usr/bin/env python3
"""Compare prespecified portfolio constructors on rolling out-of-sample predictions."""
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
from scripts.okx_return_model_research import BASE_FEATURES, dataset, metric, walkforward_predictions  # noqa: E402

NY = ZoneInfo("America/New_York")
OUTPUT = ROOT / "data" / "okx_portfolio_robustness_90d.json"


def construct(rows, predictions: np.ndarray, *, threshold: float, per_side: int,
              trend: bool = False, skip_open: bool = False) -> list[dict]:
    candidates = rows.copy()
    candidates["prediction"] = predictions
    candidates = candidates[abs(candidates["prediction"]) >= threshold]
    active: list[dict] = []
    selected: list[dict] = []
    for entry_time, group in candidates.groupby("entry_time", sort=True):
        active = [item for item in active if item["exit_time"] > entry_time]
        slots = max(0, 5 - len(active))
        if slots == 0:
            continue
        if skip_open:
            local = datetime.fromtimestamp(int(entry_time) / 1000, timezone.utc).astimezone(NY)
            if (local.hour, local.minute) < (10, 30):
                continue
        if trend:
            group = group[((group["prediction"] > 0) & (group["r6"] > 0) & (group["ema_gap"] > 0)) |
                          ((group["prediction"] < 0) & (group["r6"] < 0) & (group["ema_gap"] < 0))]
        longs = group[group["prediction"] > 0].nlargest(per_side, "prediction")
        shorts = group[group["prediction"] < 0].nsmallest(per_side, "prediction")
        choices = []
        for _, record in longs.iterrows():
            choices.append((abs(float(record["prediction"])), record, 1))
        for _, record in shorts.iterrows():
            choices.append((abs(float(record["prediction"])), record, -1))
        for _, record, direction in sorted(choices, key=lambda item: item[0], reverse=True)[:slots]:
            trade = {
                "symbol": record["symbol"], "side": "LONG" if direction > 0 else "SHORT",
                "entry_time": int(entry_time), "exit_time": int(record["exit_time_12"]),
                "net_r": (direction * float(record["return_bps_12"]) - 14) / 100,
                "prediction_bps": float(record["prediction"]),
            }
            active.append(trade)
            selected.append(trade)
    return selected


def main() -> None:
    end = datetime.now(NY).date() - timedelta(days=1)
    sessions = weekday_sessions(end, 90)
    days = [day.isoformat() for day in sessions]
    rows = dataset(load_market_data(OKX(settings()), DEFAULT_SYMBOLS, sessions))
    features = [*BASE_FEATURES, *sorted(column for column in rows if column.startswith("symbol_"))]
    variants = [
        {"name": "balanced_1_t25", "threshold": 25, "per_side": 1},
        {"name": "balanced_2_t25", "threshold": 25, "per_side": 2},
        {"name": "balanced_2_t40", "threshold": 40, "per_side": 2},
        {"name": "trend_2_t25", "threshold": 25, "per_side": 2, "trend": True},
        {"name": "trend_2_t40", "threshold": 40, "per_side": 2, "trend": True},
        {"name": "skip_open_2_t25", "threshold": 25, "per_side": 2, "skip_open": True},
        {"name": "skip_open_trend_t25", "threshold": 25, "per_side": 2, "trend": True, "skip_open": True},
    ]
    results = []
    for split, target_days in (("validation", days[40:50]), ("development", days[50:60]), ("final", days[60:90])):
        frame, prediction = walkforward_predictions(rows, days, target_days, "hist7", 12, features)
        for variant in variants:
            trades = construct(frame, prediction, **{k: v for k, v in variant.items() if k != "name"})
            results.append({"split": split, "variant": variant["name"], **metric(trades),
                            "longs": sum(t["side"] == "LONG" for t in trades),
                            "shorts": sum(t["side"] == "SHORT" for t in trades)})
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "results": results,
              "selection_rule": "variants fixed before this diagnostic; 14 bps round trip; max five active"}
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
