from __future__ import annotations

import pandas as pd

from scripts.okx_gap_strategy_v5 import AdaptiveHorizonConfig
from scripts.okx_gap_symbol_edge_research import causal_symbol_edge_trades, tail_robustness


def _row(day: str, symbol: str, net: float) -> dict:
    return {
        "date": day,
        "symbol": symbol,
        "entry_time": 1_000,
        "side": "LONG",
        "relative_gap": -200.0,
        "stop_bps": 100.0,
        "net_30": net,
        "net_60": net - 0.1,
        "net_90": net - 0.2,
        "reason_30": "horizon",
        "reason_60": "horizon",
        "reason_90": "horizon",
    }


def test_symbol_edge_filter_uses_prior_rows_and_cannot_see_current_outcome():
    days = [f"day-{index:02d}" for index in range(21)]
    rows = []
    for day in days[:20]:
        rows.append(_row(day, "GOOD", 1.0))
        rows.append(_row(day, "BAD", -0.8))
    # BAD has a huge current winner, but its score must still use prior losses.
    rows.append(_row(days[20], "GOOD", -10.0))
    rows.append(_row(days[20], "BAD", 100.0))
    config = AdaptiveHorizonConfig(lookback_sessions=20, min_side_samples=7)
    trades = causal_symbol_edge_trades(
        pd.DataFrame(rows), days, config,
        require_positive_edge=True, rank_by_edge=False,
    )
    assert [item["symbol"] for item in trades] == ["GOOD"]
    assert trades[0]["net_pct"] == -10.0
    assert trades[0]["prior_symbol_samples"] == 20


def test_symbol_edge_shrinkage_falls_back_to_global_for_unseen_symbol():
    days = [f"day-{index:02d}" for index in range(21)]
    rows = [_row(day, "GOOD", 1.0) for day in days[:20]]
    rows.append(_row(days[20], "NEW", 0.5))
    config = AdaptiveHorizonConfig(lookback_sessions=20, min_side_samples=7)
    trades = causal_symbol_edge_trades(
        pd.DataFrame(rows), days, config,
        require_positive_edge=True, rank_by_edge=True,
    )
    assert len(trades) == 1
    assert trades[0]["symbol"] == "NEW"
    assert trades[0]["prior_symbol_samples"] == 0
    assert trades[0]["shrunk_symbol_edge_pct"] == 1.0


def test_tail_robustness_caps_winners_and_leaves_source_unchanged():
    trades = [
        {"date": "d1", "symbol": "A", "net_pct": 10.0, "stop_bps": 100.0},
        {"date": "d2", "symbol": "B", "net_pct": -1.0, "stop_bps": 100.0},
    ]
    value = tail_robustness(trades, AdaptiveHorizonConfig())
    assert value["cap_single_trade_profit_pct"]["3.0"]["trade_metrics"]["equal_weight_net_pct"] == 2.0
    assert trades[0]["net_pct"] == 10.0
    assert value["remove_top_winners"]["1"]["trade_metrics"]["trades"] == 1
