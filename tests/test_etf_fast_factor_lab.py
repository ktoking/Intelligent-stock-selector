import numpy as np
import pandas as pd
import pytest

from scripts.equity_factor_lab import simulate as reference_simulate
from scripts.factor_portfolio_engine import simulate
from scripts.etf_fast_factor_lab import all_targets, channel, signals, choose


def test_array_engine_matches_reference_equity_costs_and_cash():
    rng=np.random.default_rng(19)
    index=pd.date_range("2024-01-01",periods=90).strftime("%Y-%m-%d")
    op=pd.DataFrame(100*np.exp(rng.normal(0,.05,(90,4))),index=index,columns=list("ABCD"))
    p={"open":op,"close":op*np.exp(rng.normal(0,.03,(90,4)))}
    raw=rng.random((90,4));w=pd.DataFrame(raw/raw.sum(axis=1)[:,None]*rng.uniform(0,1,(90,1)),index=index,columns=op.columns)
    for rebalance in (1,5,20):
        for cost in (0,7,20):
            a=simulate(p,w,1,90,rebalance=rebalance,cost_bps=cost)
            b=reference_simulate(p,w,1,90,rebalance=rebalance,cost_bps=cost)
            pd.testing.assert_frame_equal(pd.DataFrame(a["daily"]),pd.DataFrame(b["daily"]),atol=1e-7,rtol=1e-12)
            assert a["metrics"]["annualized_pct"]==pytest.approx(b["metrics"]["annualized_pct"],abs=1e-9)


def test_channel_can_break_previous_range_and_exit_without_future_data():
    c=pd.Series([10.,11.,10.,12.,13.,8.,9.])
    assert channel(c,3,2).tolist()==[0,0,0,1,1,0,0]


def test_all_fast_factors_and_sizing_are_prefix_invariant():
    rng=np.random.default_rng(12)
    c=pd.DataFrame(100*np.exp(np.cumsum(rng.normal(.001,.02,(320,4)),axis=0)),columns=["QQQ","SPY","TQQQ","UPRO"])
    other=c.copy();other.iloc[300:]*=5
    cfg,a=all_targets({"close":c});cfg2,b=all_targets({"close":other})
    assert len(cfg)==60
    for name in a:
        pd.testing.assert_frame_equal(a[name].iloc[:300],b[name].iloc[:300])
        assert a[name].min().min()>=0
        assert a[name].sum(axis=1).max()<=1+1e-10
        assert np.allclose(a[name].iloc[:253].to_numpy(),0)


def test_return_and_risk_rankings_are_explicitly_different():
    def r(name,growth,dd):
        part={"annualized_pct":growth,"max_drawdown_close_pct":dd}
        return {"name":name,"train":part,"validation":part}
    board=[r("higher_return",45,60),r("lower_risk",20,10)]
    assert choose(board,"return_first")["name"]=="higher_return"
    assert choose(board,"calmar")["name"]=="lower_risk"
