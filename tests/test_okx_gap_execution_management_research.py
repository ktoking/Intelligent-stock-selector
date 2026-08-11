from __future__ import annotations

import pandas as pd

from scripts.okx_gap_execution_management_research import managed_outcome
from scripts.okx_gap_strategy_v4 import V4Config


def _row(path, exit_price=100.0):
    return pd.Series({
        "relative_gap": -200.0,
        "atr_bps": 100.0,
        "entry": 100.0,
        "path_150": path,
        "exit_30": exit_price,
    })


def test_same_bar_original_stop_precedes_new_breakeven_trigger():
    row = _row([(102.0, 98.0, 101.0)] * 6)
    net, reason = managed_outcome(row, 30, V4Config(), "breakeven_at_1r")
    assert reason == "managed_stop"
    assert net < -1.2


def test_breakeven_activates_only_for_following_bar():
    row = _row([
        (101.3, 99.5, 101.0),
        (101.1, 99.8, 100.0),
        (100.2, 99.8, 100.0),
        (100.2, 99.8, 100.0),
        (100.2, 99.8, 100.0),
        (100.2, 99.8, 100.0),
    ])
    net, reason = managed_outcome(row, 30, V4Config(), "breakeven_at_1r")
    assert reason == "managed_stop"
    assert net == -0.14


def test_partial_take_profit_preserves_only_remaining_horizon_fraction():
    row = _row([(102.0, 99.5, 101.0)] * 6, exit_price=101.0)
    net, reason = managed_outcome(row, 30, V4Config(), "partial_40_at_1_5r")
    # 40% at 1.5R with a 1.2% stop distance, then 60% at +1%, less 14bp.
    assert reason == "horizon_partial"
    assert round(net, 6) == 1.18
