from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from scripts.okx_gap_strategy_v4 import (
    EdgeGate,
    V4Config,
    candidate_eligible,
    causal_gate_trades,
    latest_completed_us_session,
    select_candidate,
    trade_net_pct,
)


def _row(day: str, side: str, net_pct: float, symbol: str = "TEST-USDT-SWAP") -> dict:
    relative_gap = 200.0 if side == "SHORT" else -200.0
    return {
        "date": day, "symbol": symbol, "entry_time": 1_000,
        "relative_gap": relative_gap, "side": side, "net_pct": net_pct,
        "exit_reason": "horizon", "atr_bps": 100.0,
    }


def test_causal_gate_uses_only_prior_sessions():
    days = [f"2026-01-{value:02d}" for value in range(1, 13)]
    rows = [_row(day, "SHORT", 1.0) for day in days[:10]]
    rows.append(_row(days[10], "SHORT", -99.0))
    rows.append(_row(days[11], "SHORT", 1.0))
    gate = EdgeGate("test", lookback_sessions=10, min_side_samples=7, min_side_expectancy_pct=0.1)
    trades = causal_gate_trades(pd.DataFrame(rows), days, gate, V4Config())
    assert [item["date"] for item in trades] == [days[10]]


def test_side_gate_does_not_mix_long_and_short_histories():
    days = [f"2026-02-{value:02d}" for value in range(1, 12)]
    rows = [_row(day, "LONG", 1.0) for day in days[:10]]
    rows.append(_row(days[10], "SHORT", 1.0))
    gate = EdgeGate("test", lookback_sessions=10, min_side_samples=7, min_side_expectancy_pct=0.1)
    assert causal_gate_trades(pd.DataFrame(rows), days, gate, V4Config()) == []


def test_30_minute_exit_uses_six_bars_and_stop_precedence():
    row = pd.Series({
        "relative_gap": -200.0, "atr_bps": 100.0, "entry": 100.0,
        "exit_30": 102.0,
        "path_150": [(101.0, 99.5, 100.0)] * 5 + [(101.0, 98.0, 100.0)] + [(103.0, 102.0, 103.0)],
    })
    value, reason = trade_net_pct(row, V4Config())
    assert reason == "atr_stop"
    assert value == pytest.approx(-1.34)


def _parts(value: float) -> dict:
    return {
        name: {"trades": 20, "equal_weight_net_pct": value, "expectancy_pct_per_trade": value / 20,
               "profit_factor": 1.2}
        for name in ("validation_a", "validation_b", "validation_c")
    }


def test_selection_ignores_diagnostic_and_latest_metrics():
    stronger = _parts(4.0)
    weaker = _parts(2.0)
    assert candidate_eligible(stronger)
    results = {
        "strong": {"folds": {**stronger, "diagnostic": {"equal_weight_net_pct": -999}}},
        "weak": {"folds": {**weaker, "diagnostic": {"equal_weight_net_pct": 999}}},
    }
    assert select_candidate(results) == "strong"


def test_latest_completed_session_includes_post_close_friday():
    ny = ZoneInfo("America/New_York")
    assert str(latest_completed_us_session(datetime(2026, 8, 7, 21, 0, tzinfo=ny))) == "2026-08-07"
    assert str(latest_completed_us_session(datetime(2026, 8, 8, 9, 0, tzinfo=ny))) == "2026-08-07"
    assert str(latest_completed_us_session(datetime(2026, 8, 7, 10, 0, tzinfo=ny))) == "2026-08-06"
