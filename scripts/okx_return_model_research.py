#!/usr/bin/env python3
"""Cross-sectional return forecasting on fixed opportunities, not prefiltered breakouts."""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import joblib
from sklearn.ensemble import HistGradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import DEFAULT_SYMBOLS, aggregate_bars, load_market_data, weekday_sessions  # noqa: E402

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
OUTPUT = ROOT / "data" / "okx_return_model_90d.json"
TRADE_SYMBOLS = tuple(symbol for symbol in DEFAULT_SYMBOLS if not symbol.startswith(("SPY-", "QQQ-")))
BASE_FEATURES = ("r1", "r3", "r6", "r12", "ema_gap", "volatility", "volume_ratio", "range_position",
                 "spy_r3", "spy_r6", "spy_r12", "qqq_r3", "qqq_r6", "qqq_r12",
                 "cross_rank_r6", "minute_sin", "minute_cos", "weekday")


def frame(rows: list[list[str]], symbol: str) -> pd.DataFrame:
    bars = aggregate_bars(rows, 5)
    value = pd.DataFrame(bars, columns=["stamp", "open", "high", "low", "close", "volume", "v1", "v2", "confirm"])
    value.index = pd.to_datetime(value.pop("stamp").astype("int64"), unit="ms", utc=True)
    for column in ("open", "high", "low", "close", "volume"):
        value[column] = pd.to_numeric(value[column], errors="coerce")
    close = value["close"]
    value["r1"] = close.pct_change()
    value["r3"] = close.pct_change(3)
    value["r6"] = close.pct_change(6)
    value["r12"] = close.pct_change(12)
    value["ema_gap"] = (close.ewm(span=9, adjust=False).mean() - close.ewm(span=21, adjust=False).mean()) / close
    value["volatility"] = value["r1"].rolling(20).std()
    value["volume_ratio"] = value["volume"] / value["volume"].shift(1).rolling(20).mean().replace(0, np.nan)
    rolling_high, rolling_low = value["high"].shift(1).rolling(12).max(), value["low"].shift(1).rolling(12).min()
    value["range_position"] = (close - rolling_low) / (rolling_high - rolling_low).replace(0, np.nan)
    value["symbol"] = symbol
    value["entry_time"] = [int(item.timestamp() * 1000) + 300_000 for item in value.index]
    local = value.index.tz_convert(NY)
    value["date"] = [item.date().isoformat() for item in local]
    minutes = np.array([item.hour * 60 + item.minute - 570 for item in local])
    value["minute_sin"] = np.sin(2 * math.pi * minutes / 390)
    value["minute_cos"] = np.cos(2 * math.pi * minutes / 390)
    value["weekday"] = [item.weekday() for item in local]
    # Decision uses the completed 5m bar; entry is next bar open.
    value["entry_price"] = value["open"].shift(-1)
    for horizon in (6, 12, 24):
        value[f"exit_time_{horizon}"] = value["entry_time"] + horizon * 300_000
        value[f"return_bps_{horizon}"] = (value["close"].shift(-horizon) / value["entry_price"] - 1) * 10_000
    cash = ((local.hour > 9) | ((local.hour == 9) & (local.minute >= 55))) & (local.hour < 15)
    cadence = np.array([item.minute % 30 == 25 for item in local])  # close becomes known at :00/:30.
    return value[cash & cadence].replace([np.inf, -np.inf], np.nan)


def dataset(data: dict[str, list[list[str]]], require_target: bool = True) -> pd.DataFrame:
    frames = {symbol: frame(rows, symbol) for symbol, rows in data.items()}
    for market_symbol, prefix in (("SPY-USDT-SWAP", "spy"), ("QQQ-USDT-SWAP", "qqq")):
        market = frames[market_symbol][["entry_time", "r3", "r6", "r12"]].rename(
            columns={name: f"{prefix}_{name}" for name in ("r3", "r6", "r12")}
        )
        for symbol in TRADE_SYMBOLS:
            frames[symbol] = frames[symbol].merge(market, on="entry_time", how="left")
    combined = pd.concat([frames[symbol] for symbol in TRADE_SYMBOLS], ignore_index=True)
    combined["cross_rank_r6"] = combined.groupby("entry_time")["r6"].rank(pct=True)
    dummies = pd.get_dummies(combined["symbol"], prefix="symbol", dtype=float)
    required = [*BASE_FEATURES, *( ["return_bps_24"] if require_target else [])]
    return pd.concat([combined, dummies], axis=1).dropna(subset=required).reset_index(drop=True)


def models() -> dict[str, Any]:
    return {
        "ridge": make_pipeline(StandardScaler(), Ridge(alpha=20)),
        "hist7": HistGradientBoostingRegressor(learning_rate=.04, max_iter=180, max_leaf_nodes=7,
                                                min_samples_leaf=40, l2_regularization=5, random_state=42),
        "hist15": HistGradientBoostingRegressor(learning_rate=.03, max_iter=200, max_leaf_nodes=15,
                                                 min_samples_leaf=50, l2_regularization=8, random_state=42),
        "forest": RandomForestRegressor(n_estimators=300, max_depth=6, min_samples_leaf=30,
                                         max_features=.7, n_jobs=-1, random_state=42),
    }


def walkforward_predictions(rows: pd.DataFrame, session_days: list[str], target_days: list[str],
                            model_name: str, horizon: int, features: list[str],
                            lookback_days: int = 40, retrain_days: int = 5) -> tuple[pd.DataFrame, np.ndarray]:
    """Fit only on days preceding each block, then predict the next five sessions."""
    predicted_frames = []
    predicted_values = []
    for offset in range(0, len(target_days), retrain_days):
        block = target_days[offset:offset + retrain_days]
        first_index = session_days.index(block[0])
        history = session_days[max(0, first_index - lookback_days):first_index]
        training = rows[rows["date"].isin(history)]
        target = rows[rows["date"].isin(block)]
        if len(training) < 1000 or target.empty:
            continue
        model = models()[model_name]
        model.fit(training[features].astype(float).to_numpy(),
                  training[f"return_bps_{horizon}"].astype(float).to_numpy())
        predicted_frames.append(target)
        predicted_values.append(model.predict(target[features].astype(float).to_numpy()))
    if not predicted_frames:
        return rows.iloc[0:0].copy(), np.array([])
    return pd.concat(predicted_frames, ignore_index=True), np.concatenate(predicted_values)


def portfolio(rows: pd.DataFrame, predictions: np.ndarray, horizon: int, threshold: float) -> list[dict[str, Any]]:
    candidates = rows.copy()
    candidates["prediction"] = predictions
    candidates = candidates[abs(candidates["prediction"]) >= threshold].sort_values(
        ["entry_time", "prediction"], ascending=[True, False]
    )
    active: list[dict[str, Any]] = []
    selected = []
    for record in candidates.to_dict("records"):
        active = [item for item in active if item["exit_time"] > record["entry_time"]]
        if len(active) >= 5 or any(item["symbol"] == record["symbol"] for item in active):
            continue
        direction = 1 if record["prediction"] > 0 else -1
        net_r = (direction * float(record[f"return_bps_{horizon}"]) - 14) / 100
        trade = {"symbol": record["symbol"], "side": "LONG" if direction > 0 else "SHORT",
                 "entry_time": int(record["entry_time"]), "exit_time": int(record[f"exit_time_{horizon}"]),
                 "net_r": net_r, "prediction_bps": float(record["prediction"])}
        active.append(trade)
        selected.append(trade)
    return selected


def metric(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(row["net_r"]) for row in rows]
    positive, negative = sum(v for v in values if v > 0), -sum(v for v in values if v <= 0)
    equity = peak = drawdown = 0.0
    for row in sorted(rows, key=lambda item: item["exit_time"]):
        equity += row["net_r"]
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {"trades": len(rows), "wins": sum(v > 0 for v in values),
            "win_rate": round(sum(v > 0 for v in values) / len(values) * 100, 2) if values else 0,
            "net_r": round(sum(values), 3), "expectancy_r": round(sum(values) / len(values), 4) if values else 0,
            "profit_factor": round(positive / negative, 3) if negative else None,
            "max_drawdown_r": round(drawdown, 3)}


def main() -> None:
    end = datetime.now(NY).date() - timedelta(days=1)
    sessions = weekday_sessions(end, 90)
    days = [day.isoformat() for day in sessions]
    data = load_market_data(OKX(settings()), DEFAULT_SYMBOLS, sessions)
    rows = dataset(data)
    train_days, validation_days = days[:40], days[40:50]
    development_days, final_days = days[50:60], days[60:90]
    train = rows[rows["date"].isin(train_days)].copy()
    validation = rows[rows["date"].isin(validation_days)].copy()
    development = rows[rows["date"].isin(development_days)].copy()
    final = rows[rows["date"].isin(final_days)].copy()
    symbol_features = sorted(column for column in rows if column.startswith("symbol_"))
    features = [*BASE_FEATURES, *symbol_features]
    candidates = []
    all_evaluations = []
    for horizon in (6, 12, 24):
        for name in models():
            key = f"{name}_{horizon}"
            evaluation_rows, evaluation_prediction = walkforward_predictions(
                rows, days, [*validation_days, *development_days], name, horizon, features,
            )
            validation_mask = evaluation_rows["date"].isin(validation_days).to_numpy()
            development_mask = evaluation_rows["date"].isin(development_days).to_numpy()
            validation_frame = evaluation_rows[validation_mask]
            development_frame = evaluation_rows[development_mask]
            validation_prediction = evaluation_prediction[validation_mask]
            development_prediction = evaluation_prediction[development_mask]
            for threshold in (0, 2, 4, 6, 8, 10, 15, 20, 25, 30, 40, 50):
                v = metric(portfolio(validation_frame, validation_prediction, horizon, threshold))
                d = metric(portfolio(development_frame, development_prediction, horizon, threshold))
                evaluation = {"model": key, "horizon": horizon, "threshold": threshold,
                              "validation": v, "development": d}
                all_evaluations.append(evaluation)
                if (v["trades"] >= 40 and v["net_r"] > 0 and (v["profit_factor"] or 0) > 1.1 and v["expectancy_r"] > .05
                        and d["trades"] >= 40 and d["net_r"] > 0 and (d["profit_factor"] or 0) > 1.1 and d["expectancy_r"] > .05):
                    candidates.append(evaluation)
    candidates.sort(key=lambda row: (min(row["validation"]["profit_factor"], row["development"]["profit_factor"]),
                                     row["validation"]["net_r"] + row["development"]["net_r"]), reverse=True)
    all_evaluations.sort(key=lambda row: (
        min(row["validation"]["profit_factor"] or 0, row["development"]["profit_factor"] or 0),
        row["validation"]["net_r"] + row["development"]["net_r"],
    ), reverse=True)
    sample_evaluations = [row for row in all_evaluations
                          if row["validation"]["trades"] >= 40 and row["development"]["trades"] >= 40]
    sample_evaluations.sort(key=lambda row: (
        min(row["validation"]["profit_factor"] or 0, row["development"]["profit_factor"] or 0),
        row["validation"]["net_r"] + row["development"]["net_r"],
    ), reverse=True)
    selected = candidates[0] if candidates else None
    final_result = None
    if selected:
        selected_name = selected["model"].rsplit("_", 1)[0]
        final_frame, prediction = walkforward_predictions(
            rows, days, final_days, selected_name, selected["horizon"], features,
        )
        final_result = metric(portfolio(final_frame, prediction, selected["horizon"], selected["threshold"]))
    passed = bool(final_result and final_result["trades"] >= 100 and final_result["net_r"] > 0
                  and (final_result["profit_factor"] or 0) > 1.2 and final_result["expectancy_r"] > .1)
    shadow_model_path = None
    if selected:
        selected_name = selected["model"].rsplit("_", 1)[0]
        latest_training = rows[rows["date"].isin(days[-40:])]
        shadow_model = models()[selected_name]
        shadow_model.fit(latest_training[features].astype(float).to_numpy(),
                         latest_training[f"return_bps_{selected['horizon']}"].astype(float).to_numpy())
        shadow_model_path = ROOT / "data" / "okx_return_shadow_model.joblib"
        joblib.dump({"model": shadow_model, "model_name": selected_name,
                     "features": features, "horizon": selected["horizon"],
                     "threshold": selected["threshold"], "trained_days": days[-40:],
                     "created_at": datetime.now(UTC).isoformat(), "mode": "shadow_only"}, shadow_model_path)
    report = {"generated_at": datetime.now(UTC).isoformat(), "opportunities": len(rows),
              "split_counts": {"train": len(train), "validation": len(validation),
                               "development": len(development), "final": len(final)},
              "retraining": "rolling 40 sessions, refit before each 5-session block",
              "selected": selected, "final_diagnostic": final_result, "passed": passed,
              "shadow_model": str(shadow_model_path) if shadow_model_path else None,
              "warning": "last 30 dates overlap earlier studies; a passing result would still require forward shadow validation",
              "leaderboard": candidates[:20], "diagnostic_leaderboard": all_evaluations[:20],
              "sample_eligible_diagnostics": sample_evaluations[:20], "features": features}
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
