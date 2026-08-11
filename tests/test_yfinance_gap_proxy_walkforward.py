from __future__ import annotations

import pandas as pd

from scripts.okx_gap_strategy_v4 import V4Config
from scripts.okx_gap_strategy_v5 import AdaptiveHorizonConfig
from scripts.yfinance_gap_proxy_walkforward import (
    SymbolEdgeGateConfig,
    _hourly_outcome,
    assess,
    base_candidates,
    build_proxy_rows,
    causal_symbol_edge_trades,
    causal_trades,
)


def _bars(open_price: float, closes: tuple[float, ...]) -> list[dict[str, float]]:
    return [
        {
            "open": open_price if index == 0 else closes[index - 1],
            "high": max(open_price, close) + 0.1,
            "low": min(open_price, close) - 0.1,
            "close": close,
            "volume": 1.0,
        }
        for index, close in enumerate(closes)
    ]


def test_proxy_entry_uses_open_or_next_hour_without_lookahead():
    market = {
        "d1": {
            "SPY": _bars(100, (100, 100, 100, 100, 100, 100)),
            "A": _bars(100, (100, 100, 100, 100, 100, 100)),
        },
        "d2": {
            "SPY": _bars(100, (100, 100, 100, 100, 100, 100)),
            "A": _bars(102, (101, 100.5, 100, 100, 100, 100)),
        },
    }
    at_open = build_proxy_rows(market, "open_prior_confirm", ("A",)).iloc[0]
    after_hour = build_proxy_rows(market, "hour1_or_prior_confirm", ("A",)).iloc[0]
    assert at_open.entry == 102
    assert after_hour.entry == 101
    assert after_hour.relative_first_hour < 0
    assert after_hour.exit_60 == 100.5


def test_continuation_mirror_uses_aligned_confirmation_and_gap_direction():
    market = {
        "d1": {
            "SPY": _bars(100, (100, 100, 100, 100, 100, 100)),
            "A": _bars(100, (101, 101, 101, 101, 101, 101)),
        },
        "d2": {
            "SPY": _bars(100, (100, 100, 100, 100, 100, 100)),
            "A": _bars(103, (104, 105, 106, 106, 106, 106)),
        },
    }
    rows = build_proxy_rows(market, "hour1_strict_continuation", ("A",))
    assert rows.iloc[0].side == "LONG"
    assert rows.iloc[0].relative_first_hour > 0
    candidates = base_candidates(
        rows,
        "hour1_strict_continuation",
        V4Config(min_relative_gap_bps=1, min_cross_sectional_rank=0),
        "close_only",
    )
    assert len(candidates) == 1
    assert candidates.iloc[0].net_60 > 0


def test_hourly_stop_has_conservative_precedence():
    row = pd.Series({
        "relative_gap": 200.0, "atr_bps": 100.0, "entry": 100.0,
        "path_hourly": [(102.0, 98.0, 99.0)] * 3,
        "exit_60": 99.0,
    })
    net, reason = _hourly_outcome(row, 60, V4Config())
    assert reason == "atr_stop"
    assert net < -1.0


def test_close_only_diagnostic_ignores_ambiguous_intrahour_stop():
    row = pd.Series({
        "relative_gap": 200.0, "atr_bps": 100.0, "entry": 100.0,
        "path_hourly": [(102.0, 98.0, 99.0)] * 3,
        "exit_60": 99.0,
    })
    net, reason = _hourly_outcome(row, 60, V4Config(), "close_only")
    assert reason == "horizon"
    assert net > 0.8


def test_hour_close_stop_ignores_wick_but_exits_on_close_breach():
    row = pd.Series({
        "relative_gap": 200.0, "side": "SHORT", "atr_bps": 100.0, "entry": 100.0,
        "path_hourly": [(102.0, 98.0, 99.0), (102.0, 99.0, 101.5), (101.0, 99.0, 99.0)],
        "exit_60": 99.0, "exit_120": 101.5,
    })
    first, first_reason = _hourly_outcome(row, 60, V4Config(), "hour_close_dynamic")
    second, second_reason = _hourly_outcome(row, 120, V4Config(), "hour_close_dynamic")
    assert first_reason == "horizon"
    assert first > 0.8
    assert second_reason == "hour_close_stop"
    assert second < -1.5


def test_causal_hourly_horizon_ignores_current_outcome():
    days = [f"d{index:02d}" for index in range(21)]
    rows = []
    for day in days[:20]:
        rows.append({
            "date": day, "symbol": "A", "side": "LONG", "relative_gap": -200.0,
            "stop_bps": 100.0, "net_60": 1.0, "net_120": 0.0, "net_180": -1.0,
            "reason_60": "horizon", "reason_120": "horizon", "reason_180": "horizon",
        })
    rows.append({
        "date": days[20], "symbol": "A", "side": "LONG", "relative_gap": -200.0,
        "stop_bps": 100.0, "net_60": -10.0, "net_120": 100.0, "net_180": 100.0,
        "reason_60": "horizon", "reason_120": "horizon", "reason_180": "horizon",
    })
    config = AdaptiveHorizonConfig(
        lookback_sessions=20, min_side_samples=7, horizons_minutes=(60, 120, 180),
    )
    trades = causal_trades(pd.DataFrame(rows), days, config)
    assert len(trades) == 1
    assert trades[0]["horizon_minutes"] == 60
    assert trades[0]["net_pct"] == -10.0


def test_symbol_edge_gate_cannot_use_current_winner():
    days = [f"d{index:02d}" for index in range(21)]
    rows = []
    for day in days[:20]:
        rows.extend([
            {
                "date": day, "symbol": "GOOD", "side": "LONG", "relative_gap": 200.0,
                "stop_bps": 300.0, "net_60": 1.0, "net_120": 1.0, "net_180": 1.0,
                "reason_60": "horizon", "reason_120": "horizon", "reason_180": "horizon",
            },
            {
                "date": day, "symbol": "BAD", "side": "LONG", "relative_gap": 190.0,
                "stop_bps": 300.0, "net_60": -1.0, "net_120": -1.0, "net_180": -1.0,
                "reason_60": "horizon", "reason_120": "horizon", "reason_180": "horizon",
            },
        ])
    rows.extend([
        {
            "date": days[-1], "symbol": "GOOD", "side": "LONG", "relative_gap": 200.0,
            "stop_bps": 300.0, "net_60": -10.0, "net_120": -10.0, "net_180": -10.0,
            "reason_60": "horizon", "reason_120": "horizon", "reason_180": "horizon",
        },
        {
            "date": days[-1], "symbol": "BAD", "side": "LONG", "relative_gap": 190.0,
            "stop_bps": 300.0, "net_60": 100.0, "net_120": 100.0, "net_180": 100.0,
            "reason_60": "horizon", "reason_120": "horizon", "reason_180": "horizon",
        },
    ])
    trades = causal_symbol_edge_trades(
        pd.DataFrame(rows),
        days,
        AdaptiveHorizonConfig(horizons_minutes=(60, 120, 180)),
        SymbolEdgeGateConfig(lookback_sessions=240, min_samples=8),
    )
    assert [item["symbol"] for item in trades] == ["GOOD"]
    assert trades[0]["net_pct"] == -10.0
    assert trades[0]["prior_symbol_samples"] == 20


def test_assess_returns_complete_report_for_empty_candidate_set():
    rows = pd.DataFrame(columns=[
        "relative_gap", "spy_gap", "relative_gap_rank", "relative_previous",
        "relative_first_hour", "atr_bps", "side", "date", "symbol", "entry",
        "path_hourly", "exit_60", "exit_120", "exit_180",
    ])
    value = assess(rows, "open_prior_confirm", [f"d{index:03d}" for index in range(30)])
    assert value["all"]["trades"] == 0
    assert value["development_eligible"] is False
    assert value["risk_weighted_portfolio"]["return_pct"] == 0.0
