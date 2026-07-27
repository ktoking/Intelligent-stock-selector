#!/usr/bin/env python3
"""Train and honestly validate a cost-aware OKX intraday signal-quality model."""
from __future__ import annotations

import argparse
from bisect import bisect_left
import json
import math
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.inspection import permutation_importance
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import (  # noqa: E402
    DEFAULT_SYMBOLS,
    StrategyConfig,
    _bar_index_at_or_after,
    _entry_allowed,
    _metrics,
    aggregate_bars,
    backtest_symbol,
    load_market_data,
    stats,
    simulate_exit,
    weekday_sessions,
)
from scripts.okx_trade_replay import closed_chronological  # noqa: E402

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
FEATURE_COLUMNS = (
    "side_long", "risk_bps", "volume10", "volume5", "volume1",
    "extension_atr", "trend10_atr", "slope10_atr", "vwap10_atr",
    "rsi_directional", "trend5_atr", "vwap5_atr", "breakout_buffer_atr",
    "confirmation_body_atr", "minute_sin", "minute_cos", "weekday",
    "spy_return_30m", "spy_return_60m", "spy_trend", "spy_volume",
    "qqq_return_30m", "qqq_return_60m", "qqq_trend", "qqq_volume",
    "cross_rank", "opening_return_bps",
)
RAW_CANDIDATE_CACHE = ROOT / "data" / "okx_candidate_universe_90d_raw.pkl"
CANDIDATE_CACHE = ROOT / "data" / "okx_candidate_universe_90d.pkl"


def _market_features(rows: list[list[str]], prefix: str) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=["stamp", "open", "high", "low", "close", "volume", "vol_ccy", "vol_quote", "confirm"])
    frame.index = pd.to_datetime(frame.pop("stamp").astype("int64"), unit="ms", utc=True)
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    bars = frame.resample("10min", origin="epoch").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
    }).dropna()
    bars.index = bars.index + pd.Timedelta(minutes=10)  # Feature becomes knowable at bar close.
    fast = bars["close"].ewm(span=9, adjust=False).mean()
    slow = bars["close"].ewm(span=21, adjust=False).mean()
    volume_mean = bars["volume"].shift(1).rolling(20, min_periods=5).mean()
    return pd.DataFrame({
        "stamp": bars.index,
        f"{prefix}_return_30m": bars["close"].pct_change(3),
        f"{prefix}_return_60m": bars["close"].pct_change(6),
        f"{prefix}_trend": (fast - slow) / bars["close"],
        f"{prefix}_volume": bars["volume"] / volume_mean.replace(0, np.nan),
    }).reset_index(drop=True).replace([np.inf, -np.inf], np.nan).fillna(0)


def _add_market_features(frame: pd.DataFrame, data: dict[str, list[list[str]]]) -> pd.DataFrame:
    for symbol, prefix in (("SPY-USDT-SWAP", "spy"), ("QQQ-USDT-SWAP", "qqq")):
        market = _market_features(data[symbol], prefix).sort_values("stamp")
        frame["stamp"] = pd.to_datetime(frame["entry_time"], unit="ms", utc=True).astype("datetime64[ns, UTC]")
        market["stamp"] = market["stamp"].astype("datetime64[ns, UTC]")
        frame = pd.merge_asof(frame.sort_values("stamp"), market, on="stamp", direction="backward")
    return frame.replace([np.inf, -np.inf], np.nan).fillna(0)


def candidate_frame(data: dict[str, list[list[str]]]) -> pd.DataFrame:
    if CANDIDATE_CACHE.exists():
        return pd.read_pickle(CANDIDATE_CACHE)
    # Deliberately loose discovery universe. The model must decide which signals
    # deserve execution; transaction costs remain in every trade label.
    cfg = StrategyConfig(
        "training_universe", min_10m_volume=0.3, min_5m_volume=0.3,
        max_extension_atr=3.0, confirmation_bars=1,
        require_ema_slope=False, min_reward_cost_multiple=0,
        cooldown_minutes=0,
    )
    if RAW_CANDIDATE_CACHE.exists():
        frame = pd.read_pickle(RAW_CANDIDATE_CACHE)
    else:
        trades = []
        for index, (inst_id, rows) in enumerate(data.items(), 1):
            trades.extend(backtest_symbol(inst_id, rows, cfg))
            print(f"candidate replay {index}/{len(data)} {inst_id}", file=sys.stderr, flush=True)
        records = []
        for trade in trades:
            local = datetime.fromtimestamp(trade["entry_time"] / 1000, UTC).astimezone(NY)
            minute = (local.hour * 60 + local.minute) - (9 * 60 + 30)
            record = {
                **trade,
                **trade["score_inputs"],
                "date": local.date().isoformat(),
                "side_long": 1.0 if trade["side"] == "LONG" else 0.0,
                "minute_sin": math.sin(2 * math.pi * minute / 390),
                "minute_cos": math.cos(2 * math.pi * minute / 390),
                "weekday": float(local.weekday()),
                "profitable": int(trade["net_r"] > 0),
            }
            records.append(record)
        frame = pd.DataFrame(records).sort_values("entry_time").reset_index(drop=True)
        RAW_CANDIDATE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        frame.to_pickle(RAW_CANDIDATE_CACHE)
    frame = _add_market_features(frame, data)
    frame.to_pickle(CANDIDATE_CACHE)
    return frame


def mean_reversion_frame(data: dict[str, list[list[str]]]) -> pd.DataFrame:
    cache = ROOT / "data" / "okx_mean_reversion_universe_90d.pkl"
    if cache.exists():
        return pd.read_pickle(cache)
    cfg = StrategyConfig(
        "mean_reversion_exit", max_holding_minutes=60, breakeven_r=1.0,
        scale_target_r=1.5, scale_fraction=0.5, trail_stop_r=1.0,
        enable_5m_reversal=False,
    )
    records = []
    for symbol_index, (inst_id, source_rows) in enumerate(data.items(), 1):
        rows_1m = closed_chronological(source_rows)
        rows_5m, rows_10m = aggregate_bars(rows_1m, 5), aggregate_bars(rows_1m, 10)
        one_stamps = [int(row[0]) for row in rows_1m]
        five_stamps = [int(row[0]) for row in rows_5m]
        five_close_stamps = [stamp + 300_000 for stamp in five_stamps]
        five_metrics_cache: dict[int, dict[str, float] | None] = {}
        blocked_until = 0
        for ten_index, ten_bar in enumerate(rows_10m):
            metrics10 = _metrics(rows_10m, ten_index)
            if not metrics10:
                continue
            decision_time = int(ten_bar[0]) + 600_000
            if decision_time < blocked_until or not _entry_allowed(decision_time):
                continue
            deviation = (metrics10["price"] - metrics10["vwap20"]) / metrics10["atr14"]
            long = deviation <= -0.3 and metrics10["rsi14"] <= 45
            short = deviation >= 0.3 and metrics10["rsi14"] >= 55
            if not (long or short):
                continue
            side = "LONG" if long else "SHORT"
            five_index = _bar_index_at_or_after(rows_5m, decision_time, five_stamps)
            if five_index is None or five_index < 1:
                continue
            metrics5 = _metrics(rows_5m, five_index)
            if not metrics5 or metrics5["volume_ratio"] < 0.3:
                continue
            row5, previous5 = rows_5m[five_index], rows_5m[five_index - 1]
            reversal = (
                float(row5[4]) > float(row5[1]) and float(row5[4]) > float(previous5[4])
                if side == "LONG"
                else float(row5[4]) < float(row5[1]) and float(row5[4]) < float(previous5[4])
            )
            if not reversal:
                continue
            confirm_time = int(row5[0]) + 300_000
            entry_index = _bar_index_at_or_after(rows_1m, confirm_time, one_stamps)
            if entry_index is None or entry_index >= len(rows_1m):
                continue
            entry_time = int(rows_1m[entry_index][0])
            if not _entry_allowed(entry_time):
                continue
            entry = float(rows_1m[entry_index][1])
            stop_distance = max(metrics5["atr14"] * 1.5, entry * 0.0035)
            result = simulate_exit(
                side, entry, entry_index, rows_1m, rows_5m, stop_distance,
                cfg, five_close_stamps, five_metrics_cache,
            )
            local = datetime.fromtimestamp(entry_time / 1000, UTC).astimezone(NY)
            minute = (local.hour * 60 + local.minute) - (9 * 60 + 30)
            prior_volume = [float(row[5]) for row in rows_1m[max(0, entry_index - 20):entry_index]]
            volume1 = float(rows_1m[entry_index - 1][5]) / max(sum(prior_volume) / len(prior_volume), 1e-9) if prior_volume else 0
            record = {
                "inst_id": inst_id, "side": side, "entry_time": entry_time,
                "session": "open30" if local.hour < 10 else "cash_rest",
                "entry_price": entry, "risk_bps": stop_distance / entry * 10_000,
                "date": local.date().isoformat(), "side_long": 1.0 if side == "LONG" else 0.0,
                "minute_sin": math.sin(2 * math.pi * minute / 390),
                "minute_cos": math.cos(2 * math.pi * minute / 390), "weekday": float(local.weekday()),
                "volume10": metrics10["volume_ratio"], "volume5": metrics5["volume_ratio"], "volume1": volume1,
                "extension_atr": abs(deviation),
                "trend10_atr": abs(metrics10["ema9"] - metrics10["ema21"]) / metrics10["atr14"],
                "slope10_atr": abs(metrics10["ema9_slope"]) / metrics10["atr14"],
                "vwap10_atr": abs(deviation),
                "rsi_directional": (50 - metrics10["rsi14"]) if side == "LONG" else (metrics10["rsi14"] - 50),
                "trend5_atr": abs(metrics5["ema9"] - metrics5["ema21"]) / metrics5["atr14"],
                "vwap5_atr": abs(metrics5["price"] - metrics5["vwap20"]) / metrics5["atr14"],
                "breakout_buffer_atr": 0.0,
                "confirmation_body_atr": abs(float(row5[4]) - float(row5[1])) / metrics5["atr14"],
                **result,
            }
            record["profitable"] = int(record["net_r"] > 0)
            records.append(record)
            blocked_until = result["exit_time"]
        print(f"mean reversion replay {symbol_index}/{len(data)} {inst_id}", file=sys.stderr, flush=True)
    frame = pd.DataFrame(records).sort_values("entry_time").reset_index(drop=True)
    frame = _add_market_features(frame, data)
    frame.to_pickle(cache)
    return frame


def cross_sectional_momentum_frame(data: dict[str, list[list[str]]]) -> pd.DataFrame:
    cache = ROOT / "data" / "okx_cross_sectional_universe_90d.pkl"
    if cache.exists():
        return pd.read_pickle(cache)
    prepared: dict[str, dict[str, Any]] = {}
    opening_by_day: dict[str, list[dict[str, Any]]] = {}
    for inst_id, rows in data.items():
        rows_1m = closed_chronological(rows)
        rows_5m = aggregate_bars(rows_1m, 5)
        stamps = [int(row[0]) for row in rows_1m]
        by_day: dict[str, list[int]] = {}
        for index, stamp in enumerate(stamps):
            local = datetime.fromtimestamp(stamp / 1000, UTC).astimezone(NY)
            if local.weekday() < 5 and local.hour == 9 and local.minute >= 30:
                by_day.setdefault(local.date().isoformat(), []).append(index)
        for day, indices in by_day.items():
            indices = [index for index in indices if datetime.fromtimestamp(stamps[index] / 1000, UTC).astimezone(NY).minute < 60]
            indices = indices[:30]
            if len(indices) < 25:
                continue
            first, last = rows_1m[indices[0]], rows_1m[indices[-1]]
            opening_return = float(last[4]) / float(first[1]) - 1
            entry_index = bisect_left(stamps, int(last[0]) + 60_000)
            if entry_index >= len(rows_1m):
                continue
            opening_by_day.setdefault(day, []).append({
                "inst_id": inst_id, "return": opening_return,
                "range_bps": (max(float(rows_1m[i][2]) for i in indices) - min(float(rows_1m[i][3]) for i in indices)) / float(first[1]) * 10_000,
                "volume": sum(float(rows_1m[i][5]) for i in indices),
                "entry_index": entry_index,
            })
        prepared[inst_id] = {
            "rows_1m": rows_1m, "rows_5m": rows_5m,
            "five_close_stamps": [int(row[0]) + 300_000 for row in rows_5m],
        }
    cfg = StrategyConfig(
        "cross_close", max_holding_minutes=330, breakeven_r=100,
        scale_target_r=100, scale_fraction=0, trail_stop_r=100,
        enable_5m_reversal=False,
    )
    records = []
    for day, rows in sorted(opening_by_day.items()):
        if len(rows) < 10:
            continue
        ranked = sorted(rows, key=lambda row: row["return"])
        chosen = [(row, "SHORT", rank) for rank, row in enumerate(ranked[:3], 1)]
        chosen += [(row, "LONG", rank) for rank, row in enumerate(reversed(ranked[-3:]), 1)]
        median_volume = float(np.median([row["volume"] for row in rows])) or 1.0
        for row, side, rank in chosen:
            prepared_symbol = prepared[row["inst_id"]]
            rows_1m, rows_5m = prepared_symbol["rows_1m"], prepared_symbol["rows_5m"]
            entry_index = row["entry_index"]
            entry_time, entry = int(rows_1m[entry_index][0]), float(rows_1m[entry_index][1])
            stop_distance = entry * 0.008
            result = simulate_exit(
                side, entry, entry_index, rows_1m, rows_5m, stop_distance,
                cfg, prepared_symbol["five_close_stamps"], {},
            )
            local = datetime.fromtimestamp(entry_time / 1000, UTC).astimezone(NY)
            signed_return = row["return"] if side == "LONG" else -row["return"]
            record = {
                "inst_id": row["inst_id"], "side": side, "entry_time": entry_time,
                "session": "cash_rest", "entry_price": entry,
                "risk_bps": 80.0, "date": day, "side_long": 1.0 if side == "LONG" else 0.0,
                "minute_sin": math.sin(2 * math.pi * 30 / 390),
                "minute_cos": math.cos(2 * math.pi * 30 / 390), "weekday": float(local.weekday()),
                "volume10": row["volume"] / median_volume, "volume5": 0.0, "volume1": 0.0,
                "extension_atr": abs(row["return"] * 10_000) / max(row["range_bps"], 1),
                "trend10_atr": abs(row["return"] * 10_000) / max(row["range_bps"], 1),
                "slope10_atr": 0.0, "vwap10_atr": 0.0,
                "rsi_directional": signed_return * 10_000,
                "trend5_atr": 0.0, "vwap5_atr": 0.0,
                "breakout_buffer_atr": 0.0, "confirmation_body_atr": 0.0,
                "cross_rank": float(rank), "opening_return_bps": signed_return * 10_000,
                **result,
            }
            record["profitable"] = int(record["net_r"] > 0)
            records.append(record)
    frame = pd.DataFrame(records).sort_values("entry_time").reset_index(drop=True)
    frame = _add_market_features(frame, data)
    frame.to_pickle(cache)
    return frame


def cross_sectional_reversal_frame(momentum: pd.DataFrame,
                                   data: dict[str, list[list[str]]]) -> pd.DataFrame:
    cache = ROOT / "data" / "okx_cross_sectional_reversal_90d.pkl"
    if cache.exists():
        return pd.read_pickle(cache)
    flipped = momentum.copy()
    flipped["side"] = flipped["side"].map({"LONG": "SHORT", "SHORT": "LONG"})
    flipped["side_long"] = 1.0 - flipped["side_long"].astype(float)
    flipped["opening_return_bps"] = -flipped["opening_return_bps"].astype(float)
    flipped["rsi_directional"] = -flipped["rsi_directional"].astype(float)
    cfg = StrategyConfig(
        "cross_reversal_close", max_holding_minutes=330,
        breakeven_r=100, scale_target_r=100, scale_fraction=0,
        trail_stop_r=100, enable_5m_reversal=False,
    )
    result = reprice_candidates(flipped, data, cfg, 1.0)
    result.to_pickle(cache)
    return result


def adaptive_cross_frame(momentum: pd.DataFrame, reversal: pd.DataFrame,
                         lookback_days: int) -> pd.DataFrame:
    momentum = momentum[momentum["cross_rank"] <= 2].copy()
    reversal = reversal[reversal["cross_rank"] <= 2].copy()
    days = sorted(set(momentum["date"]) & set(reversal["date"]))
    daily_momentum = momentum.groupby("date")["net_r"].sum().to_dict()
    daily_reversal = reversal.groupby("date")["net_r"].sum().to_dict()
    selected = []
    choices = []
    for index, day in enumerate(days):
        history = days[max(0, index - lookback_days):index]
        momentum_score = sum(float(daily_momentum.get(item, 0)) for item in history)
        reversal_score = sum(float(daily_reversal.get(item, 0)) for item in history)
        use_reversal = bool(history) and reversal_score > momentum_score
        source = reversal if use_reversal else momentum
        rows = source[source["date"] == day].copy()
        rows["adaptive_mode"] = "reversal" if use_reversal else "momentum"
        rows["adaptive_momentum_score"] = momentum_score
        rows["adaptive_reversal_score"] = reversal_score
        selected.append(rows)
        choices.append(use_reversal)
    result = pd.concat(selected, ignore_index=True).sort_values("entry_time").reset_index(drop=True)
    result.attrs["reversal_days"] = sum(choices)
    return result


def exit_profiles() -> list[tuple[StrategyConfig, float]]:
    return [
        (StrategyConfig("current_exit"), 1.0),
        (StrategyConfig(
            "cost_aware_90m", max_holding_minutes=90, breakeven_r=1.2,
            scale_target_r=2.0, scale_fraction=0.33, trail_stop_r=1.5,
        ), 1.5),
        (StrategyConfig(
            "trend_runner_120m", max_holding_minutes=120, breakeven_r=1.5,
            scale_target_r=2.5, scale_fraction=0.25, trail_stop_r=2.0,
            enable_5m_reversal=False,
        ), 1.25),
        (StrategyConfig(
            "wide_trend_180m", max_holding_minutes=180, breakeven_r=1.5,
            scale_target_r=3.0, scale_fraction=0.25, trail_stop_r=2.5,
            enable_5m_reversal=False,
        ), 2.0),
    ]


def reprice_candidates(frame: pd.DataFrame, data: dict[str, list[list[str]]],
                       cfg: StrategyConfig, risk_multiplier: float) -> pd.DataFrame:
    cache = ROOT / "data" / f"okx_candidate_universe_90d_{cfg.name}.pkl"
    if cache.exists():
        return pd.read_pickle(cache)
    updated = []
    for symbol_index, (inst_id, symbol_frame) in enumerate(frame.groupby("inst_id"), 1):
        rows_1m = sorted(data[inst_id], key=lambda row: int(row[0]))
        rows_5m = aggregate_bars(rows_1m, 5)
        one_stamps = [int(row[0]) for row in rows_1m]
        five_close_stamps = [int(row[0]) + 300_000 for row in rows_5m]
        five_metrics_cache: dict[int, dict[str, float] | None] = {}
        for record in symbol_frame.to_dict("records"):
            entry_index = bisect_left(one_stamps, int(record["entry_time"]))
            if entry_index >= len(rows_1m):
                continue
            entry = float(record["entry_price"])
            stop_distance = entry * float(record["risk_bps"]) / 10_000 * risk_multiplier
            result = simulate_exit(
                record["side"], entry, entry_index, rows_1m, rows_5m,
                stop_distance, cfg, five_close_stamps, five_metrics_cache,
            )
            record.update(result)
            record["risk_bps"] = stop_distance / entry * 10_000
            record["profitable"] = int(result["net_r"] > 0)
            updated.append(record)
        print(f"exit replay {symbol_index}/{frame['inst_id'].nunique()} {cfg.name} {inst_id}", file=sys.stderr, flush=True)
    result = pd.DataFrame(updated).sort_values("entry_time").reset_index(drop=True)
    result.to_pickle(cache)
    return result


def profile_comparison(profiles: dict[str, pd.DataFrame], session_days: list[str]) -> list[dict[str, Any]]:
    train_days, validation_days, development_days = session_days[:40], session_days[40:50], session_days[50:60]
    outcomes = []
    for name, frame in profiles.items():
        train = frame[frame["date"].isin(train_days)].to_dict("records")
        validation = frame[frame["date"].isin(validation_days)].to_dict("records")
        development = frame[frame["date"].isin(development_days)].to_dict("records")
        outcomes.append({
            "profile": name, "train": stats(train),
            "validation": stats(validation), "development": stats(development),
        })
    return sorted(
        outcomes,
        key=lambda row: (
            min(
                row["train"]["profit_factor"] or 0,
                row["validation"]["profit_factor"] or 0,
                row["development"]["profit_factor"] or 0,
            ),
            row["validation"]["net_r"] + row["development"]["net_r"],
        ),
        reverse=True,
    )


def _matrix(frame: pd.DataFrame, symbols: list[str] | None = None) -> tuple[np.ndarray, list[str]]:
    symbols = symbols or sorted(frame["inst_id"].unique())
    numeric = frame.reindex(columns=list(FEATURE_COLUMNS), fill_value=0).astype(float).to_numpy()
    one_hot = np.column_stack([(frame["inst_id"] == symbol).astype(float).to_numpy() for symbol in symbols])
    return np.column_stack([numeric, one_hot]), symbols


def _portfolio_filter(frame: pd.DataFrame, probability: np.ndarray, threshold: float,
                      max_positions: int = 5) -> list[dict[str, Any]]:
    rows = frame.copy()
    rows["probability"] = probability
    rows = rows[rows["probability"] >= threshold].sort_values(["entry_time", "probability"], ascending=[True, False])
    active: list[int] = []
    selected: list[dict[str, Any]] = []
    for record in rows.to_dict("records"):
        active = [stamp for stamp in active if stamp > int(record["entry_time"])]
        if len(active) >= max_positions:
            continue
        active.append(int(record["exit_time"]))
        selected.append(record)
    return selected


def _evaluation(frame: pd.DataFrame, probability: np.ndarray, threshold: float,
                target: str = "profitable") -> dict[str, Any]:
    selected = _portfolio_filter(frame, probability, threshold)
    result = stats(selected)
    breakdown = {}
    for field in ("inst_id", "side", "session", "exit_reason"):
        values = {}
        for value in sorted({str(row[field]) for row in selected}):
            values[value] = stats([row for row in selected if str(row[field]) == value])
        breakdown[field] = values
    result.update(
        threshold=round(threshold, 3),
        expectancy_r=result["avg_r"],
        auc=(round(roc_auc_score(frame[target].astype(int), probability), 4)
             if len(set(frame[target].astype(int))) > 1 else None),
        breakdown=breakdown,
    )
    return result


def _models() -> dict[str, Any]:
    return {
        "logistic": make_pipeline(StandardScaler(), LogisticRegression(C=0.3, max_iter=1000, class_weight="balanced")),
        "hist_gb": HistGradientBoostingClassifier(
            learning_rate=0.04, max_iter=180, max_leaf_nodes=7,
            min_samples_leaf=25, l2_regularization=3.0, random_state=42,
        ),
        "random_forest": RandomForestClassifier(
            n_estimators=400, max_depth=5, min_samples_leaf=15,
            max_features=0.7, class_weight="balanced_subsample",
            random_state=42, n_jobs=-1,
        ),
    }


def train_walkforward(frame: pd.DataFrame, session_days: list[str],
                      open_holdout: bool = True) -> dict[str, Any]:
    if len(session_days) < 90:
        raise RuntimeError(f"need 90 sessions, only have {len(session_days)}")
    train_days = session_days[:40]
    validation_days = session_days[40:50]
    development_days = session_days[50:60]
    holdout_days = session_days[60:90]
    train = frame[frame["date"].isin(train_days)].copy()
    validation = frame[frame["date"].isin(validation_days)].copy()
    development = frame[frame["date"].isin(development_days)].copy()
    holdout = frame[frame["date"].isin(holdout_days)].copy()
    x_train, symbols = _matrix(train)
    x_validation, _ = _matrix(validation, symbols)
    x_development, _ = _matrix(development, symbols)
    x_holdout, _ = _matrix(holdout, symbols)
    baseline = {
        "train": stats(train.to_dict("records")),
        "validation": stats(validation.to_dict("records")),
        "development": stats(development.to_dict("records")),
        "holdout": stats(holdout.to_dict("records")),
    }
    candidates = []
    trained: dict[str, Any] = {}
    for target in ("profitable", "scaled"):
        y_train = train[target].astype(int).to_numpy()
        if len(set(y_train)) < 2:
            continue
        for name, model in _models().items():
            model_key = f"{target}:{name}"
            model.fit(x_train, y_train)
            trained[model_key] = model
            validation_probability = model.predict_proba(x_validation)[:, 1]
            for threshold in np.arange(0.35, 0.751, 0.01):
                evaluation = _evaluation(validation, validation_probability, float(threshold), target)
                if evaluation["trades"] >= 25:
                    candidates.append({"model": model_key, "target": target, "threshold": float(threshold), "validation": evaluation})
    eligible = [
        row for row in candidates
        if row["validation"]["net_r"] > 0
        and (row["validation"]["profit_factor"] or 0) > 1.1
        and row["validation"]["expectancy_r"] > 0.05
    ]
    best_validation_only = max(
        eligible,
        key=lambda row: ((row["validation"]["profit_factor"] or 0), row["validation"]["net_r"]),
        default=None,
    )
    robust_candidates = []
    for row in eligible:
        probability = trained[row["model"]].predict_proba(x_development)[:, 1]
        development_evaluation = _evaluation(
            development, probability, row["threshold"], row["target"]
        )
        if (
            development_evaluation["trades"] >= 25
            and development_evaluation["net_r"] > 0
            and (development_evaluation["profit_factor"] or 0) > 1.1
            and development_evaluation["avg_r"] > 0.05
        ):
            robust_candidates.append({**row, "development": development_evaluation})
    best = max(
        robust_candidates,
        key=lambda row: (
            min(
                row["validation"]["profit_factor"] or 0,
                row["development"]["profit_factor"] or 0,
            ),
            row["validation"]["net_r"] + row["development"]["net_r"],
        ),
        default=best_validation_only,
    )
    holdout_result = None
    development_result = None
    importance: list[dict[str, Any]] = []
    passed = False
    if best:
        try:
            measured = permutation_importance(
                trained[best["model"]], x_validation, validation[best["target"]].astype(int).to_numpy(),
                scoring="roc_auc", n_repeats=8, random_state=42, n_jobs=-1,
            )
            names = list(FEATURE_COLUMNS) + [f"symbol:{symbol}" for symbol in symbols]
            importance = [
                {"feature": names[index], "auc_importance": round(float(measured.importances_mean[index]), 5)}
                for index in np.argsort(measured.importances_mean)[::-1][:12]
            ]
        except ValueError:
            importance = []
        development_probability = trained[best["model"]].predict_proba(x_development)[:, 1]
        development_result = _evaluation(development, development_probability, best["threshold"], best["target"])
        development_passed = (
            development_result["trades"] >= 25
            and development_result["net_r"] > 0
            and (development_result["profit_factor"] or 0) > 1.1
            and development_result["expectancy_r"] > 0.05
        )
        # Do not even inspect the sealed final 30 sessions unless both model
        # selection stages pass. This keeps room for structural research
        # without repeatedly optimizing against the final answer set.
        if development_passed and open_holdout:
            probability = trained[best["model"]].predict_proba(x_holdout)[:, 1]
            holdout_result = _evaluation(holdout, probability, best["threshold"], best["target"])
            passed = (
                holdout_result["trades"] >= 100
                and holdout_result["net_r"] > 0
                and (holdout_result["profit_factor"] or 0) > 1.2
                and holdout_result["expectancy_r"] > 0.1
            )
    return {
        "split": {
            "train": [train_days[0], train_days[-1]],
            "validation": [validation_days[0], validation_days[-1]],
            "development": [development_days[0], development_days[-1]],
            "holdout": [holdout_days[0], holdout_days[-1]],
            "candidate_counts": {"train": len(train), "validation": len(validation), "development": len(development), "holdout": len(holdout)},
        },
        "selection_gate": "validation and development each n>=25, PF>1.1, netR>0, expectancy>0.05R",
        "deployment_gate": "untouched holdout n>=100, PF>1.2, netR>0, expectancy>0.1R",
        "baseline": baseline,
        "best_validation": best,
        "development": development_result,
        "holdout": holdout_result,
        "passed": passed,
        "feature_importance": importance,
        "leaderboard": sorted(candidates, key=lambda row: ((row["validation"]["profit_factor"] or 0), row["validation"]["net_r"]), reverse=True)[:20],
        "feature_columns": list(FEATURE_COLUMNS) + [f"symbol:{symbol}" for symbol in symbols],
        "model": trained.get(best["model"]) if best else None,
        "symbols": symbols,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=90)
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--output", default=str(ROOT / "data" / "okx_walkforward_90d.json"))
    parser.add_argument("--model-output", default=str(ROOT / "data" / "okx_signal_quality_model.joblib"))
    args = parser.parse_args()
    end = datetime.now(NY).date() - timedelta(days=1)
    days = weekday_sessions(end, args.sessions)
    symbols = tuple(item.strip().upper() for item in args.symbols.split(",") if item.strip())
    data = load_market_data(OKX(settings()), symbols, days)
    frame = candidate_frame(data)
    session_days = [day.isoformat() for day in days]
    profile_frames = {}
    for cfg, risk_multiplier in exit_profiles():
        profile_frames[cfg.name] = (
            frame if cfg.name == "current_exit"
            else reprice_candidates(frame, data, cfg, risk_multiplier)
        )
    profile_frames["mean_reversion"] = mean_reversion_frame(data)
    cross = cross_sectional_momentum_frame(data)
    cross_reversal = cross_sectional_reversal_frame(cross, data)
    cross_120_cfg = StrategyConfig(
        "cross_120m", max_holding_minutes=120, breakeven_r=100,
        scale_target_r=100, scale_fraction=0, trail_stop_r=100,
        enable_5m_reversal=False,
    )
    cross_120 = reprice_candidates(cross, data, cross_120_cfg, 0.75)
    profile_frames["cross3_close"] = cross
    profile_frames["cross2_close"] = cross[cross["cross_rank"] <= 2].copy()
    profile_frames["cross3_120m"] = cross_120
    profile_frames["cross2_120m"] = cross_120[cross_120["cross_rank"] <= 2].copy()
    profile_frames["cross_reversal3_close"] = cross_reversal
    profile_frames["cross_reversal2_close"] = cross_reversal[cross_reversal["cross_rank"] <= 2].copy()
    for lookback in (5, 10, 20):
        profile_frames[f"cross_adaptive_{lookback}d"] = adaptive_cross_frame(cross, cross_reversal, lookback)
    for stop_bps, multiplier in ((100, 1.25), (120, 1.5), (160, 2.0)):
        cfg = StrategyConfig(
            f"cross_close_stop{stop_bps}", max_holding_minutes=330,
            breakeven_r=100, scale_target_r=100, scale_fraction=0,
            trail_stop_r=100, enable_5m_reversal=False,
        )
        wider = reprice_candidates(cross, data, cfg, multiplier)
        profile_frames[f"cross3_close_stop{stop_bps}"] = wider
        profile_frames[f"cross2_close_stop{stop_bps}"] = wider[wider["cross_rank"] <= 2].copy()
    profiles = profile_comparison(profile_frames, session_days)
    model_research = []
    for profile_name, profile_frame in profile_frames.items():
        research = train_walkforward(profile_frame, session_days, open_holdout=False)
        research.pop("model", None)
        model_research.append({
            "profile": profile_name,
            "best_validation": research["best_validation"],
            "development": research["development"],
        })
    eligible_models = [
        row for row in model_research
        if row["best_validation"] and row["development"]
        and row["development"]["trades"] >= 25
        and row["development"]["net_r"] > 0
        and (row["development"]["profit_factor"] or 0) > 1.1
        and row["development"]["avg_r"] > 0.05
    ]
    selected_research = max(
        eligible_models,
        key=lambda row: (
            min(
                row["best_validation"]["validation"]["profit_factor"] or 0,
                row["development"]["profit_factor"] or 0,
            ),
            row["development"]["net_r"],
        ),
        default=None,
    )
    selected_profile = selected_research["profile"] if selected_research else profiles[0]["profile"]
    result = train_walkforward(profile_frames[selected_profile], session_days, open_holdout=bool(selected_research))
    model = result.pop("model")
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "sessions": len(days), "symbols": list(symbols),
        "cost_bps": 14, "candidate_count": len(frame),
        "exit_profiles": profiles, "selected_exit_profile": selected_profile,
        "model_profile_research": model_research,
        **result,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    if payload["passed"] and model is not None:
        joblib.dump({
            "model": model,
            "target": payload["best_validation"]["target"],
            "threshold": payload["best_validation"]["threshold"],
            "features": payload["feature_columns"],
        }, args.model_output)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
