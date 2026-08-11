from __future__ import annotations

import pandas as pd

from scripts.okx_gap_stop_trigger_research import (
    replay_close_trigger,
    research,
)
from scripts.okx_gap_strategy_v3 import metrics


def _trade(day: str, symbol: str, *, reason: str, net_pct: float) -> dict:
    return {
        "date": day,
        "symbol": symbol,
        "entry_time": 1_000_000,
        "exit_time": 2_800_000,
        "exit_time_basis": "first_triggering_5m_bar_end" if reason == "atr_stop" else "scheduled_horizon",
        "stop_bar_number": 1 if reason == "atr_stop" else None,
        "side": "LONG",
        "relative_gap_bps": -200.0,
        "horizon_minutes": 30,
        "prior_side_samples": 10,
        "prior_best_horizon_expectancy_pct": 0.2,
        "stop_bps": 100.0,
        "net_pct": net_pct,
        "exit_reason": reason,
    }


def _row(path, *, exit_price=100.0) -> pd.Series:
    return pd.Series({
        "entry": 100.0,
        "path_150": path,
        "exit_30": exit_price,
    })


def test_close_confirmation_ignores_wick_then_exits_on_later_completed_close():
    row = _row([
        (100.5, 98.9, 100.1),
        (100.2, 98.5, 98.8),
        (99.0, 98.0, 98.5),
        (99.0, 98.0, 98.5),
        (99.0, 98.0, 98.5),
        (99.0, 98.0, 98.5),
    ])
    value = replay_close_trigger(
        row, _trade("d1", "A", reason="atr_stop", net_pct=-1.14),
        round_trip_cost_bps=14.0,
    )
    assert value["exit_reason"] == "five_minute_close_confirmed_stop"
    assert value["stop_bar_number"] == 2
    assert value["net_pct"] == -1.34
    assert value["label_end_time"] == 1_000_000 + 10 * 60_000


def test_two_r_intrabar_emergency_is_explicitly_synthetic():
    row = _row([(100.5, 97.9, 99.5)] * 6)
    value = replay_close_trigger(
        row, _trade("d1", "A", reason="atr_stop", net_pct=-1.14),
        round_trip_cost_bps=14.0,
        emergency_multiple=2.0,
    )
    assert value["exit_reason"] == "synthetic_2r_intrabar_emergency"
    assert value["exit_time_basis"] == "five_minute_extreme_threshold_assumed_fill"
    assert value["net_pct"] == -2.14


def test_research_is_diagnostic_only_and_preserves_frozen_trade_count():
    days = ["d1", "d2"]
    trades = [
        _trade("d1", "A", reason="atr_stop", net_pct=-1.14),
        _trade("d2", "B", reason="horizon", net_pct=-0.14),
    ]
    flat = [(100.2, 98.9, 100.0)] + [(100.2, 99.5, 100.0)] * 5
    rows = pd.DataFrame([
        {"date": "d1", "symbol": "A", "entry": 100.0, "path_150": flat, "exit_30": 100.0},
        {"date": "d2", "symbol": "B", "entry": 100.0, "path_150": flat, "exit_30": 100.0},
    ])
    source = {
        "generated_at": "source",
        "effective_sessions": {"count": 2, "start": "d1", "end": "d2"},
        "trade_filter": {"round_trip_cost_bps": 14.0},
        "metrics": metrics(trades),
        "trades": trades,
    }
    value = research(rows, source, generated_at="generated", fingerprints={"test": "hash"})
    assert value["promotion_passed"] is False
    assert value["research_governance"]["explicit_trial_count"] == 2
    assert value["research_governance"]["selection_eligible"] is False
    assert value["stop_path_diagnostics"]["all"]["wick_only_trigger_bars"] == 1
    assert value["baseline"]["all"]["trades"] == 2
    assert all(item["all"]["trades"] == 2 for item in value["diagnostics"].values())
