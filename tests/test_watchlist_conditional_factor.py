import numpy as np
import pandas as pd
import pytest

from research.daily_factor_lab.core import Panel
from research.watchlist_conditional_factor import (
    all_rules, bounded_weights, combine_rank_scores, conditional_features,
)
from tests.test_watchlist_adaptive_cost import truncated


def fixture():
    days, symbols = 270, [f"US.T{i}" for i in range(8)] + ["US.QQQ"]
    t = np.arange(days)[:, None]
    j = np.arange(len(symbols))[None, :]
    growth = np.r_[np.linspace(.0002, .0025, 8), .001][None, :]
    close = (40 + j * 5) * np.exp(t * growth + .02 * np.sin(t * .12 + j))
    returns = np.vstack([np.zeros(len(symbols)), close[1:] / close[:-1] - 1])
    fields = {
        "open": close.copy(), "close": close, "returns": returns,
        "high": close * 1.01, "low": close * .99,
        "volume": np.full_like(close, 1e6), "turnover": np.full_like(close, 2e7),
        "liquid20": np.full_like(close, 2e7), "vol20": np.full_like(close, .02),
        "mom63": np.full_like(close, .1),
        "momentum_risk": np.broadcast_to(10 - j, close.shape).copy().astype(float),
        "residual_momentum": np.broadcast_to(j + 1, close.shape).copy().astype(float),
        "near_high": np.broadcast_to(np.sin(j) + 2, close.shape).copy(),
    }
    for span, ratio in ((20, .98), (50, .96), (60, .94), (100, .92), (150, .90), (200, .88)):
        fields[f"ema{span}"] = close * ratio
    return Panel(pd.bdate_range("2023-01-03", periods=days), symbols,
                 np.r_[np.ones(8, dtype=bool), False], fields, {})


def test_live_cost_reference_portfolios_and_weights_are_causal():
    panel = fixture()
    args = {"warmup_sessions": 60, "min_stocks": 2}
    full = conditional_features(panel, **args)
    early = conditional_features(truncated(panel, 220), **args)
    for mode in ("conditional", "net63", "net126"):
        np.testing.assert_allclose(full["weights"][mode][:220], early["weights"][mode], atol=1e-12)
        np.testing.assert_allclose(full["scores"][mode][:220], early["scores"][mode],
                                   atol=1e-12, equal_nan=True)
    np.testing.assert_allclose(full["reference_returns"][:220], early["reference_returns"],
                               atol=1e-12, equal_nan=True)
    assert any(r["metrics"]["orders"] > 0 for r in full["reference_audit"].values())
    assert any(r["metrics"]["cost_dollars"] > 0 for r in full["reference_audit"].values())
    assert not np.allclose(full["weights"]["net63"][-30:], 1 / 3)


def test_conditioned_sample_excludes_nonbuyable_assets_and_risk_off_signals():
    panel = fixture()
    panel.fields["ema50"][:, 0] = panel.fields["close"][:, 0] * 1.1
    panel.fields["ema100"][100:110, -1] = panel.fields["close"][100:110, -1] * 1.1
    features = conditional_features(panel, min_stocks=2, warmup_sessions=60)
    assert not features["eligible"][:, 0].any()
    assert np.isnan(features["matured_conditional_ic"][120:130]).all()
    # Risk off cannot erase valid held-stock ranks and become a liquidation rule.
    assert np.isfinite(features["scores"]["conditional"][100:110, 1:8]).all()


def test_weights_have_shrinkage_and_missing_information_falls_back():
    signal = np.array([[np.nan, np.nan, np.nan], [-1., -2., -3.], [2., 0., 0.]])
    weights = bounded_weights(signal)
    np.testing.assert_allclose(weights[:2], np.full((2, 3), 1 / 3))
    np.testing.assert_allclose(weights[2], [2 / 3, 1 / 6, 1 / 6])
    with pytest.raises(ValueError):
        bounded_weights(signal, learned_share=1.2)


def test_all_previous_hypotheses_are_preserved():
    rules, new = all_rules()
    assert len(rules) == 33
    assert len(new) == 5
    assert "cost_weekly32" in rules and "blend_weekly20" in rules
    assert all(rules[n]["policy"].schedule == "twice" for n in new)


def test_machine_epsilon_cannot_break_mathematically_tied_rankings():
    ranks = np.array([[[1., 1., 2 / 3], [.95, .95, 11 / 12]]])
    weights = np.array([[.4, 5 / 6 - .4, 1 / 6]])
    perturbed = np.nextafter(weights, np.inf)
    first = combine_rank_scores(ranks, weights)
    second = combine_rank_scores(ranks, perturbed)
    assert first[0, 0] == first[0, 1]
    np.testing.assert_array_equal(first, second)
    raw = np.sum(ranks * weights[:, None, :], axis=-1)
    assert np.max(np.abs(first - raw)) <= 5e-13
