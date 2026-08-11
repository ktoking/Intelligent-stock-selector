from __future__ import annotations

import json

import pandas as pd
import pytest

from scripts.okx_gap_strategy_v3 import (
    V3Config,
    dynamic_stop_bps,
    load_event_sets,
    promotion_gate,
    risk_budget_notional,
    select_v3_candidates,
)


def _candidate(**overrides):
    value = {
        "relative_gap": 250.0,
        "relative_first5": -45.0,
        "relative_previous": 100.0,
        "relative_gap_rank": 0.9,
        "spy_gap": 15.0,
        "expected_net_pct": 0.35,
        "macro_day": 0.0,
        "earnings_window": 0.0,
        "entry": 100.0,
        "exit_90": 100.8,
        "atr_bps": 100.0,
        "path_150": [(101.0, 99.0, 100.0)] * 18,
    }
    value.update(overrides)
    return value


def test_dynamic_stop_is_atr_based_and_clamped():
    config = V3Config()
    assert dynamic_stop_bps(100.0, config) == 120.0
    assert dynamic_stop_bps(10.0, config) == config.min_stop_bps
    assert dynamic_stop_bps(500.0, config) == config.max_stop_bps


def test_risk_budget_notional_scales_down_with_wider_stop():
    narrow = risk_budget_notional(10_000.0, 0.0035, 100.0, 75.0, 10_000.0)
    wide = risk_budget_notional(10_000.0, 0.0035, 100.0, 300.0, 10_000.0)
    assert narrow == pytest.approx(4666.6667)
    assert wide == pytest.approx(1166.6667)


def test_v3_filters_low_expected_return_and_event_risk():
    rows = pd.DataFrame([
        _candidate(),
        _candidate(expected_net_pct=0.01),
        _candidate(macro_day=1.0),
        _candidate(earnings_window=1.0),
    ])
    selected = select_v3_candidates(rows, V3Config())
    assert len(selected) == 1
    assert selected.iloc[0].expected_net_pct == 0.35


def test_promotion_gate_requires_oos_profit_and_drawdown_limits():
    passing = {
        "validation": {"trades": 30, "win_rate_pct": 50, "profit_factor": 1.25, "equal_weight_net_pct": 2, "max_drawdown_pct_points": 8},
        "development": {"trades": 30, "win_rate_pct": 50, "profit_factor": 1.25, "equal_weight_net_pct": 2, "max_drawdown_pct_points": 8},
        "final_diagnostic": {"trades": 60, "win_rate_pct": 48, "profit_factor": 1.1, "equal_weight_net_pct": 1, "max_drawdown_pct_points": 10},
    }
    assert promotion_gate(passing, V3Config()) is True
    passing["development"]["equal_weight_net_pct"] = -0.1
    assert promotion_gate(passing, V3Config()) is False


def test_load_event_sets_builds_macro_and_earnings_windows(tmp_path):
    path = tmp_path / "events.json"
    path.write_text(json.dumps({
        "macro_events": [{"scheduled_at": "2026-07-14T12:30:00+00:00"}],
        "earnings": [{
            "symbol": "MU", "event_type": "EARNINGS_REPORTED",
            "scheduled_at": "2026-06-24T16:00:00-04:00",
        }],
    }))
    macro, earnings = load_event_sets(path)
    assert macro == {"2026-07-14"}
    assert earnings == {
        ("MU", "2026-06-23"), ("MU", "2026-06-24"), ("MU", "2026-06-25"),
    }
