#!/usr/bin/env python3
"""Walk-forward models trained on execution-equivalent triple-barrier outcomes."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import DEFAULT_SYMBOLS, aggregate_bars, load_market_data, weekday_sessions  # noqa: E402
from scripts.okx_return_model_research import BASE_FEATURES, dataset, metric, models  # noqa: E402

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
OUTPUT = ROOT / "data" / "okx_barrier_model_90d.json"
TRADE_SYMBOLS = tuple(x for x in DEFAULT_SYMBOLS if not x.startswith(("SPY-", "QQQ-")))


def barrier_targets(rows: list[list[str]], stop_min_bps: float, target_r: float,
                    horizon_bars: int, cost_bps: float = 14) -> pd.DataFrame:
    bars = aggregate_bars(rows, 5)
    frame = pd.DataFrame(bars, columns=["stamp", "open", "high", "low", "close", "volume", "v1", "v2", "confirm"])
    if frame.empty:
        return pd.DataFrame(columns=["entry_time", "long_net_r", "short_net_r"])
    frame.index = pd.to_datetime(frame.pop("stamp").astype("int64"), unit="ms", utc=True)
    for name in ("open", "high", "low", "close"):
        frame[name] = pd.to_numeric(frame[name], errors="coerce")
    previous = frame["close"].shift(1)
    true_range = pd.concat([
        frame["high"] - frame["low"],
        (frame["high"] - previous).abs(),
        (frame["low"] - previous).abs(),
    ], axis=1).max(axis=1)
    atr14 = true_range.rolling(14).mean()
    local = frame.index.tz_convert(NY)
    cash = ((local.hour > 9) | ((local.hour == 9) & (local.minute >= 55))) & (local.hour < 15)
    cadence = np.array([item.minute % 30 == 25 for item in local])
    result = []
    for index in np.flatnonzero(cash & cadence):
        if index + horizon_bars >= len(frame) or not np.isfinite(atr14.iloc[index]):
            continue
        entry = float(frame["open"].iloc[index + 1])
        risk = max(float(atr14.iloc[index]) * 1.2, entry * stop_min_bps / 10_000)
        if entry <= 0 or risk <= 0:
            continue
        outcomes: dict[str, float] = {}
        for side, direction in (("long", 1), ("short", -1)):
            gross_r = None
            for offset in range(1, horizon_bars + 1):
                bar = frame.iloc[index + offset]
                favorable = (float(bar.high) - entry) * direction if direction > 0 else entry - float(bar.low)
                adverse = (float(bar.low) - entry) * direction if direction > 0 else entry - float(bar.high)
                # Conservative ambiguity rule: when both barriers occur in one
                # 5m candle, the stop is assumed to have happened first.
                if adverse <= -risk:
                    gross_r = -1.0
                    break
                if favorable >= target_r * risk:
                    gross_r = target_r
                    break
            if gross_r is None:
                close = float(frame["close"].iloc[index + horizon_bars])
                gross_r = (close - entry) * direction / risk
            outcomes[f"{side}_net_r"] = gross_r - (cost_bps / 10_000) / (risk / entry)
        result.append({
            "entry_time": int(frame.index[index].timestamp() * 1000) + 300_000,
            **outcomes,
        })
    return pd.DataFrame(result)


def research_dataset(data: dict[str, list[list[str]]], stop_min_bps: float,
                     target_r: float, horizon_bars: int) -> pd.DataFrame:
    features = dataset(data, require_target=False)
    frames = []
    for symbol in TRADE_SYMBOLS:
        targets = barrier_targets(data[symbol], stop_min_bps, target_r, horizon_bars)
        target_features = features[features["symbol"] == symbol].merge(targets, on="entry_time", how="inner")
        frames.append(target_features)
    return pd.concat(frames, ignore_index=True).dropna(subset=["long_net_r", "short_net_r"])


def rolling_predictions(rows: pd.DataFrame, days: list[str], target_days: list[str],
                        model_name: str, features: list[str], lookback_days: int = 40,
                        retrain_days: int = 5) -> tuple[pd.DataFrame, np.ndarray, np.ndarray]:
    frames, longs, shorts = [], [], []
    for offset in range(0, len(target_days), retrain_days):
        block = target_days[offset:offset + retrain_days]
        first = days.index(block[0])
        history = days[max(0, first - lookback_days):first]
        training = rows[rows["date"].isin(history)]
        target = rows[rows["date"].isin(block)]
        if len(training) < 700 or target.empty:
            continue
        values = training[features].astype(float).to_numpy()
        future = target[features].astype(float).to_numpy()
        long_model, short_model = models()[model_name], models()[model_name]
        long_model.fit(values, training["long_net_r"].astype(float).to_numpy())
        short_model.fit(values, training["short_net_r"].astype(float).to_numpy())
        frames.append(target)
        longs.append(long_model.predict(future))
        shorts.append(short_model.predict(future))
    if not frames:
        return rows.iloc[:0].copy(), np.array([]), np.array([])
    return pd.concat(frames, ignore_index=True), np.concatenate(longs), np.concatenate(shorts)


def portfolio(rows: pd.DataFrame, long_prediction: np.ndarray, short_prediction: np.ndarray,
              threshold_r: float, horizon_bars: int) -> list[dict[str, Any]]:
    value = rows.copy()
    value["long_prediction"] = long_prediction
    value["short_prediction"] = short_prediction
    value["prediction"] = value[["long_prediction", "short_prediction"]].max(axis=1)
    value = value[value["prediction"] >= threshold_r].sort_values(
        ["entry_time", "prediction"], ascending=[True, False]
    )
    active, selected = [], []
    for record in value.to_dict("records"):
        active = [item for item in active if item["exit_time"] > record["entry_time"]]
        if len(active) >= 5 or any(item["symbol"] == record["symbol"] for item in active):
            continue
        side = "LONG" if record["long_prediction"] >= record["short_prediction"] else "SHORT"
        net_r = float(record["long_net_r"] if side == "LONG" else record["short_net_r"])
        trade = {
            "symbol": record["symbol"], "side": side, "date": record["date"],
            "entry_time": int(record["entry_time"]),
            "exit_time": int(record["entry_time"] + horizon_bars * 300_000),
            "prediction_r": float(record["prediction"]), "net_r": net_r,
        }
        active.append(trade)
        selected.append(trade)
    return selected


def main() -> None:
    end = datetime.now(NY).date() - timedelta(days=1)
    sessions = weekday_sessions(end, 90)
    days = [item.isoformat() for item in sessions]
    data = load_market_data(OKX(settings()), DEFAULT_SYMBOLS, sessions)
    train_days, validation_days = days[20:40], days[40:50]
    development_days, final_days = days[50:60], days[60:90]
    results = []
    for stop_min_bps in (50, 75, 100):
        for target_r in (1.5, 2.0):
            for horizon_bars in (12, 24):
                rows = research_dataset(data, stop_min_bps, target_r, horizon_bars)
                symbol_features = sorted(x for x in rows if x.startswith("symbol_"))
                features = [*BASE_FEATURES, *symbol_features]
                target_days = [*train_days, *validation_days, *development_days, *final_days]
                for model_name in ("ridge", "hist7"):
                    scored, long_prediction, short_prediction = rolling_predictions(
                        rows, days, target_days, model_name, features
                    )
                    for threshold_r in (0, .05, .1, .2, .3):
                        trades = portfolio(scored, long_prediction, short_prediction, threshold_r, horizon_bars)
                        parts = {name: metric([x for x in trades if x["date"] in target]) for name, target in (
                            ("train", train_days), ("validation", validation_days),
                            ("development", development_days), ("final_diagnostic", final_days),
                        )}
                        results.append({
                            "stop_min_bps": stop_min_bps, "target_r": target_r,
                            "horizon_bars": horizon_bars, "model": model_name,
                            "threshold_r": threshold_r, **parts,
                        })
    eligible = [row for row in results if all(
        row[split]["trades"] >= (60 if split == "train" else 30)
        and (row[split]["profit_factor"] or 0) > 1.1
        and row[split]["expectancy_r"] > .05
        for split in ("train", "validation", "development")
    )]
    eligible.sort(key=lambda row: min(row[x]["profit_factor"] or 0 for x in ("train", "validation", "development")), reverse=True)
    results.sort(key=lambda row: min(row[x]["profit_factor"] or 0 for x in ("train", "validation", "development")), reverse=True)
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "method": "dual-side execution-equivalent triple barrier, 14bps costs, rolling date training",
        "tested": len(results), "eligible": eligible[:20], "leaderboard": results[:30],
        "selected": eligible[0] if eligible else None, "passed": False,
        "warning": "research only; final dates are contaminated diagnostics and never deployment evidence",
    }
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
