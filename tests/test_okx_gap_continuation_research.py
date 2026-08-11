from __future__ import annotations

import pandas as pd

from scripts.okx_gap_continuation_research import (
    causal_continuation_trades,
    continuation_base,
    continuation_outcome,
)
from scripts.okx_gap_strategy_v4 import V4Config
from scripts.okx_gap_strategy_v5 import AdaptiveHorizonConfig
from scripts.yfinance_gap_proxy_walkforward import SymbolEdgeGateConfig


def test_continuation_outcome_follows_positive_gap():
    row = pd.Series({
        "relative_gap": 200.0, "atr_bps": 100.0, "entry": 100.0,
        "path_150": [(101.0, 99.5, 100.5)] * 18,
        "exit_30": 101.0,
    })
    net, reason = continuation_outcome(row, 30, V4Config(), "fixed_300bp")
    assert reason == "horizon"
    assert net > 0.8


def test_prior_aligned_filter_and_side_are_causal():
    rows = pd.DataFrame([{
        "relative_gap": 200.0, "relative_previous": 50.0, "relative_first5": -20.0,
        "spy_gap": 0.0, "relative_gap_rank": 1.0, "exit_30": 101.0,
        "exit_60": 102.0, "exit_90": 103.0, "atr_bps": 100.0, "entry": 100.0,
        "path_150": [(101.0, 99.5, 100.5)] * 18, "date": "d1", "symbol": "A",
        "entry_time": 1,
    }])
    value = continuation_base(rows, "prior_aligned", "fixed_300bp")
    assert len(value) == 1
    assert value.iloc[0].side == "LONG"
    assert value.iloc[0].net_30 > 0


def test_cross_source_gate_uses_only_proxy_rows_before_current_day():
    days = [f"2026-01-{index:02d}" for index in range(1, 22)]
    base_rows = []
    for day in days[:20]:
        base_rows.append({
            "date": day, "symbol": "A-USDT-SWAP", "side": "LONG", "relative_gap": 200.0,
            "entry_time": 1, "stop_bps": 300.0,
            "net_30": 1.0, "net_60": 0.9, "net_90": 0.8,
            "reason_30": "horizon", "reason_60": "horizon", "reason_90": "horizon",
        })
    base_rows.append({
        "date": days[-1], "symbol": "A-USDT-SWAP", "side": "LONG", "relative_gap": 200.0,
        "entry_time": 1, "stop_bps": 300.0,
        "net_30": -10.0, "net_60": -10.0, "net_90": -10.0,
        "reason_30": "horizon", "reason_60": "horizon", "reason_90": "horizon",
    })
    proxy_rows = [
        {"date": f"2025-12-{index:02d}", "symbol": "A", "net_60": 1.0,
         "net_120": 1.0, "net_180": 1.0}
        for index in range(1, 9)
    ]
    # A huge current-date proxy loss must not alter the prior score.
    proxy_rows.append({
        "date": days[-1], "symbol": "A", "net_60": -100.0,
        "net_120": -100.0, "net_180": -100.0,
    })
    trades = causal_continuation_trades(
        pd.DataFrame(base_rows), days,
        AdaptiveHorizonConfig(horizons_minutes=(30, 60, 90)),
        pd.DataFrame(proxy_rows),
        SymbolEdgeGateConfig(lookback_sessions=240, min_samples=8),
    )
    assert len(trades) == 1
    assert trades[0]["net_pct"] == -10.0
    assert trades[0]["proxy_symbol_expectancy_pct"] == 1.0
