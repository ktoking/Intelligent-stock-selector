from __future__ import annotations

import pandas as pd
import pytest

from scripts.okx_gap_strategy_v4 import V4Config
from scripts.okx_gap_strategy_v5 import (
    AdaptiveHorizonConfig,
    add_horizon_outcomes,
    causal_adaptive_horizon_trades,
    portfolio_metrics,
    robustness_report,
    risk_weighted_portfolio_metrics,
    resolved_trade_exit,
    side_horizon_choices,
    next_session_symbol_edge_scores,
)


def _row(day: str, side: str, net_30: float, net_60: float, net_90: float) -> dict:
    return {
        "date": day, "symbol": "TEST-USDT-SWAP", "entry_time": 1_000,
        "relative_gap": 200.0 if side == "SHORT" else -200.0, "side": side,
        "net_30": net_30, "net_60": net_60, "net_90": net_90,
        "reason_30": "horizon", "reason_60": "horizon", "reason_90": "horizon",
    }


def test_adaptive_horizon_uses_prior_same_side_only():
    days = [f"2026-01-{value:02d}" for value in range(1, 12)]
    rows = [_row(day, "SHORT", 1.0, 0.0, -1.0) for day in days[:10]]
    rows.append(_row(days[10], "SHORT", -99.0, 99.0, 99.0))
    config = AdaptiveHorizonConfig(lookback_sessions=10, min_side_samples=7)
    trades = causal_adaptive_horizon_trades(pd.DataFrame(rows), days, config)
    assert len(trades) == 1
    assert trades[0]["horizon_minutes"] == 30
    assert trades[0]["net_pct"] == -99.0


def test_side_horizon_choices_exposes_frozen_next_session_decision():
    rows = pd.DataFrame([
        _row(f"2026-01-{value:02d}", "LONG", 0.1, 0.5, -0.1)
        for value in range(1, 9)
    ])
    value = side_horizon_choices(rows, AdaptiveHorizonConfig(min_side_samples=7))
    assert value["LONG"]["horizon_minutes"] == 60
    assert value["LONG"]["prior_side_samples"] == 8


def test_adaptive_horizon_abstains_when_only_other_side_has_history():
    days = [f"2026-02-{value:02d}" for value in range(1, 12)]
    rows = [_row(day, "LONG", 1.0, 0.0, -1.0) for day in days[:10]]
    rows.append(_row(days[10], "SHORT", 1.0, 0.0, -1.0))
    config = AdaptiveHorizonConfig(lookback_sessions=10, min_side_samples=7)
    assert causal_adaptive_horizon_trades(pd.DataFrame(rows), days, config) == []


def test_portfolio_metrics_respects_gross_cap_and_compounds():
    trades = [
        {"date": "2026-01-01", "net_pct": 10.0},
        {"date": "2026-01-01", "net_pct": 10.0},
        {"date": "2026-01-02", "net_pct": -10.0},
    ]
    value = portfolio_metrics(trades, AdaptiveHorizonConfig(), initial_equity=10_000.0)
    assert value["daily"][0]["allocation_pct_each"] == 20.0
    assert value["final_equity"] == pytest.approx(10_192.0)


def test_risk_weighted_portfolio_scales_inverse_stop_and_gross_cap():
    trades = [
        {"date": "2026-01-01", "net_pct": 10.0, "stop_bps": 100.0},
        {"date": "2026-01-01", "net_pct": 10.0, "stop_bps": 200.0},
    ]
    config = AdaptiveHorizonConfig(risk_fraction=.0035)
    value = risk_weighted_portfolio_metrics(trades, config, initial_equity=10_000.0)
    assert value["daily"][0]["allocation_pct_each"] == [20.0, 17.5]
    assert value["final_equity"] == pytest.approx(10_375.0)


def test_robustness_report_charges_incremental_cost_and_is_deterministic():
    trades = [{
        "date": "2026-01-02", "net_pct": 1.0, "side": "LONG", "horizon_minutes": 30,
    }]
    config = AdaptiveHorizonConfig(lookback_sessions=0)
    value = robustness_report(trades, ["2026-01-02"], config, 14.0, bootstrap_samples=20)
    assert value["cost_sensitivity"]["14"]["trade_metrics"]["expectancy_pct_per_trade"] == 1.0
    assert value["cost_sensitivity"]["40"]["trade_metrics"]["expectancy_pct_per_trade"] == 0.74
    assert value["session_cluster_bootstrap"]["expectancy_pct_per_trade_median"] == 1.0


def test_add_horizon_outcomes_drops_incomplete_path_rows():
    complete = {
        "symbol": "A", "date": "2026-01-01", "relative_gap": 200.0,
        "atr_bps": 100.0, "entry": 100.0, "exit_30": 99.0, "exit_60": 99.0,
        "exit_90": 99.0, "path_150": [(100.0, 99.0, 99.5)] * 18,
    }
    incomplete = {**complete, "symbol": "B", "path_150": float("nan")}
    value = add_horizon_outcomes(pd.DataFrame([complete, incomplete]), V4Config())
    assert value.symbol.tolist() == ["A"]


def test_next_session_symbol_edge_scores_use_completed_history_and_shrink():
    days = [f"2026-01-{value:02d}" for value in range(1, 9)]
    rows = pd.DataFrame([
        _row(day, "LONG", 1.0, 0.0, -1.0) | {"symbol": "A"}
        for day in days
    ])
    config = AdaptiveHorizonConfig(lookback_sessions=8, min_side_samples=7)
    value = next_session_symbol_edge_scores(
        rows, days, config, symbol_lookback_sessions=8, prior_strength=2.0,
    )
    assert value["LONG"]["A"]["horizon_minutes"] == 30
    assert value["LONG"]["A"]["prior_symbol_samples"] == 8
    assert value["LONG"]["A"]["shrunk_symbol_edge_pct"] == 1.0
    assert value["LONG"]["A"]["allocation_multiplier"] == 1.5


def test_resolved_trade_exit_uses_first_triggering_five_minute_bar_end():
    row = pd.Series({
        "entry_time": 1_000,
        "entry": 100.0,
        "relative_gap": -200.0,
        "stop_bps": 100.0,
        "path_150": [
            (100.5, 99.5, 100.0),
            (100.2, 98.9, 99.0),
            (101.0, 98.0, 100.0),
        ],
    })
    value = resolved_trade_exit(row, 30, "atr_stop")
    assert value == {
        "exit_time": 1_000 + 10 * 60_000,
        "exit_time_basis": "first_triggering_5m_bar_end",
        "stop_bar_number": 2,
    }


def test_resolved_trade_exit_keeps_scheduled_horizon_for_non_stop():
    row = pd.Series({"entry_time": 1_000})
    assert resolved_trade_exit(row, 60, "horizon") == {
        "exit_time": 1_000 + 60 * 60_000,
        "exit_time_basis": "scheduled_horizon",
        "stop_bar_number": None,
    }


def test_adaptive_trade_records_stop_bar_exit_without_changing_return():
    days = ["2026-01-01", "2026-01-02"]
    history = _row(days[0], "LONG", 1.0, 0.0, -1.0)
    current = _row(days[1], "LONG", -1.14, -2.0, -3.0) | {
        "entry": 100.0,
        "stop_bps": 100.0,
        "path_150": [
            (100.5, 99.5, 100.0),
            (100.2, 98.9, 99.0),
        ],
        "reason_30": "atr_stop",
    }
    value = causal_adaptive_horizon_trades(
        pd.DataFrame([history, current]),
        days,
        AdaptiveHorizonConfig(lookback_sessions=1, min_side_samples=1),
    )
    assert len(value) == 1
    assert value[0]["net_pct"] == -1.14
    assert value[0]["exit_time"] == current["entry_time"] + 10 * 60_000
    assert value[0]["exit_time_basis"] == "first_triggering_5m_bar_end"
    assert value[0]["stop_bar_number"] == 2
