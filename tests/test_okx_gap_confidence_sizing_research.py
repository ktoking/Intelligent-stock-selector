from __future__ import annotations

import pytest

from scripts.okx_gap_confidence_sizing_research import (
    capped_normalized_weights,
    confidence_multiplier,
    portfolio_metrics,
)
from scripts.okx_gap_strategy_v5 import AdaptiveHorizonConfig


def test_capped_normalized_weights_preserves_target_and_cap():
    value = capped_normalized_weights([1.0, 3.0], 0.30, 0.20)
    assert value == pytest.approx([0.10, 0.20])
    assert sum(value) == pytest.approx(0.30)


def test_confidence_multiplier_is_monotonic_and_bounded():
    assert confidence_multiplier(-99.0) == 0.5
    assert confidence_multiplier(0.0) == 1.0
    assert confidence_multiplier(0.25) == 1.25
    assert confidence_multiplier(99.0) == 1.5


def test_confidence_portfolio_keeps_same_gross_and_position_cap():
    trades = []
    for index, expectation in enumerate((2.0, 1.0, 0.0, -0.1, -0.1)):
        trades.append({
            "date": "2026-01-01",
            "symbol": f"S{index}",
            "stop_bps": 100.0,
            "net_pct": 1.0 if index == 0 else -1.0,
            "prior_best_horizon_expectancy_pct": expectation,
        })
    config = AdaptiveHorizonConfig()
    baseline = portfolio_metrics(trades, config, confidence_weighted=False)
    weighted = portfolio_metrics(trades, config, confidence_weighted=True)
    base_day = baseline["daily"][0]
    weighted_day = weighted["daily"][0]
    assert weighted_day["gross_allocation_pct"] == pytest.approx(base_day["gross_allocation_pct"])
    assert max(weighted_day["allocation_pct_each"]) <= 20.0
    assert weighted["return_pct"] > baseline["return_pct"]


def test_portfolio_accepts_a_causal_alternative_confidence_field():
    trades = [{
        "date": "2026-01-01", "stop_bps": 100.0, "net_pct": 1.0,
        "shrunk_symbol_edge_pct": 0.25,
    }]
    value = portfolio_metrics(
        trades, AdaptiveHorizonConfig(), confidence_weighted=True,
        confidence_field="shrunk_symbol_edge_pct",
    )
    assert value["return_pct"] == pytest.approx(0.2)
