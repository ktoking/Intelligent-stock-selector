#!/usr/bin/env python3
"""Label confirmed shadow signals after 60 minutes without placing orders."""
from __future__ import annotations

import json
import logging
import math
import signal
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_intraday_agent import DB_PATH, OKX, settings  # noqa: E402

UTC = timezone.utc
STATE_PATH = ROOT / "data" / "okx_shadow_learning.json"
LOG = logging.getLogger("okx-shadow-labeler")
V5_STOP_AWARE_STAGES = {
    "GAP_FADE_V5_FORWARD",
    "GAP_FADE_V5_STRICT_CONFIRM_FORWARD",
}


def _stats(rows: list[sqlite3.Row]) -> dict[str, Any]:
    raw_values = [float(row["net_r"]) for row in rows]
    values = [value for value in raw_values if math.isfinite(value)]
    positive, negative = sum(v for v in values if v > 0), -sum(v for v in values if v < 0)
    return {
        "samples": len(values), "win_rate": round(sum(v > 0 for v in values) / len(values) * 100, 2) if values else 0,
        "expectancy_r": round(sum(values) / len(values), 4) if values else 0,
        "profit_factor": round(positive / negative, 3) if negative else None,
        "invalid_samples": len(raw_values) - len(values),
    }


def _experiment_stats(rows: list[sqlite3.Row]) -> dict[str, Any]:
    result = _stats(rows)
    result["opportunities"] = len({int(row["entry_ts"]) for row in rows})
    result["trading_days"] = len({
        datetime.fromtimestamp(int(row["entry_ts"]) / 1000, UTC).date() for row in rows
    })
    result["long"] = _stats([row for row in rows if row["side"] == "LONG"])
    result["short"] = _stats([row for row in rows if row["side"] == "SHORT"])
    return result


def _capped_normalized_weights(raw: list[float], target: float, cap: float = 0.20) -> list[float]:
    weights = [0.0] * len(raw)
    active = {index for index, value in enumerate(raw) if value > 0}
    target = min(max(0.0, target), len(raw) * cap)
    while active and target - sum(weights) > 1e-12:
        remaining = target - sum(weights)
        total = sum(raw[index] for index in active)
        if total <= 0:
            break
        capped = []
        for index in active:
            proposed = remaining * raw[index] / total
            room = cap - weights[index]
            if proposed >= room - 1e-12:
                weights[index] += max(0.0, room)
                capped.append(index)
        if not capped:
            for index in active:
                weights[index] += remaining * raw[index] / total
            break
        active.difference_update(capped)
    return weights


def _portfolio_path_stats(daily_returns: list[float]) -> dict[str, Any]:
    equity = peak = 1.0
    max_drawdown = 0.0
    positive = negative = 0.0
    for value in daily_returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak * 100.0)
        if value > 0:
            positive += value
        elif value < 0:
            negative -= value
    return {
        "return_pct": round((equity - 1.0) * 100.0, 4),
        "max_drawdown_pct": round(max_drawdown, 4),
        "daily_profit_factor": round(positive / negative, 3) if negative else None,
        "positive_days": sum(value > 0 for value in daily_returns),
    }


def _v5_symbol_edge_sizing_stats(rows: list[sqlite3.Row]) -> dict[str, Any]:
    """Compare baseline and weighted allocation with identical daily gross."""
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        day_name = datetime.fromtimestamp(int(row["entry_ts"]) / 1000, UTC).date().isoformat()
        grouped.setdefault(day_name, []).append(row)
    baseline_daily: list[float] = []
    weighted_daily: list[float] = []
    missing_scores = 0
    max_gross_difference = 0.0
    for day_name in sorted(grouped):
        current = grouped[day_name]
        desired = [
            min(0.20, 0.0035 / max(float(row["stop_bps"] or 0) / 10_000.0, 1e-9))
            for row in current
        ]
        scale = min(1.0, 0.80 / sum(desired)) if sum(desired) else 0.0
        baseline = [value * scale for value in desired]
        multipliers = []
        for row in current:
            value = row["allocation_multiplier"]
            if value is None:
                missing_scores += 1
                value = 1.0
            multipliers.append(max(0.5, min(1.5, float(value))))
        weighted = _capped_normalized_weights(
            [weight * multiplier for weight, multiplier in zip(baseline, multipliers)],
            sum(baseline),
        )
        max_gross_difference = max(max_gross_difference, abs(sum(baseline) - sum(weighted)))
        net_pct = [float(row["net_r"]) * float(row["stop_bps"] or 0) / 100.0 for row in current]
        baseline_daily.append(sum(weight * value / 100.0 for weight, value in zip(baseline, net_pct)))
        weighted_daily.append(sum(weight * value / 100.0 for weight, value in zip(weighted, net_pct)))
    baseline_stats = _portfolio_path_stats(baseline_daily)
    weighted_stats = _portfolio_path_stats(weighted_daily)
    return {
        "samples": len(rows),
        "trading_days": len(grouped),
        "missing_symbol_scores": missing_scores,
        "max_daily_gross_difference_pct_points": round(max_gross_difference * 100.0, 10),
        "baseline": baseline_stats,
        "symbol_edge_weighted": weighted_stats,
        "incremental_return_pct_points": round(
            weighted_stats["return_pct"] - baseline_stats["return_pct"], 4,
        ),
    }


def _profit_factor_value(stats: dict[str, Any]) -> float:
    if stats.get("profit_factor") is not None:
        return float(stats["profit_factor"])
    if stats.get("samples") and stats.get("win_rate") == 100 and stats.get("expectancy_r", 0) > 0:
        return math.inf
    return 0.0


def _v5_forward_gate_checks(stats: dict[str, Any], invalid_prestart: int) -> dict[str, bool]:
    """Shared evidence floor for independent, non-promotable V5 shadow lanes."""
    return {
        "samples_at_least_25": stats["samples"] >= 25,
        "profit_factor_above_1_2": _profit_factor_value(stats) > 1.2,
        "expectancy_above_zero": stats["expectancy_r"] > 0,
        "at_least_5_trading_days": stats["trading_days"] >= 5,
        "no_invalid_outcomes": stats.get("invalid_samples", 0) == 0,
        "no_prestart_samples": invalid_prestart == 0,
    }


class Labeler:
    def __init__(self) -> None:
        self.client = OKX(settings())
        self.running = True

    def stop(self) -> None:
        self.running = False

    @staticmethod
    def outcome(row: sqlite3.Row, candles: list[list[str]]) -> dict[str, float] | None:
        future = sorted(
            [item for item in candles if len(item) >= 9 and item[8] == "1"
             and int(row["entry_ts"]) < int(item[0]) <= int(row["due_ts"])],
            key=lambda item: int(item[0]),
        )
        expected = max(5, int((int(row["due_ts"]) - int(row["entry_ts"])) / 60_000))
        if len(future) < math.ceil(expected * 0.75):
            return None
        entry = float(row["entry_price"])
        direction = 1 if row["side"] == "LONG" else -1
        try:
            stage = str(row["stage"] or "")
        except (KeyError, IndexError):
            stage = ""
        try:
            stop_bps = float(row["stop_bps"] or 0)
        except (KeyError, IndexError):
            stop_bps = 0.0
        v5_stop_aware = stage in V5_STOP_AWARE_STAGES and stop_bps > 0
        risk = entry * stop_bps / 10_000 if v5_stop_aware else max(float(row["atr14"] or 0) * 1.2, entry * 0.002)
        evaluated = future
        exit_reason = "horizon"
        exit_price = float(future[-1][4])
        if v5_stop_aware:
            for index, item in enumerate(future):
                stopped = float(item[3]) <= entry - risk if direction > 0 else float(item[2]) >= entry + risk
                if stopped:
                    evaluated = future[:index + 1]
                    exit_price = entry - risk if direction > 0 else entry + risk
                    exit_reason = "atr_stop"
                    break
        favorable = max((float(item[2]) - entry) * direction for item in future)
        adverse = min((float(item[3]) - entry) * direction for item in evaluated)
        if direction < 0:
            favorable = max((entry - float(item[3])) for item in evaluated)
            adverse = min((entry - float(item[2])) for item in evaluated)
        else:
            favorable = max((float(item[2]) - entry) for item in evaluated)
        gross_r = (exit_price - entry) * direction / risk
        # 8 bps round-trip fee allowance plus observed entry spread and estimated impact.
        cost_bps = 8 + float(row["spread_bps"] or 0) + float(row["slippage_bps"] or 0)
        return {"exit_price": exit_price, "gross_r": gross_r,
                "net_r": gross_r - cost_bps / (risk / entry * 10_000),
                "mfe_r": favorable / risk, "mae_r": adverse / risk,
                "exit_reason": exit_reason}

    def label_once(self) -> int:
        now_ms = int(time.time() * 1000)
        with closing(sqlite3.connect(DB_PATH)) as conn, conn:
            conn.row_factory = sqlite3.Row
            try:
                pending = conn.execute(
                    "SELECT * FROM okx_signal_shadow WHERE labeled_at IS NULL AND due_ts<=? ORDER BY due_ts LIMIT 25",
                    (now_ms,),
                ).fetchall()
            except sqlite3.OperationalError:
                return 0
        completed = 0
        for row in pending:
            try:
                horizon = max(60, int((int(row["due_ts"]) - int(row["entry_ts"])) / 60_000))
                candles = self.client.candles_ending_at(
                    row["inst_id"], int(row["due_ts"]), limit=min(300, horizon + 40), bar="1m"
                )
                outcome = self.outcome(row, candles)
                if not outcome:
                    continue
                with closing(sqlite3.connect(DB_PATH)) as conn, conn:
                    conn.execute("""
                        UPDATE okx_signal_shadow SET exit_price=:exit_price,gross_r=:gross_r,net_r=:net_r,
                        mfe_r=:mfe_r,mae_r=:mae_r,exit_reason=:exit_reason,
                        labeled_at=:labeled_at WHERE signal_key=:signal_key
                    """, {**outcome, "labeled_at": datetime.now(UTC).isoformat(), "signal_key": row["signal_key"]})
                completed += 1
            except Exception:
                LOG.exception("shadow label failed for %s", row["signal_key"])
        self.write_state()
        return completed

    def write_state(self) -> None:
        with closing(sqlite3.connect(DB_PATH)) as conn, conn:
            conn.row_factory = sqlite3.Row
            try:
                all_rows = conn.execute("SELECT * FROM okx_signal_shadow WHERE labeled_at IS NOT NULL").fetchall()
                pending = conn.execute("SELECT COUNT(*) FROM okx_signal_shadow WHERE labeled_at IS NULL").fetchone()[0]
            except sqlite3.OperationalError:
                all_rows, pending = [], 0
        pass_rows = [row for row in all_rows if row["stage"] == "ONE_MIN_PASS"]
        aligned = [row for row in pass_rows if row["micro_available"] and
                   ((row["side"] == "LONG" and row["book_imbalance"] > 0 and row["aggressive_imbalance"] > 0)
                    or (row["side"] == "SHORT" and row["book_imbalance"] < 0 and row["aggressive_imbalance"] < 0))]
        unaligned = [row for row in pass_rows if row["micro_available"] and row not in aligned]
        all_stats, aligned_stats, unaligned_stats = _stats(all_rows), _stats(aligned), _stats(unaligned)
        stage_stats = {stage: _stats([row for row in all_rows if row["stage"] == stage])
                       for stage in sorted({row["stage"] for row in all_rows})}
        rule_passed = (aligned_stats["samples"] >= 100
                  and _profit_factor_value(aligned_stats) > 1.2
                  and aligned_stats["expectancy_r"] > 0.1)
        try:
            return_state = json.loads((ROOT / "data" / "okx_return_shadow_state.json").read_text())
            current_experiment = return_state.get("experiment_id")
            experiment_start_ms = int(datetime.fromisoformat(
                return_state["experiment_started_at"].replace("Z", "+00:00")
            ).timestamp() * 1000)
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            current_experiment = None
            experiment_start_ms = 0
        return_rows = [row for row in all_rows if row["stage"] == "RETURN_MODEL_PASS"
                       and current_experiment and row["experiment_id"] == current_experiment
                       and int(row["entry_ts"]) >= experiment_start_ms]
        invalid_prestart = sum(bool(
            row["stage"] == "RETURN_MODEL_PASS" and current_experiment
            and row["experiment_id"] == current_experiment
            and int(row["entry_ts"]) < experiment_start_ms
        ) for row in all_rows)
        return_stats = _experiment_stats(return_rows)
        return_gate_checks = {
            "samples_at_least_100": return_stats["samples"] >= 100,
            "profit_factor_above_1_2": _profit_factor_value(return_stats) > 1.2,
            "expectancy_above_0_1r": return_stats["expectancy_r"] > 0.1,
            "at_least_20_opportunities": return_stats["opportunities"] >= 20,
            "at_least_10_trading_days": return_stats["trading_days"] >= 10,
            "no_prestart_samples": invalid_prestart == 0,
        }
        return_passed = all(return_gate_checks.values())
        return_micro_stats = _experiment_stats([
            row for row in all_rows if row["stage"] == "RETURN_MODEL_MICRO_PASS"
            and current_experiment and row["experiment_id"] == current_experiment
            and int(row["entry_ts"]) >= experiment_start_ms
        ])
        return_micro_passed = (return_micro_stats["samples"] >= 100
                               and _profit_factor_value(return_micro_stats) > 1.2
                               and return_micro_stats["expectancy_r"] > 0.1
                               and return_micro_stats["opportunities"] >= 20
                               and return_micro_stats["trading_days"] >= 10
                               and invalid_prestart == 0)
        return_executable_stats = _experiment_stats([
            row for row in all_rows if row["stage"] == "RETURN_MODEL_EXECUTABLE_PASS"
            and current_experiment and row["experiment_id"] == current_experiment
            and int(row["entry_ts"]) >= experiment_start_ms
        ])
        return_executable_passed = (return_executable_stats["samples"] >= 100
                                    and _profit_factor_value(return_executable_stats) > 1.2
                                    and return_executable_stats["expectancy_r"] > 0.1
                                    and return_executable_stats["opportunities"] >= 20
                                    and return_executable_stats["trading_days"] >= 10
                                    and invalid_prestart == 0)
        try:
            gap_state = json.loads((ROOT / "data" / "okx_gap_shadow_state.json").read_text())
            gap_experiment = gap_state.get("experiment_id")
            gap_start_ms = int(datetime.fromisoformat(
                gap_state["experiment_started_at"].replace("Z", "+00:00")
            ).timestamp() * 1000)
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            gap_experiment, gap_start_ms = None, 0
        gap_rows = [row for row in all_rows if row["stage"] == "GAP_FADE_EXECUTABLE_PASS"
                    and gap_experiment and row["experiment_id"] == gap_experiment
                    and int(row["entry_ts"]) >= gap_start_ms]
        # Diagnostic gap experiments intentionally never become executable
        # signals.  Keep their labelled outcomes visible separately so the
        # 150m baseline and 60m challenger accumulate forward evidence rather
        # than disappearing from the state file merely because execution is
        # blocked.
        gap_diagnostic_rows = [row for row in all_rows
                               if str(row["stage"]).startswith("GAP_FADE_")
                               and gap_experiment and int(row["entry_ts"]) >= gap_start_ms]
        gap_diagnostics = {
            f"{stage}:{experiment}": _experiment_stats([
                row for row in gap_diagnostic_rows
                if row["stage"] == stage and row["experiment_id"] == experiment
            ])
            for stage, experiment in sorted({(str(row["stage"]), str(row["experiment_id"]))
                                              for row in gap_diagnostic_rows})
        }
        gap_invalid_prestart = sum(bool(
            row["stage"] == "GAP_FADE_EXECUTABLE_PASS" and gap_experiment
            and row["experiment_id"] == gap_experiment and int(row["entry_ts"]) < gap_start_ms
        ) for row in all_rows)
        gap_stats = _experiment_stats(gap_rows)
        gap_gate_checks = {
            "samples_at_least_100": gap_stats["samples"] >= 100,
            "profit_factor_above_1_2": _profit_factor_value(gap_stats) > 1.2,
            "expectancy_above_0_1r": gap_stats["expectancy_r"] > 0.1,
            "at_least_20_opportunities": gap_stats["opportunities"] >= 20,
            "at_least_10_trading_days": gap_stats["trading_days"] >= 10,
            "no_prestart_samples": gap_invalid_prestart == 0,
        }
        gap_passed = all(gap_gate_checks.values())
        try:
            v5_experiment = str(gap_state["v5_experiment_id"])
            v5_start_ms = int(datetime.fromisoformat(
                str(gap_state["v5_experiment_started_at"]).replace("Z", "+00:00")
            ).timestamp() * 1000)
        except (KeyError, TypeError, ValueError):
            v5_experiment, v5_start_ms = "", 0
        v5_rows = [
            row for row in all_rows
            if row["stage"] == "GAP_FADE_V5_FORWARD"
            and v5_experiment and row["experiment_id"] == v5_experiment
            and int(row["entry_ts"]) >= v5_start_ms
        ]
        v5_invalid_prestart = sum(bool(
            row["stage"] == "GAP_FADE_V5_FORWARD"
            and v5_experiment and row["experiment_id"] == v5_experiment
            and int(row["entry_ts"]) < v5_start_ms
        ) for row in all_rows)
        v5_stats = _experiment_stats(v5_rows)
        v5_symbol_edge_sizing = _v5_symbol_edge_sizing_stats(v5_rows)
        v5_gate_checks = _v5_forward_gate_checks(v5_stats, v5_invalid_prestart)
        v5_passed = all(v5_gate_checks.values())
        try:
            v5_strict_experiment = str(gap_state["v5_strict_confirm_experiment_id"])
            v5_strict_stage = str(gap_state["v5_strict_confirm_stage"])
            v5_strict_start_ms = int(datetime.fromisoformat(
                str(gap_state["v5_strict_confirm_experiment_started_at"]).replace("Z", "+00:00")
            ).timestamp() * 1000)
        except (KeyError, TypeError, ValueError):
            v5_strict_experiment, v5_strict_stage, v5_strict_start_ms = "", "", 0
        v5_strict_rows = [
            row for row in all_rows
            if v5_strict_stage and row["stage"] == v5_strict_stage
            and v5_strict_experiment and row["experiment_id"] == v5_strict_experiment
            and int(row["entry_ts"]) >= v5_strict_start_ms
        ]
        v5_strict_invalid_prestart = sum(bool(
            v5_strict_stage and row["stage"] == v5_strict_stage
            and v5_strict_experiment and row["experiment_id"] == v5_strict_experiment
            and int(row["entry_ts"]) < v5_strict_start_ms
        ) for row in all_rows)
        v5_strict_stats = _experiment_stats(v5_strict_rows)
        v5_strict_gate_checks = _v5_forward_gate_checks(
            v5_strict_stats, v5_strict_invalid_prestart
        )
        v5_symbol_edge_sizing_gate_checks = {
            "samples_at_least_25": v5_symbol_edge_sizing["samples"] >= 25,
            "at_least_5_trading_days": v5_symbol_edge_sizing["trading_days"] >= 5,
            "weighted_return_above_baseline": (
                v5_symbol_edge_sizing["symbol_edge_weighted"]["return_pct"]
                > v5_symbol_edge_sizing["baseline"]["return_pct"]
            ),
            "weighted_drawdown_not_above_baseline": (
                v5_symbol_edge_sizing["symbol_edge_weighted"]["max_drawdown_pct"]
                <= v5_symbol_edge_sizing["baseline"]["max_drawdown_pct"]
            ),
            "no_missing_symbol_scores": v5_symbol_edge_sizing["missing_symbol_scores"] == 0,
            "same_daily_gross": v5_symbol_edge_sizing["max_daily_gross_difference_pct_points"] <= 1e-8,
        }
        try:
            micro_model_state = json.loads((ROOT / "data" / "okx_microstructure_model_state.json").read_text())
        except (OSError, json.JSONDecodeError):
            micro_model_state = {"passed": False, "forward": {"samples": 0}}
        micro_model_passed = bool(micro_model_state.get("passed"))
        try:
            micro_audit = json.loads((ROOT / "data" / "okx_micro_execution_audit.json").read_text())
        except (OSError, json.JSONDecodeError):
            micro_audit = {"passed": False}
        micro_execution_ready = bool(
            micro_model_passed and micro_audit.get("passed")
            and micro_audit.get("experiment_id") == micro_model_state.get("experiment_id")
        )
        # Only the fully executable challenger may be promoted.  The broader
        # model and micro-aligned layers remain diagnostics and cannot bypass
        # the liquidity assumptions of the eventual order path.
        qualified = ("micro_barrier_executable" if micro_model_passed
                     else "gap_fade_executable" if gap_passed
                     else "return_model_executable" if return_executable_passed
                     else "rule_micro_aligned" if rule_passed else None)
        STATE_PATH.write_text(json.dumps({
            "updated_at": datetime.now(UTC).isoformat(), "pending": pending,
            "all": all_stats, "micro_aligned": aligned_stats, "micro_unaligned": unaligned_stats,
            "by_stage": stage_stats,
            "return_model": return_stats,
            "return_model_micro": return_micro_stats,
            "return_model_executable": return_executable_stats,
            "return_experiment_id": current_experiment,
            "return_invalid_prestart_samples": invalid_prestart,
            "return_gate_checks": return_gate_checks,
            "gap_fade_executable": gap_stats,
            "gap_shadow_diagnostics": gap_diagnostics,
            "gap_experiment_id": gap_experiment,
            "gap_invalid_prestart_samples": gap_invalid_prestart,
            "gap_gate_checks": gap_gate_checks,
            "v5_forward": v5_stats,
            "v5_experiment_id": v5_experiment,
            "v5_invalid_prestart_samples": v5_invalid_prestart,
            "v5_gate_checks": v5_gate_checks,
            "v5_forward_passed": v5_passed,
            "v5_strict_confirm_forward": v5_strict_stats,
            "v5_strict_confirm_experiment_id": v5_strict_experiment,
            "v5_strict_confirm_stage": v5_strict_stage,
            "v5_strict_confirm_invalid_prestart_samples": v5_strict_invalid_prestart,
            "v5_strict_confirm_gate_checks": v5_strict_gate_checks,
            "v5_strict_confirm_forward_passed": all(v5_strict_gate_checks.values()),
            "v5_strict_confirm_execution_ready": False,
            "v5_strict_confirm_promotion_block_reason": "independent shadow diagnostic; no audited execution path",
            "v5_symbol_edge_sizing": v5_symbol_edge_sizing,
            "v5_symbol_edge_sizing_gate_checks": v5_symbol_edge_sizing_gate_checks,
            "v5_symbol_edge_sizing_forward_passed": all(v5_symbol_edge_sizing_gate_checks.values()),
            "micro_barrier_executable": micro_model_state.get("forward") or {},
            "micro_barrier_experiment_id": micro_model_state.get("experiment_id"),
            "micro_barrier_gate_checks": micro_model_state.get("gate_checks") or {},
            "passed": bool(qualified), "qualified_strategy": qualified,
            "execution_ready": bool(qualified == "micro_barrier_executable" and micro_execution_ready),
            "micro_execution_audit": micro_audit,
            "promotion_gate": "at least 100 fresh labeled samples, PF>1.2 and expectancy>0.1R; then execution-path audit",
        }, ensure_ascii=False, indent=2))

    def run(self) -> None:
        while self.running:
            completed = self.label_once()
            if completed:
                LOG.info("labeled %s shadow signals", completed)
            for _ in range(30):
                if not self.running:
                    break
                time.sleep(1)


def main() -> None:
    labeler = Labeler()
    signal.signal(signal.SIGTERM, lambda *_: labeler.stop())
    signal.signal(signal.SIGINT, lambda *_: labeler.stop())
    labeler.run()


if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")
    main()
