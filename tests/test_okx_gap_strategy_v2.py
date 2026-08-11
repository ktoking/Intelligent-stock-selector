from __future__ import annotations

import pandas as pd

from scripts.okx_gap_strategy_v2 import V2Config, _trade_net_pct, select_candidates


def _row(**overrides):
    value = {
        "relative_gap": 250.0,
        "relative_first5": -40.0,
        "relative_previous": 100.0,
        "relative_gap_rank": 0.9,
        "spy_gap": 20.0,
        "exit_90": 99.0,
        "entry": 100.0,
        "path_150": [(101.0, 99.5, 100.0)] * 18,
    }
    value.update(overrides)
    return value


def test_v2_filters_market_extremes_and_tail_gaps():
    rows = pd.DataFrame([
        _row(),
        _row(relative_gap=750.0),
        _row(spy_gap=100.0),
        _row(relative_gap_rank=0.5),
    ])
    selected = select_candidates(rows, V2Config())
    assert len(selected) == 1
    assert selected.iloc[0].relative_gap == 250.0


def test_v2_hard_stop_precedes_horizon_exit():
    row = pd.Series(_row(exit_90=110.0, path_150=[(104.0, 99.5, 99.0)] + [(101.0, 99.0, 100.0)] * 17))
    net, reason = _trade_net_pct(row, V2Config(stop_loss_pct=3.0))
    assert reason == "hard_stop"
    assert net == -3.14


def test_v2_horizon_applies_cost_when_stop_not_hit():
    row = pd.Series(_row(exit_90=101.0))
    net, reason = _trade_net_pct(row, V2Config(stop_loss_pct=3.0))
    assert reason == "horizon"
    # Positive relative gap means the fade is short, so a 1% rise is a loss.
    assert abs(net + 1.14) < 1e-9
