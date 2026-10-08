import numpy as np
import pandas as pd

from scripts.etf_core_factor_lab import Config, SECTORS, feature_cache, select, weights
from scripts.etf_factor_ablation import targets


def example_panel():
    rng = np.random.default_rng(41)
    names = ["SPY", "QQQ", "TQQQ", "UPRO", *SECTORS, "GLD", "IEF", "SHY"]
    prices = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(.001, .012, (240, len(names))), axis=0)), columns=names)
    return {"close": prices}


def test_future_candles_cannot_change_past_core_or_satellite_allocations():
    p = example_panel()
    altered = {"close": p["close"].copy()}
    altered["close"].iloc[225:] *= 3
    a, b = feature_cache(p), feature_cache(altered)
    for core in ("SPY", "QQQ", "UPRO", "TQQQ"):
        for family in ("momentum", "residual"):
            cfg = Config(core, .75, family, .4)
            wa, wb = weights(p,a,cfg), weights(altered,b,cfg)
            pd.testing.assert_frame_equal(wa.iloc[:225],wb.iloc[:225])
            assert wa.min().min() >= 0
            assert wa.sum(axis=1).max() <= 1 + 1e-10


def test_risk_off_uses_only_causal_defensive_assets_and_volatility_cap():
    p = example_panel()
    f = feature_cache(p)
    f["risk_on"].loc[220:, "QQQ"] = False
    f["defensive_momentum"].loc[220:, "GLD"] = .1
    f["defensive_momentum"].loc[220:, "IEF"] = .05
    names = list(p["close"])
    f["covariance"][220:] = np.eye(len(names)) * .25
    w = weights(p,f,Config("TQQQ",.75,"momentum",.25))
    assert np.allclose(w.GLD.iloc[220:], .5)
    assert np.allclose(w.drop(columns="GLD").iloc[220:].to_numpy(), 0)


def test_leveraged_core_allocates_real_fund_instead_of_synthetic_leverage():
    p = example_panel()
    f = feature_cache(p)
    f["risk_on"].loc[220:, "QQQ"] = True
    f["covariance"][220:] = np.eye(len(p["close"].columns)) * .01
    w = weights(p,f,Config("TQQQ",.75,"residual",.4))
    assert np.allclose(w.TQQQ.iloc[220:], .75)
    assert np.allclose(w.QQQ.iloc[220:], 0)
    assert np.allclose(w.UPRO.iloc[220:], 0)


def test_selection_rejects_losses_and_prefers_worse_segment_strength():
    def entry(name, train, val):
        return {"name":name,"train":{"return_pct":train,"annualized_pct":train,"max_drawdown_close_pct":10},
                "validation":{"return_pct":val,"annualized_pct":val,"max_drawdown_close_pct":10}}
    assert select([entry("spike",100,-1)]) is None
    assert select([entry("spike",100,5),entry("steady",20,20)])["name"] == "steady"


def test_ablation_warmup_and_future_independence():
    p = example_panel()
    altered = {"close":p["close"].copy()}
    altered["close"].iloc[225:] *= 5
    for rule in ("always","ma200","ma200_momentum","ma200_momentum_defense","ma200_momentum_defense_vol40"):
        a,b = targets(p,"TQQQ",rule),targets(altered,"TQQQ",rule)
        assert np.allclose(a.iloc[:200].to_numpy(),0)
        pd.testing.assert_frame_equal(a.iloc[:225],b.iloc[:225])
        assert a.min().min() >= 0
        assert a.sum(axis=1).max() <= 1 + 1e-10
