#!/usr/bin/env python3
"""Run the selected return model forward without sending any order."""
from __future__ import annotations

import json
import hashlib
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_candidate_ws import microstructure_confirmation, record_shadow_signal  # noqa: E402
from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import DEFAULT_SYMBOLS  # noqa: E402
from scripts.okx_return_model_research import dataset  # noqa: E402

UTC = timezone.utc
MODEL_PATH = ROOT / "data" / "okx_return_shadow_model.joblib"
STATE_PATH = ROOT / "data" / "okx_return_shadow_state.json"
LOG = logging.getLogger("okx-return-shadow")


def score_rows(rows: pd.DataFrame, artifact: dict[str, Any]) -> list[dict[str, Any]]:
    """Score one fixed opportunity per instrument with the sealed feature order."""
    if rows.empty:
        return []
    latest_entry = int(rows["entry_time"].max())
    current = rows[rows["entry_time"] == latest_entry].copy()
    features = list(artifact["features"])
    values = current.reindex(columns=features, fill_value=0).astype(float).to_numpy()
    current["prediction_bps"] = artifact["model"].predict(values)
    return current.sort_values("prediction_bps", key=abs, ascending=False).to_dict("records")


class ShadowRunner:
    def __init__(self) -> None:
        self.client = OKX(settings())
        self.running = True
        self.last_entry_time = 0

    def stop(self) -> None:
        self.running = False

    def evaluate_once(self) -> dict[str, Any]:
        artifact = joblib.load(MODEL_PATH)
        experiment_id = (
            f"return_{artifact['model_name']}_h{artifact['horizon']}_t{artifact['threshold']}_"
            f"{hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest()[:12]}"
        )
        raw: dict[str, list[list[str]]] = {}
        errors: dict[str, str] = {}
        for symbol in DEFAULT_SYMBOLS:
            try:
                raw[symbol] = self.client.candles(symbol, limit=300, bar="1m")
            except Exception as exc:
                errors[symbol] = str(exc)
        if errors or len(raw) != len(DEFAULT_SYMBOLS):
            raise RuntimeError(f"incomplete market snapshot: {errors}")
        predictions = score_rows(dataset(raw, require_target=False), artifact)
        now_ms = int(time.time() * 1000)
        entry_time = int(predictions[0]["entry_time"]) if predictions else 0
        # A restarted process must never backfill a signal it could not have acted on.
        fresh = bool(entry_time and -60_000 <= now_ms - entry_time <= 10 * 60_000)
        threshold = float(artifact["threshold"])
        candidates = []
        for item in predictions:
            prediction = float(item["prediction_bps"])
            symbol = str(item["symbol"])
            ticker = self.client.ticker(symbol)
            price = float(ticker.get("last") or item.get("entry_price") or 0)
            bid, ask = float(ticker.get("bidPx") or 0), float(ticker.get("askPx") or 0)
            spread_bps = (ask - bid) / ((ask + bid) / 2) * 10_000 if bid > 0 and ask > bid else 0.0
            side = "LONG" if prediction > 0 else "SHORT"
            row = {
                "instId": symbol, "side": side, "atr14": price / 120,
                "score": abs(prediction), "volume_ratio": float(item.get("volume_ratio") or 0),
                "spread_bps": spread_bps,
                # Together with 8 bps fees and observed spread, labels retain the
                # same conservative 14 bps round-trip cost used by research.
                "estimated_slippage_bps": max(0.0, 6.0 - spread_bps),
                "microstructure": microstructure_confirmation(symbol, side),
                "experiment_id": experiment_id,
            }
            passed = abs(prediction) >= threshold
            if fresh and entry_time > self.last_entry_time:
                record_shadow_signal(row, entry_time, price, "RETURN_MODEL_ALL")
                if passed:
                    record_shadow_signal(row, entry_time, price, "RETURN_MODEL_PASS")
                    if row["microstructure"].get("aligned"):
                        record_shadow_signal(row, entry_time, price, "RETURN_MODEL_MICRO_PASS")
                    side_depth = float(row["microstructure"].get(
                        "ask_depth" if side == "LONG" else "bid_depth"
                    ) or 0)
                    executable = (
                        row["microstructure"].get("aligned")
                        and spread_bps <= 5.0
                        and side_depth * price >= 5_000
                    )
                    if executable:
                        record_shadow_signal(row, entry_time, price, "RETURN_MODEL_EXECUTABLE_PASS")
            candidates.append({
                "inst_id": symbol, "side": side, "prediction_bps": round(prediction, 3),
                "threshold_passed": passed, "price": price, "spread_bps": round(spread_bps, 3),
            })
        if fresh and predictions:
            self.last_entry_time = max(self.last_entry_time, entry_time)
        state = {
            "updated_at": datetime.now(UTC).isoformat(), "mode": "shadow_only",
            "model": artifact["model_name"], "horizon_minutes": int(artifact["horizon"]) * 5,
            "threshold_bps": threshold, "entry_time": entry_time, "fresh": fresh,
            "experiment_id": experiment_id,
            "experiment_started_at": artifact["created_at"],
            "signals": sum(row["threshold_passed"] for row in candidates),
            "candidates": candidates[:10],
        }
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        return state

    def run(self) -> None:
        while self.running:
            try:
                state = self.evaluate_once()
                LOG.info("shadow opportunity=%s signals=%s fresh=%s", state["entry_time"], state["signals"], state["fresh"])
            except Exception as exc:
                LOG.exception("return shadow evaluation failed")
                STATE_PATH.write_text(json.dumps({
                    "updated_at": datetime.now(UTC).isoformat(), "mode": "shadow_only", "error": str(exc),
                }, ensure_ascii=False, indent=2))
            for _ in range(60):
                if not self.running:
                    break
                time.sleep(1)


def main() -> None:
    runner = ShadowRunner()
    signal.signal(signal.SIGTERM, lambda *_: runner.stop())
    signal.signal(signal.SIGINT, lambda *_: runner.stop())
    runner.run()


if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")
    main()
