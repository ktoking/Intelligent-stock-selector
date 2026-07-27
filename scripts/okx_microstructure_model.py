#!/usr/bin/env python3
"""Develop, freeze and forward-test an execution-equivalent microstructure model."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_intraday_agent import DB_PATH  # noqa: E402
from scripts.okx_return_model_research import metric  # noqa: E402

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
STATE_PATH = ROOT / "data" / "okx_microstructure_model_state.json"
ARTIFACT_PATH = ROOT / "data" / "okx_microstructure_model.joblib"
LOG = logging.getLogger("okx-micro-model")
RESEARCH_VERSION = "micro_barrier_cash_horizon_v13"
WARMUP_DEMO_ENV = "OKX_MICRO_V13_WARMUP_DEMO"
IMPACT_BUFFER_BPS = 4.0
NON_US_CASH_SYMBOLS = {"SAMSUNG-USDT-SWAP"}
DEV_STOP_BPS = (50, 75)
DEV_TARGET_R = (1.5, 2.0)
DEV_HORIZONS = (15, 30)
DEV_MODELS = ("ridge", "hist7")
DEV_THRESHOLDS = (.05, .1, .2)
MIN_QUALIFIED_MINUTES = 300
MIN_QUALIFIED_SPAN_MINUTES = 380
MIN_QUALIFIED_SYMBOLS = 10
MIN_MINUTES_PER_SYMBOL = 180
# A completed feature minute is normally acted on in the following minute.
# More delay changes the realized entry/holding window relative to development
# labels, so it is never admissible for the strict forward experiment.
MAX_FORWARD_FEATURE_AGE_SECONDS = 90
FEATURES = (
    "book_imbalance", "aggressive_imbalance", "flow_interaction", "spread_bps",
    "microprice_offset_bps", "log_trade_count", "log_flow_volume",
    "log_bid_depth_usd", "log_ask_depth_usd", "book_lag1", "aggressive_lag1",
    "depth_normalized_ofi", "ofi_aggressive_interaction", "log_quote_updates",
    "ofi_lag1", "mid_return_1m_bps", "flow_persistence", "minute_sin", "minute_cos",
)


def warmup_demo_enabled() -> bool:
    return os.getenv(WARMUP_DEMO_ENV) == "1"


def warmup_artifact() -> dict[str, Any]:
    """Create an explicitly isolated Demo artifact; never eligible for promotion."""
    now = int(time.time() // 60 * 60)
    config = {"stop_bps": 50, "target_r": 1.5, "horizon": 15, "model": "v13_warmup", "threshold": .35}
    digest = "warmup-demo-not-training-data"
    experiment_id = "micro_barrier_" + experiment_fingerprint(config, [], now, digest)
    # Use a normal sklearn estimator so the Demo artifact can be loaded by a
    # separately supervised executor process.  Coefficients encode the fixed
    # warmup score; they are not fitted from market outcomes.
    weights = np.zeros(len(FEATURES))
    weights[[0, 1, 2, 11, 15]] = [.25, .30, .15, .15, .015]
    design = np.vstack([np.zeros(len(FEATURES)), np.eye(len(FEATURES)), -np.eye(len(FEATURES))])
    long_model = Ridge(alpha=1e-8, fit_intercept=False).fit(design, design @ weights)
    short_model = Ridge(alpha=1e-8, fit_intercept=False).fit(design, -(design @ weights))
    return {"experiment_id": experiment_id, "experiment_started_at": datetime.now(UTC).isoformat(),
            "research_version": RESEARCH_VERSION, "config": config, "features": FEATURES,
            "development_cohort": 0, "development_days": [], "training_data_end": now,
            "training_data_digest": digest, "long_model": long_model,
            "short_model": short_model, "warmup_demo": True}


def is_cash(stamp: int) -> bool:
    local = datetime.fromtimestamp(stamp, UTC).astimezone(NY)
    return local.weekday() < 5 and (local.hour, local.minute) >= (9, 30) and (local.hour, local.minute) < (16, 0)


def ensure_schema() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS okx_micro_model_signals (
                signal_key TEXT PRIMARY KEY, experiment_id TEXT NOT NULL,
                inst_id TEXT NOT NULL, side TEXT NOT NULL,
                feature_minute INTEGER, entry_minute INTEGER NOT NULL, due_minute INTEGER NOT NULL,
                entry_price REAL NOT NULL, stop_price REAL NOT NULL, target_price REAL NOT NULL,
                risk_price REAL NOT NULL, predicted_r REAL NOT NULL,
                signal_generated_at TEXT, entry_quote_source TEXT, cost_bps REAL,
                exit_minute INTEGER, exit_price REAL, net_r REAL, exit_reason TEXT,
                labeled_at TEXT, created_at TEXT NOT NULL
            )
        """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(okx_micro_model_signals)")}
        for column, declaration in (("feature_minute", "INTEGER"),
                                    ("signal_generated_at", "TEXT"),
                                    ("entry_quote_source", "TEXT"),
                                    ("cost_bps", "REAL")):
            if column not in columns:
                conn.execute(f"ALTER TABLE okx_micro_model_signals ADD COLUMN {column} {declaration}")


def _rows(since_ts: int | None = None) -> list[sqlite3.Row]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(okx_microstructure_minute)")}
            required = {"ct_val", "demo_tradeable", "book_age_seconds", "capture_complete",
                        "depth_normalized_ofi", "quote_updates", "taker_fee_bps",
                        "min_bid_px", "max_bid_px", "min_ask_px", "max_ask_px"}
            if not required.issubset(columns):
                return []
            if since_ts is None:
                return conn.execute("SELECT * FROM okx_microstructure_minute ORDER BY inst_id,minute_ts").fetchall()
            return conn.execute("""SELECT * FROM okx_microstructure_minute
                                   WHERE minute_ts>=? ORDER BY inst_id,minute_ts""",
                                (int(since_ts),)).fetchall()
        except sqlite3.OperationalError:
            return []


def row_float(row: Any, key: str, fallback: str | None = None) -> float:
    keys = row.keys() if hasattr(row, "keys") else ()
    value = row[key] if key in keys else row[fallback] if fallback else 0
    return float(value or 0)


def feature_frame(rows: list[sqlite3.Row]) -> pd.DataFrame:
    records = []
    for row in rows:
        stamp = int(row["minute_ts"])
        bid, ask, ct_val = float(row["bid_px"] or 0), float(row["ask_px"] or 0), float(row["ct_val"] or 0)
        taker_fee_bps = float(row["taker_fee_bps"] or 0)
        path = [row_float(row, key, fallback) for key, fallback in (
            ("min_bid_px", "bid_px"), ("max_bid_px", "bid_px"),
            ("min_ask_px", "ask_px"), ("max_ask_px", "ask_px"),
        )]
        if (row["inst_id"] == "BTC-USDT-SWAP" or row["inst_id"] in NON_US_CASH_SYMBOLS
                or not int(row["demo_tradeable"] or 0) or not is_cash(stamp)
                or not int(row["capture_complete"] or 0) or float(row["book_age_seconds"] or 999) > 10
                or bid <= 0 or ask <= bid or ct_val <= 0 or taker_fee_bps <= 0
                or min(path) <= 0
                or int(row["trade_count"] or 0) <= 0):
            continue
        mid = (bid + ask) / 2
        local = datetime.fromtimestamp(stamp, UTC).astimezone(NY)
        book, aggressive = float(row["book_imbalance"] or 0), float(row["aggressive_imbalance"] or 0)
        records.append({
            "minute_ts": stamp, "date": local.date().isoformat(), "inst_id": row["inst_id"],
            "bid_px": bid, "ask_px": ask, "mid": mid, "ct_val": ct_val,
            "taker_fee_bps": taker_fee_bps,
            "spread_bps": float(row["spread_bps"] or 0),
            "bid_depth_usd": float(row["bid_depth"] or 0) * mid * ct_val,
            "ask_depth_usd": float(row["ask_depth"] or 0) * mid * ct_val,
            "book_imbalance": book, "aggressive_imbalance": aggressive,
            "flow_interaction": book * aggressive,
            "microprice_offset_bps": (float(row["microprice"] or mid) / mid - 1) * 10_000,
            "log_trade_count": math.log1p(int(row["trade_count"] or 0)),
            "log_flow_volume": math.log1p(float(row["buy_volume"] or 0) + float(row["sell_volume"] or 0)),
            "log_bid_depth_usd": math.log1p(float(row["bid_depth"] or 0) * mid * ct_val),
            "log_ask_depth_usd": math.log1p(float(row["ask_depth"] or 0) * mid * ct_val),
            "depth_normalized_ofi": float(row["depth_normalized_ofi"] or 0),
            "ofi_aggressive_interaction": float(row["depth_normalized_ofi"] or 0) * aggressive,
            "log_quote_updates": math.log1p(int(row["quote_updates"] or 0)),
            "minute_sin": math.sin(2 * math.pi * (local.hour * 60 + local.minute - 570) / 390),
            "minute_cos": math.cos(2 * math.pi * (local.hour * 60 + local.minute - 570) / 390),
        })
    frame = pd.DataFrame(records)
    if frame.empty:
        return frame
    frame = frame.sort_values(["inst_id", "minute_ts"])
    groups = frame.groupby("inst_id", sort=False)
    previous_minute = groups["minute_ts"].shift(1)
    contiguous = frame["minute_ts"] - previous_minute == 60
    previous_mid = groups["mid"].shift(1)
    frame["book_lag1"] = groups["book_imbalance"].shift(1).where(contiguous)
    frame["aggressive_lag1"] = groups["aggressive_imbalance"].shift(1).where(contiguous)
    frame["ofi_lag1"] = groups["depth_normalized_ofi"].shift(1).where(contiguous)
    frame["mid_return_1m_bps"] = ((frame["mid"] / previous_mid - 1) * 10_000).where(contiguous)
    frame["flow_persistence"] = frame["book_imbalance"] * frame["book_lag1"]
    return frame.replace([np.inf, -np.inf], np.nan).dropna(subset=list(FEATURES)).reset_index(drop=True)


def quote_lookup(rows: list[sqlite3.Row]) -> dict[tuple[str, int], tuple[float, ...]]:
    return {
        (row["inst_id"], int(row["minute_ts"])): (
            float(row["bid_px"] or 0), float(row["ask_px"] or 0),
            row_float(row, "min_bid_px", "bid_px"), row_float(row, "max_bid_px", "bid_px"),
            row_float(row, "min_ask_px", "ask_px"), row_float(row, "max_ask_px", "ask_px"),
        )
        for row in rows if int(row["capture_complete"] or 0) and float(row["book_age_seconds"] or 999) <= 10
        and all(row_float(row, key, fallback) > 0 for key, fallback in (
            ("min_bid_px", "bid_px"), ("max_bid_px", "bid_px"),
            ("min_ask_px", "ask_px"), ("max_ask_px", "ask_px"),
        ))
    }


def quote_path(quote: tuple[float, ...]) -> tuple[float, float, float, float, float, float]:
    """Return close and intraminute executable quote extrema; accept legacy tests with two values."""
    bid, ask = float(quote[0]), float(quote[1])
    if len(quote) >= 6:
        return bid, ask, *(float(value) for value in quote[2:6])
    return bid, ask, bid, bid, ask, ask


def cash_day_diagnostics(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """Explain whether each observed US cash session is development-grade."""
    if frame.empty or not {"date", "minute_ts", "inst_id"}.issubset(frame.columns):
        return []
    result = []
    for date, group in frame.groupby("date", sort=True):
        minutes = group["minute_ts"].nunique()
        span = (int(group["minute_ts"].max()) - int(group["minute_ts"].min())) / 60
        symbols = group["inst_id"].nunique()
        symbol_minutes = group.groupby("inst_id")["minute_ts"].nunique()
        dense_symbols = int((symbol_minutes >= MIN_MINUTES_PER_SYMBOL).sum())
        checks = {
            "minutes": minutes >= MIN_QUALIFIED_MINUTES,
            "span": span >= MIN_QUALIFIED_SPAN_MINUTES,
            "dense_symbols": dense_symbols >= MIN_QUALIFIED_SYMBOLS,
        }
        result.append({
            "date": str(date), "qualified": all(checks.values()),
            "valid_minutes": int(minutes), "span_minutes": int(span),
            "symbols": int(symbols), "dense_symbols": dense_symbols,
            "minimum_minutes_per_symbol": MIN_MINUTES_PER_SYMBOL,
            "checks": checks,
        })
    return result


def qualified_cash_days(frame: pd.DataFrame) -> list[str]:
    """Accept only sessions with broad, near-open-to-close feature coverage."""
    return [row["date"] for row in cash_day_diagnostics(frame) if row["qualified"]]


def barrier_outcome(record: dict[str, Any], quotes: dict[tuple[str, int], tuple[float, ...]],
                    side: str, stop_bps: int, target_r: float, horizon: int) -> float | None:
    direction = 1 if side == "LONG" else -1
    entry = float(record["ask_px"] if direction > 0 else record["bid_px"])
    risk = entry * stop_bps / 10_000
    cost_bps = 2 * float(record["taker_fee_bps"]) + IMPACT_BUFFER_BPS
    cost_r = (cost_bps / 10_000) / (risk / entry)
    for offset in range(1, horizon + 1):
        quote = quotes.get((record["inst_id"], int(record["minute_ts"]) + offset * 60))
        if not quote or quote[0] <= 0 or quote[1] <= quote[0]:
            return None
        bid, ask, min_bid, max_bid, min_ask, max_ask = quote_path(quote)
        stop_touched = min_bid <= entry - risk if direction > 0 else max_ask >= entry + risk
        target_touched = max_bid >= entry + target_r * risk if direction > 0 else min_ask <= entry - target_r * risk
        if stop_touched:
            return -1 - cost_r
        if target_touched:
            return target_r - cost_r
        executable = bid if direction > 0 else ask
        move = (executable - entry) * direction
    return move / risk - cost_r


def executable_opportunities(frame: pd.DataFrame) -> pd.DataFrame:
    return frame[(frame["minute_ts"] % 300 == 0) & (frame["spread_bps"] <= 5)
                 & (frame["bid_depth_usd"] >= 5_000) & (frame["ask_depth_usd"] >= 5_000)].copy()


def cash_horizon_complete(feature_minute: int, horizon: int) -> bool:
    """The feature minute closes one minute later; the full holding window must end by 16:00 ET."""
    return entry_horizon_complete(int(feature_minute) + 60, horizon)


def entry_horizon_complete(entry_minute: int, horizon: int) -> bool:
    """Accept an entry only when its full executable holding window stays in cash hours."""
    start = datetime.fromtimestamp(int(entry_minute), UTC).astimezone(NY)
    end = datetime.fromtimestamp(int(entry_minute) + int(horizon) * 60, UTC).astimezone(NY)
    return (start.date() == end.date() and start.weekday() < 5
            and (start.hour, start.minute) >= (9, 30)
            and (end.hour, end.minute) <= (16, 0))


def horizon_opportunities(frame: pd.DataFrame, horizon: int) -> pd.DataFrame:
    value = executable_opportunities(frame)
    if value.empty:
        return value
    return value[value["minute_ts"].map(lambda stamp: cash_horizon_complete(int(stamp), horizon))].copy()


def labeled_frame(frame: pd.DataFrame, quotes: dict[tuple[str, int], tuple[float, ...]],
                  stop_bps: int, target_r: float, horizon: int) -> pd.DataFrame:
    value = horizon_opportunities(frame, horizon)
    longs, shorts = [], []
    for record in value.to_dict("records"):
        longs.append(barrier_outcome(record, quotes, "LONG", stop_bps, target_r, horizon))
        shorts.append(barrier_outcome(record, quotes, "SHORT", stop_bps, target_r, horizon))
    value["long_net_r"], value["short_net_r"] = longs, shorts
    return value.dropna(subset=["long_net_r", "short_net_r"])


def model(name: str):
    if name == "ridge":
        return make_pipeline(StandardScaler(), Ridge(alpha=20))
    return HistGradientBoostingRegressor(
        learning_rate=.04, max_iter=160, max_leaf_nodes=7,
        min_samples_leaf=40, l2_regularization=8, random_state=42,
    )


def profit_factor_value(stats: dict[str, Any]) -> float:
    value = stats.get("profit_factor")
    if value is not None:
        return float(value)
    if stats.get("samples", stats.get("trades", 0)) and stats.get("wins") == stats.get("samples", stats.get("trades")) and stats.get("expectancy_r", 0) > 0:
        return math.inf
    return 0.0


def predict_two(training: pd.DataFrame, target: pd.DataFrame, name: str):
    x, future = training[list(FEATURES)].to_numpy(float), target[list(FEATURES)].to_numpy(float)
    long_model, short_model = model(name), model(name)
    long_model.fit(x, training["long_net_r"].to_numpy(float))
    short_model.fit(x, training["short_net_r"].to_numpy(float))
    return long_model.predict(future), short_model.predict(future), long_model, short_model


def portfolio(value: pd.DataFrame, long_prediction: np.ndarray, short_prediction: np.ndarray,
              threshold: float, horizon: int) -> list[dict[str, Any]]:
    rows = value.copy()
    rows["long_prediction"], rows["short_prediction"] = long_prediction, short_prediction
    rows["prediction"] = rows[["long_prediction", "short_prediction"]].max(axis=1)
    rows = rows[rows["prediction"] >= threshold]
    active, result = [], []
    for stamp, candidates in rows.groupby("minute_ts", sort=True):
        active = [item for item in active if item["exit_time"] > stamp]
        slots = max(0, 5 - len(active))
        for record in candidates.sort_values("prediction", ascending=False).to_dict("records"):
            if not slots or any(x["inst_id"] == record["inst_id"] for x in active):
                continue
            side = "LONG" if record["long_prediction"] >= record["short_prediction"] else "SHORT"
            item = {
                "inst_id": record["inst_id"], "side": side, "date": record["date"],
                "entry_time": int(stamp), "exit_time": int(stamp + horizon * 60),
                "net_r": float(record["long_net_r"] if side == "LONG" else record["short_net_r"]),
            }
            active.append(item); result.append(item); slots -= 1
    return result


def experiment_fingerprint(selected: dict[str, Any], development_days: list[str],
                           training_data_end: int, training_data_digest: str) -> str:
    payload = {
        "research_version": RESEARCH_VERSION,
        "features": list(FEATURES),
        "config": {key: selected[key] for key in ("stop_bps", "target_r", "horizon", "model", "threshold")},
        "development_days": development_days,
        "training_data_end": int(training_data_end),
        "training_data_digest": training_data_digest,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def training_digest(labeled: pd.DataFrame) -> str:
    columns = ["minute_ts", "inst_id", *FEATURES, "long_net_r", "short_net_r"]
    ordered = labeled[columns].sort_values(["minute_ts", "inst_id"]).reset_index(drop=True)
    hashes = pd.util.hash_pandas_object(ordered, index=False).to_numpy(dtype="uint64")
    return hashlib.sha256(hashes.tobytes()).hexdigest()


def artifact_compatible(artifact: dict[str, Any] | None) -> bool:
    if not artifact or artifact.get("research_version") != RESEARCH_VERSION:
        return False
    if tuple(artifact.get("features") or ()) != FEATURES:
        return False
    try:
        expected = "micro_barrier_" + experiment_fingerprint(
            artifact["config"], list(artifact["development_days"]), int(artifact["training_data_end"]),
            str(artifact["training_data_digest"]),
        )
        started = int(datetime.fromisoformat(artifact["experiment_started_at"].replace("Z", "+00:00")).timestamp())
        return artifact.get("experiment_id") == expected and started > int(artifact["training_data_end"])
    except (KeyError, TypeError, ValueError):
        return False


def try_freeze(rows: list[sqlite3.Row], frame: pd.DataFrame, cohort_index: int = 0,
               prior_attempts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    observed_days = sorted(frame["date"].unique()) if not frame.empty and "date" in frame else []
    day_diagnostics = cash_day_diagnostics(frame)
    days = qualified_cash_days(frame)
    prior_attempts = list(prior_attempts or [])
    cohort_start, cohort_end = cohort_index * 10, (cohort_index + 1) * 10
    cohort_days = days[cohort_start:cohort_end]
    base = {"research_version": RESEARCH_VERSION,
            "research_phase": "collect_cash_development", "cash_development_days": len(days),
            "freeze_after_cash_days": cohort_end, "development_cohort": cohort_index + 1,
            "development_days": cohort_days, "selection_attempts": prior_attempts,
            "partial_cash_days_excluded": [day for day in observed_days if day not in days],
            "cash_day_diagnostics": day_diagnostics,
            "cash_day_requirements": {
                "valid_minutes": MIN_QUALIFIED_MINUTES,
                "span_minutes": MIN_QUALIFIED_SPAN_MINUTES,
                "dense_symbols": MIN_QUALIFIED_SYMBOLS,
                "minutes_per_symbol": MIN_MINUTES_PER_SYMBOL,
            }}
    if len(days) < cohort_end:
        return base
    development_days = cohort_days
    source = frame[frame["date"].isin(development_days)]
    quotes = quote_lookup(rows)
    evaluations = []
    for stop_bps in DEV_STOP_BPS:
        for target_r in DEV_TARGET_R:
            for horizon in DEV_HORIZONS:
                labeled = labeled_frame(source, quotes, stop_bps, target_r, horizon)
                opportunity_count = len(horizon_opportunities(source, horizon))
                path_coverage = len(labeled) / opportunity_count if opportunity_count else 0.0
                for name in DEV_MODELS:
                    blocks = []
                    for training_days, target_days in ((development_days[:4], development_days[4:6]),
                                                       (development_days[:6], development_days[6:10])):
                        training = labeled[labeled["date"].isin(training_days)]
                        target = labeled[labeled["date"].isin(target_days)]
                        if len(training) < 300 or target.empty:
                            blocks = []; break
                        lp, sp, _, _ = predict_two(training, target, name)
                        blocks.append((target, lp, sp))
                    if len(blocks) != 2:
                        continue
                    for threshold in DEV_THRESHOLDS:
                        trades = [portfolio(*block, threshold, horizon) for block in blocks]
                        stats = [metric(part) for part in trades]
                        evaluations.append({"stop_bps": stop_bps, "target_r": target_r, "horizon": horizon,
                                            "model": name, "threshold": threshold,
                                            "path_coverage": round(path_coverage, 4),
                                            "oos_1": stats[0], "oos_2": stats[1]})
    eligible = [x for x in evaluations if x["oos_1"]["trades"] >= 30 and x["oos_2"]["trades"] >= 60
                and x["path_coverage"] >= .95
                and all(profit_factor_value(x[key]) > 1.1 and x[key]["expectancy_r"] > .05
                        for key in ("oos_1", "oos_2"))]
    eligible.sort(key=lambda x: min(profit_factor_value(x[k]) for k in ("oos_1", "oos_2")), reverse=True)
    if not eligible:
        best = max(evaluations, key=lambda x:min(profit_factor_value(x[k]) for k in ("oos_1","oos_2")), default=None)
        attempt = {"cohort": cohort_index + 1, "development_days": development_days,
                   "tested": len(evaluations), "eligible": 0, "best": best,
                   "completed_at": datetime.now(UTC).isoformat()}
        return {**base, "research_phase": "collect_next_development_cohort",
                "selection_attempted": True, "tested": len(evaluations), "best": best,
                "selection_attempts": [*prior_attempts, attempt],
                "next_attempt_after_cash_days": cohort_end + 10}
    selected = eligible[0]
    labeled = labeled_frame(source, quotes, selected["stop_bps"], selected["target_r"], selected["horizon"])
    lp, sp, long_model, short_model = predict_two(labeled, labeled.iloc[:1], selected["model"])
    training_data_end = int(source["minute_ts"].max())
    data_digest = training_digest(labeled)
    experiment_id = "micro_barrier_" + experiment_fingerprint(
        selected, development_days, training_data_end, data_digest,
    )
    started_at = datetime.now(UTC).isoformat()
    joblib.dump({"experiment_id": experiment_id, "experiment_started_at": started_at,
                 "research_version": RESEARCH_VERSION, "config": selected, "features": FEATURES,
                 "development_cohort": cohort_index + 1, "development_days": development_days,
                 "training_data_end": training_data_end, "training_data_digest": data_digest,
                 "long_model": long_model, "short_model": short_model}, ARTIFACT_PATH)
    return {**base, "research_phase": "frozen_forward", "selection_attempted": True,
            "tested": len(evaluations), "selected": selected,
            "selection_attempts": prior_attempts,
            "experiment_id": experiment_id, "experiment_started_at": started_at}


def signal_stats(experiment_id: str | None) -> dict[str, Any]:
    if not experiment_id:
        return {"samples": 0, "opportunities": 0, "trading_days": 0, "profit_factor": None,
                "expectancy_r": 0, "max_drawdown_r": 0, "positive_trading_days": 0,
                "best_day_profit_share": 0, "total_completed": 0,
                "data_gap_samples": 0, "data_gap_rate": 0}
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        completed = conn.execute("SELECT * FROM okx_micro_model_signals WHERE experiment_id=? AND labeled_at IS NOT NULL",
                                 (experiment_id,)).fetchall()
    rows = [row for row in completed if row["net_r"] is not None]
    stats = metric([{"net_r":float(row["net_r"]),"exit_time":int(row["exit_minute"])} for row in rows])
    stats["samples"] = stats.pop("trades")
    stats["opportunities"] = len({int(row["entry_minute"]) for row in rows})
    day_values: dict[str, float] = {}
    for row in rows:
        day = datetime.fromtimestamp(int(row["entry_minute"]), UTC).astimezone(NY).date().isoformat()
        day_values[day] = day_values.get(day, 0.0) + float(row["net_r"])
    stats["trading_days"] = len(day_values)
    stats["positive_trading_days"] = sum(value > 0 for value in day_values.values())
    positive_day_profit = sum(max(0.0, value) for value in day_values.values())
    stats["best_day_profit_share"] = (
        max((max(0.0, value) for value in day_values.values()), default=0.0) / positive_day_profit
        if positive_day_profit else 0.0
    )
    gaps = sum(row["exit_reason"] == "data_gap" for row in completed)
    stats["total_completed"] = len(completed)
    stats["data_gap_samples"] = gaps
    stats["data_gap_rate"] = gaps / len(completed) if completed else 0
    return stats


def pending_lookback_start(experiment_id: str, default_since: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("""SELECT MIN(entry_minute) FROM okx_micro_model_signals
                              WHERE experiment_id=? AND labeled_at IS NULL""",
                           (experiment_id,)).fetchone()
    return min(int(default_since), int(row[0])) if row and row[0] is not None else int(default_since)


def forward_gate_checks(stats: dict[str, Any]) -> dict[str, bool]:
    return {
        "samples_at_least_100": stats["samples"] >= 100,
        "profit_factor_above_1_2": profit_factor_value(stats) > 1.2,
        "expectancy_above_0_1r": stats["expectancy_r"] > .1,
        "at_least_20_opportunities": stats["opportunities"] >= 20,
        "at_least_10_trading_days": stats["trading_days"] >= 10,
        "at_least_6_positive_days": stats["positive_trading_days"] >= 6,
        "best_day_profit_share_at_most_50pct": stats["best_day_profit_share"] <= .5,
        "max_drawdown_below_12r": stats.get("max_drawdown_r", math.inf) < 12,
        "data_gap_rate_at_most_5pct": stats["data_gap_rate"] <= .05,
    }


def label_pending(artifact: dict[str, Any], quotes: dict[tuple[str, int], tuple[float, ...]]) -> int:
    experiment_id = artifact["experiment_id"]
    now_minute = int(time.time() // 60 * 60)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        pending = conn.execute(
            "SELECT * FROM okx_micro_model_signals WHERE experiment_id=? AND labeled_at IS NULL ORDER BY entry_minute",
            (experiment_id,),
        ).fetchall()
    completed = 0
    for row in pending:
        direction = 1 if row["side"] == "LONG" else -1
        end = min(now_minute, int(row["due_minute"]))
        exit_minute = exit_price = reason = None
        missing = False
        for stamp in range(int(row["entry_minute"]), end, 60):
            quote = quotes.get((row["inst_id"], stamp))
            if not quote or quote[0] <= 0 or quote[1] <= quote[0]:
                missing = True; break
            _bid, _ask, min_bid, max_bid, min_ask, max_ask = quote_path(quote)
            stop_touched = (min_bid <= float(row["stop_price"]) if direction > 0
                            else max_ask >= float(row["stop_price"]))
            target_touched = (max_bid >= float(row["target_price"]) if direction > 0
                              else min_ask <= float(row["target_price"]))
            if stop_touched:
                exit_minute, exit_price, reason = stamp + 60, float(row["stop_price"]), "stop"; break
            if target_touched:
                exit_minute, exit_price, reason = stamp + 60, float(row["target_price"]), "target"; break
        if missing and now_minute >= int(row["due_minute"]):
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("UPDATE okx_micro_model_signals SET exit_reason='data_gap',labeled_at=? WHERE signal_key=?",
                             (datetime.now(UTC).isoformat(), row["signal_key"]))
            completed += 1; continue
        if exit_minute is None and now_minute >= int(row["due_minute"]):
            quote = quotes.get((row["inst_id"], int(row["due_minute"]) - 60))
            if not quote:
                continue
            bid, ask, *_ = quote_path(quote)
            exit_minute, exit_price, reason = int(row["due_minute"]), bid if direction > 0 else ask, "time"
        if exit_minute is None:
            continue
        entry, risk = float(row["entry_price"]), float(row["risk_price"])
        cost_bps = float(row["cost_bps"] or 14.0)
        net_r = (float(exit_price) - entry) * direction / risk - (cost_bps / 10_000) / (risk / entry)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""UPDATE okx_micro_model_signals SET exit_minute=?,exit_price=?,net_r=?,
                            exit_reason=?,labeled_at=? WHERE signal_key=?""",
                         (exit_minute, exit_price, net_r, reason, datetime.now(UTC).isoformat(), row["signal_key"]))
        completed += 1
    return completed


def generate_forward(artifact: dict[str, Any], frame: pd.DataFrame,
                     live_quote_provider=None, decision_ts: int | None = None,
                     max_feature_age_seconds: int | None = None) -> int:
    if frame.empty:
        return 0
    config, experiment_id = artifact["config"], artifact["experiment_id"]
    start = int(datetime.fromisoformat(artifact["experiment_started_at"].replace("Z", "+00:00")).timestamp())
    with sqlite3.connect(DB_PATH) as conn:
        latest = conn.execute("SELECT MAX(entry_minute) FROM okx_micro_model_signals WHERE experiment_id=?",
                              (experiment_id,)).fetchone()[0]
    after = max(start, int(latest or 0))
    opportunities = horizon_opportunities(frame, int(config["horizon"]))
    opportunities = opportunities[opportunities["minute_ts"] > after]
    if decision_ts is not None and max_feature_age_seconds is not None:
        allowed_age = min(int(max_feature_age_seconds), MAX_FORWARD_FEATURE_AGE_SECONDS)
        opportunities = opportunities[
            (opportunities["minute_ts"] <= decision_ts)
            & (opportunities["minute_ts"] >= decision_ts - allowed_age)
        ]
    inserted = 0
    for stamp, group in opportunities.groupby("minute_ts", sort=True):
        liquid = group[(group["spread_bps"] <= 5) & (group["bid_depth_usd"] >= 5_000)
                       & (group["ask_depth_usd"] >= 5_000)].copy()
        if liquid.empty:
            continue
        values = liquid[list(FEATURES)].to_numpy(float)
        liquid["long_prediction"] = artifact["long_model"].predict(values)
        liquid["short_prediction"] = artifact["short_model"].predict(values)
        liquid["prediction"] = liquid[["long_prediction", "short_prediction"]].max(axis=1)
        liquid = liquid[liquid["prediction"] >= float(config["threshold"])].sort_values("prediction", ascending=False)
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            active = conn.execute("""SELECT inst_id FROM okx_micro_model_signals
                                     WHERE experiment_id=? AND entry_minute<=? AND due_minute>?""",
                                  (experiment_id, int(stamp), int(stamp))).fetchall()
        active_symbols = {row["inst_id"] for row in active}
        slots = max(0, 5 - len(active))
        for record in liquid.to_dict("records"):
            if not slots or record["inst_id"] in active_symbols:
                continue
            side = "LONG" if record["long_prediction"] >= record["short_prediction"] else "SHORT"
            direction = 1 if side == "LONG" else -1
            feature_minute = int(stamp)
            entry_minute = int(decision_ts if decision_ts is not None else stamp)
            # Development labels model a feature minute closing into the next
            # minute's entry.  A delayed live decision must not silently turn
            # that into an after-hours holding window.
            if not entry_horizon_complete(entry_minute, int(config["horizon"])):
                continue
            if live_quote_provider is not None:
                try:
                    live_bid, live_ask = live_quote_provider(record["inst_id"])
                    live_bid, live_ask = float(live_bid), float(live_ask)
                except Exception:
                    continue
                mid = (live_bid + live_ask) / 2
                if live_bid <= 0 or live_ask <= live_bid or not mid or (live_ask - live_bid) / mid * 10_000 > 5:
                    continue
                entry, quote_source = (live_ask if direction > 0 else live_bid), "okx_rest_ticker"
            else:
                entry, quote_source = float(record["ask_px"] if direction > 0 else record["bid_px"]), "feature_quote"
            risk = entry * float(config["stop_bps"]) / 10_000
            stop = entry - direction * risk
            target = entry + direction * float(config["target_r"]) * risk
            due = entry_minute + int(config["horizon"]) * 60
            cost_bps = 2 * float(record["taker_fee_bps"]) + IMPACT_BUFFER_BPS
            key = f"{experiment_id}:{record['inst_id']}:{side}:{feature_minute}"
            with sqlite3.connect(DB_PATH) as conn:
                before = conn.total_changes
                conn.execute("""INSERT OR IGNORE INTO okx_micro_model_signals
                    (signal_key,experiment_id,inst_id,side,entry_minute,due_minute,entry_price,
                     stop_price,target_price,risk_price,predicted_r,created_at,
                     feature_minute,signal_generated_at,entry_quote_source,cost_bps)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (key,experiment_id,record["inst_id"],side,entry_minute,due,entry,stop,target,risk,
                     float(record["prediction"]),datetime.now(UTC).isoformat(),feature_minute,
                     datetime.now(UTC).isoformat(),quote_source,cost_bps))
                inserted += conn.total_changes - before
            active_symbols.add(record["inst_id"]); slots -= 1
    return inserted


class Runner:
    def __init__(self) -> None:
        ensure_schema(); self.running = True
        from scripts.okx_intraday_agent import OKX, settings
        self.market_client = OKX(settings())
        self.last_development_scan = 0.0

    def live_quote(self, inst_id: str) -> tuple[float, float]:
        ticker = self.market_client.ticker(inst_id)
        return float(ticker.get("bidPx") or 0), float(ticker.get("askPx") or 0)

    def stop(self) -> None:
        self.running = False

    def once(self) -> dict[str, Any]:
        artifact = None
        try: artifact = joblib.load(ARTIFACT_PATH)
        except Exception: pass
        if not artifact_compatible(artifact):
            artifact = None
        now = time.time()
        if not artifact:
            previous = {}
            try: previous = json.loads(STATE_PATH.read_text())
            except Exception: pass
            if self.last_development_scan and now - self.last_development_scan < 600 and previous:
                state = {**previous, "updated_at": datetime.now(UTC).isoformat(),
                         "development_scan_deferred": True,
                         "next_development_scan_at": datetime.fromtimestamp(
                             self.last_development_scan + 600, UTC
                         ).isoformat()}
                STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2))
                return state
            rows = _rows(); frame = feature_frame(rows)
            self.last_development_scan = now
            attempts = (previous.get("selection_attempts") or []) if previous.get("research_version") == RESEARCH_VERSION else []
            if warmup_demo_enabled():
                artifact = warmup_artifact()
                joblib.dump(artifact, ARTIFACT_PATH)
                inserted = generate_forward(
                    artifact, frame, live_quote_provider=self.live_quote,
                    decision_ts=int(time.time() // 60 * 60),
                    max_feature_age_seconds=MAX_FORWARD_FEATURE_AGE_SECONDS,
                )
                state = {"research_version": RESEARCH_VERSION, "research_phase": "v13_warmup_demo",
                         "experiment_id": artifact["experiment_id"], "experiment_started_at": artifact["experiment_started_at"],
                         "selected": artifact["config"], "cash_development_days": 0,
                         "warmup_demo": True, "passed": False, "execution_ready": False,
                         "signals_inserted_this_run": inserted,
                         "warning": "固定方向评分模拟盘试运行；不计入严格前向验收"}
            else:
                state = try_freeze(rows, frame, cohort_index=len(attempts), prior_attempts=attempts)
        else:
            lookback_minutes = int(artifact["config"]["horizon"]) + 5
            default_since = int(now // 60 * 60) - lookback_minutes * 60
            since_ts = pending_lookback_start(artifact["experiment_id"], default_since)
            rows = _rows(since_ts)
            frame = feature_frame(rows)
            decision_ts = int(time.time() // 60 * 60)
            inserted = generate_forward(
                artifact, frame, live_quote_provider=self.live_quote,
                decision_ts=decision_ts, max_feature_age_seconds=MAX_FORWARD_FEATURE_AGE_SECONDS,
            )
            completed = label_pending(artifact, quote_lookup(rows))
            state = {"research_phase": "v13_warmup_demo" if artifact.get("warmup_demo") else "frozen_forward", "experiment_id": artifact["experiment_id"],
                     "experiment_started_at": artifact["experiment_started_at"], "selected": artifact["config"],
                     "research_version": artifact.get("research_version"),
                     "development_cohort": artifact.get("development_cohort"),
                     "development_days": artifact.get("development_days"),
                     "training_data_end": artifact.get("training_data_end"),
                     "training_data_digest": artifact.get("training_data_digest"),
                     "cash_development_days": len(artifact.get("development_days") or []),
                     "freeze_after_cash_days": int(artifact.get("development_cohort") or 1) * 10,
                     "data_lookback_minutes": lookback_minutes,
                     "data_lookback_since": since_ts,
                     "signals_inserted_this_run": inserted, "signals_labeled_this_run": completed}
            if artifact.get("warmup_demo"):
                state.update(warmup_demo=True, warning="固定方向评分模拟盘试运行；不计入严格前向验收")
        stats = signal_stats(state.get("experiment_id"))
        checks = forward_gate_checks(stats)
        state.update({"updated_at": datetime.now(UTC).isoformat(), "forward": stats,
                      "gate_checks": checks, "passed": all(checks.values()), "execution_ready": False})
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        return state

    def run(self) -> None:
        while self.running:
            try: self.once()
            except Exception: LOG.exception("micro model lifecycle failed")
            # Run just after the collector flushes the completed minute rather
            # than drifting at an arbitrary second within every minute.
            delay = max(1.0, 62.0 - (time.time() % 60.0))
            for _ in range(int(math.ceil(delay))):
                if not self.running: break
                time.sleep(min(1.0, delay))
                delay -= 1.0


def main() -> None:
    runner = Runner()
    signal.signal(signal.SIGTERM, lambda *_: runner.stop())
    signal.signal(signal.SIGINT, lambda *_: runner.stop())
    runner.run()


if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")
    main()
