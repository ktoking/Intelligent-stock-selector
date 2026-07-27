#!/usr/bin/env python3
"""Walk-forward regime model for the relative opening-gap fade (research only).

The model is refit before every session using the preceding 40 sessions.  It
never sees the current or future session while making a score, and it has no
execution side effects.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

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
OUTPUT = ROOT / "data" / "okx_gap_regime_model_research_90d.json"
EVENT_PATH = ROOT / "data" / "okx_event_calendar.json"


def build_rows(raw: dict[str, list[list[str]]]) -> pd.DataFrame:
    rows = opportunities(raw).merge(opening_features(raw), on=["symbol", "date"], how="left")
    # opportunities() already removes SPY/QQQ after attaching SPY features.
    # Keep the model on fields that are actually present at this research path.
    rows = rows.dropna().copy()
    rows["relative_gap"] = rows.gap_bps - rows.spy_gap
    rows["relative_first5"] = rows.first5_bps - rows.spy_first5
    rows["relative_previous"] = rows.previous_day_bps - rows.spy_previous_day
    rows["relative_gap_rank"] = rows.groupby("date").relative_gap.transform(lambda x: x.abs().rank(pct=True))
    rows["abs_relative_gap"] = rows.relative_gap.abs()
    rows["abs_spy_gap"] = rows.spy_gap.abs()
    rows["spy_first5_opposes_gap"] = (rows.relative_gap * rows.spy_first5 < 0).astype(float)
    # This is the original entry universe, not an optimized candidate subset.
    rows = rows[(rows.relative_gap.abs() >= 100) & ((rows.relative_gap * rows.relative_first5 < 0) |
                                                     (rows.relative_gap * rows.relative_previous <= 0)) & rows.exit_150.notna()].copy()
    rows["direction"] = np.where(rows.relative_gap > 0, -1.0, 1.0)
    rows["net_bps"] = rows.direction * (rows.exit_150 / rows.entry - 1) * 10_000 - 14
    rows["label"] = (rows.net_bps > 0).astype(int)
    stamp = pd.to_datetime(rows.entry_time.astype("int64"), unit="ms", utc=True).dt.tz_convert(NY)
    rows["weekday"] = stamp.dt.weekday.astype(float)
    try:
        events = json.loads(EVENT_PATH.read_text())
        macro = {datetime.fromisoformat(item["scheduled_at"]).astimezone(NY).date().isoformat()
                 for item in events.get("macro_events", [])}
        earnings = {(str(item.get("symbol")), str(item.get("scheduled_at", ""))[:10])
                    for item in events.get("earnings", []) if item.get("event_type") == "EARNINGS_REPORTED"}
        rows["macro_day"] = rows.date.isin(macro).astype(float)
        rows["earnings_window"] = [float((symbol.split("-", 1)[0], date) in earnings)
                                    for symbol, date in zip(rows.symbol, rows.date)]
    except (OSError, ValueError, KeyError):
        rows["macro_day"] = 0.0; rows["earnings_window"] = 0.0
    return rows


FEATURES = [
    "relative_gap", "relative_first5", "relative_previous", "relative_gap_rank",
    "abs_relative_gap", "abs_spy_gap", "spy_first5_opposes_gap", "macro_day", "earnings_window",
    "spy_gap", "spy_first5", "spy_previous_day",
    "open_volume_ratio", "prior_range_bps", "direction", "weekday",
]


def walkforward_scores(rows: pd.DataFrame, days: list[str]) -> pd.DataFrame:
    scored = []
    for index, day in enumerate(days):
        if index < 40:
            continue
        history = days[index - 40:index]
        train, current = rows[rows.date.isin(history)], rows[rows.date == day]
        if len(train) < 80 or current.empty or train.label.nunique() < 2:
            continue
        model = HistGradientBoostingClassifier(
            learning_rate=.045, max_iter=120, max_leaf_nodes=5, min_samples_leaf=15,
            l2_regularization=8, random_state=42,
        )
        model.fit(train[FEATURES].astype(float), train.label.astype(int))
        value = current.copy()
        value["probability"] = model.predict_proba(current[FEATURES].astype(float))[:, 1]
        scored.append(value)
    if not scored:
        raise RuntimeError("no walk-forward scores: inspect candidate coverage before interpreting a backtest")
    return pd.concat(scored, ignore_index=True)


def trades(rows: pd.DataFrame, threshold: float) -> list[dict]:
    selected = rows[rows.probability >= threshold]
    result = []
    for _, group in selected.groupby("entry_time", sort=True):
        for _, row in group.nlargest(5, "probability").iterrows():
            result.append({"date": row.date, "symbol": row.symbol, "entry_time": int(row.entry_time),
                           "exit_time": int(row.entry_time + 150 * 60_000), "net_r": float(row.net_bps) / 100,
                           "probability": float(row.probability)})
    return result


def eligible(parts: dict) -> tuple[bool, list[str]]:
    reasons = []
    for split in ("validation", "development"):
        value = parts[split]
        if value["trades"] < 25: reasons.append(f"{split}:样本<25")
        if value["win_rate"] < 52: reasons.append(f"{split}:胜率<52%")
        if (value["profit_factor"] or 0) < 1.2: reasons.append(f"{split}:PF<1.2")
        if value["expectancy_r"] < .08: reasons.append(f"{split}:期望<0.08R")
    return not reasons, reasons


def main() -> None:
    end = datetime.now(NY).date() - timedelta(days=1)
    sessions = weekday_sessions(end, 90); days = [day.isoformat() for day in sessions]
    requested = load_symbols("historical_90d")
    symbols = tuple(symbol for symbol in requested if len(list(CACHE_DIR.glob(f"{symbol}_*_1m.json"))) >= len(days))
    raw = load_market_data(OKX(settings()), symbols, sessions)
    rows = build_rows(raw)
    scored = walkforward_scores(rows, days)
    split_days = {"validation": days[40:50], "development": days[50:60], "final_diagnostic": days[60:90]}
    results = []
    # These probability thresholds are declared before evaluation, avoiding a
    # post-hoc choice of the best-looking confidence cutoff.
    for threshold in (.50, .55, .60, .65):
        value = trades(scored, threshold)
        parts = {name: metric([trade for trade in value if trade["date"] in target]) for name, target in split_days.items()}
        passed, reasons = eligible(parts)
        results.append({"threshold": threshold, **parts, "forward_demo_eligible": passed, "rejection_reasons": reasons})
    report = {"generated_at": datetime.now(UTC).isoformat(), "sessions": {"count": len(days), "start": days[0], "end": days[-1]},
              "universe": list(symbols), "universe_rows": len(rows), "model": "HistGradientBoostingClassifier; rolling 40-session fit; refit daily",
              "features": FEATURES, "cost_model": "14 bp round trip; fixed 09:35 entry and 12:00 exit",
              "promotion_gate": "validation/development: n>=25, WR>=52%, PF>=1.2, expectancy>=0.08R",
              "results": results,
              "warning": "Final diagnostic dates were previously inspected. Even a pass would require 10 new forward shadow sessions before Demo execution."}
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
