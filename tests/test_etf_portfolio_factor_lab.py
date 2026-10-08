import numpy as np
import pandas as pd

from scripts.etf_portfolio_factor_lab import specifications, targets


def fixture_prices():
    rng = np.random.default_rng(529)
    names = ["SOXL", "SMH", "TECL", "XLK", "TQQQ", "QQQ", "UPRO", "SPY", "GLD", "IEF", "SHY"]
    return pd.DataFrame(100*np.exp(np.cumsum(rng.normal(.001, .03, (380, len(names))), axis=0)), columns=names)


def test_portfolio_weights_are_causal_and_never_borrow_cash():
    close = fixture_prices()
    changed = close.copy()
    changed.iloc[340:] *= np.linspace(.01, 10, 40)[:, None]
    configs = specifications()
    assert len(configs) == len({x["name"] for x in configs}) == 30
    for cfg in configs:
        a, b = targets(close, cfg), targets(changed, cfg)
        pd.testing.assert_frame_equal(a.iloc[:340], b.iloc[:340])
        assert np.isfinite(a.to_numpy()).all()
        assert a.min().min() >= 0
        assert a.sum(axis=1).max() <= 1 + 1e-12
        assert a.iloc[:253].to_numpy().sum() == 0


def test_defense_keeps_inactive_bond_budget_in_cash():
    close = fixture_prices()
    # Both primary signal and bonds stay in downtrends; gold rises.
    close["SMH"] = 100*np.exp(-np.arange(len(close))*.003)
    close["IEF"] = 100*np.exp(-np.arange(len(close))*.001)
    close["GLD"] = 100*np.exp(np.arange(len(close))*.001)
    cfg = next(x for x in specifications() if x["name"] == "soxl100_gold_bond")
    w = targets(close, cfg)
    assert w.iloc[-1]["SOXL"] == 0
    assert w.iloc[-1]["GLD"] == .5
    assert w.iloc[-1]["IEF"] == 0
    assert w.iloc[-1].sum() == .5


def test_mixed_equity_sleeves_do_not_exceed_their_individual_budgets():
    close = fixture_prices()
    cfg = next(x for x in specifications() if x["name"] == "soxl75_TQQQ_gold_bond")
    w = targets(close, cfg)
    assert w["SOXL"].max() <= .75
    assert w["TQQQ"].max() <= .25
    assert ((w.GLD + w.IEF) <= 1-w.SOXL-w.TQQQ+1e-12).all()
