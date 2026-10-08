from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from research.daily_factor_lab.core import Panel, Policy, simulate
from research.watchlist_adaptive_cost import (
    FACTORS, adaptive_scores, panel_for_policy_changes, registry, score_panels,
)
from tests.test_daily_factor_lab_core import synthetic_panel, simple_policy


def factor_fixture():
    panel = synthetic_panel()
    n = len(panel.dates)
    panel.fields["residual_momentum"] = np.tile([2., 3., 1.], (n, 1))
    panel.fields["near_high"] = np.tile([1., 2., 3.], (n, 1))
    return panel


def truncated(panel, last):
    return Panel(panel.dates[:last], panel.symbols, panel.stock_mask,
                 {key: value[:last].copy() for key, value in panel.fields.items()}, panel.metadata)


def test_adaptive_weights_and_scores_are_prefix_invariant():
    panel = factor_fixture()
    args = dict(horizon=5, lookback=10, min_periods=3, min_stocks=2)
    full = adaptive_scores(panel, **args)
    prefix = adaptive_scores(truncated(panel, 75), **args)
    for key in ("weights", "adaptive", "matured_ic", "trailing_ic"):
        np.testing.assert_allclose(full[key][:75], prefix[key], equal_nan=True, atol=1e-14)
    # Perturb all future prices/factors. Historical factor weights must not move.
    for name in ("close", "open", *FACTORS):
        panel.fields[name][75:] *= np.tile([5., .2, 2.], (len(panel.dates) - 75, 1))
    changed = adaptive_scores(panel, **args)
    np.testing.assert_allclose(full["weights"][:75], changed["weights"][:75], atol=1e-14)
    np.testing.assert_allclose(full["adaptive"][:75], changed["adaptive"][:75], equal_nan=True)


def test_label_not_available_until_terminal_close_and_weights_bounded():
    panel = factor_fixture()
    result = adaptive_scores(panel, horizon=5, lookback=10, min_periods=3, min_stocks=2)
    assert np.isnan(result["matured_ic"][:5]).all()
    np.testing.assert_allclose(result["weights"][:7], np.full((7, 3), 1 / 3))
    np.testing.assert_allclose(result["weights"].sum(axis=1), 1.)
    assert result["weights"].min() >= 1 / 6 - 1e-12
    assert result["weights"].max() <= 2 / 3 + 1e-12
    for bad in ({"horizon": 0}, {"lookback": 2, "min_periods": 3}, {"learned_share": 2}):
        with pytest.raises(ValueError):
            adaptive_scores(panel, **bad)


def test_score_override_does_not_change_original_panel():
    panel = factor_fixture()
    before = panel.fields["momentum_risk"].copy()
    scores = adaptive_scores(panel, min_stocks=2)
    variants = score_panels(panel, scores)
    np.testing.assert_equal(panel.fields["momentum_risk"], before)
    assert variants["adaptive"].fields["open"] is panel.fields["open"]
    np.testing.assert_equal(variants["static"].fields["momentum_risk"], scores["static"])


def test_walk_forward_switch_uses_new_policy_score_at_prior_close():
    panel = factor_fixture()
    scores = adaptive_scores(panel, min_stocks=2)
    panels = score_panels(panel, scores)
    p = simple_policy(positions=1, rank_buffer=1)
    mode_policy = replace(p, name="changed")
    # Before first execution, static scoring chooses BBB; old factor chooses AAA.
    day = 70
    panels["static"].fields["momentum_risk"][day - 1, :2] = [0., 1.]
    rules = {p.name: {"policy": p, "score": "original"},
             mode_policy.name: {"policy": mode_policy, "score": "static"}}
    changes = {str(panel.dates[day].date()): mode_policy}
    mixed = panel_for_policy_changes(panels, rules, changes)
    result = simulate(mixed, mode_policy, panel.dates[day], panel.dates[day], policy_changes=changes)
    assert result["orders"][0]["symbol"] == "US.BBB"
    assert result["decisions"][0]["signal_date"] == str(panel.dates[day - 1].date())


def test_registry_preserves_all_nineteen_old_hypotheses():
    rules, additions = registry()
    assert len(rules) == 28
    assert len(additions) == 9
    assert "trend_heavy32" not in additions
    assert rules["trend_heavy32"]["policy"] == Policy(
        "trend_heavy32", annual_vol_target=.32, rank_buffer=5)
