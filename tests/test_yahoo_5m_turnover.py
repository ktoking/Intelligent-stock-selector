from scripts.yahoo_5m_factor_lab import simulate
from tests.test_yahoo_5m_exits import fixture


def test_entry_limit_counts_fills_and_prevents_later_reentry():
    data,feats,config=fixture()
    f=feats['SOXL'];f.loc[f.index[12],['score','entry_window','breakout']]=[5.,True,True]
    a=simulate(data,feats,config,['2026-07-13'],max_holding_bars=3,max_daily_entries=1)
    b=simulate(data,feats,config,['2026-07-13'],max_holding_bars=3,max_daily_entries=2)
    assert a['metrics']['trades']==1
    assert b['metrics']['trades']==2
    assert a['trades'][0]['entry_time']==b['trades'][0]['entry_time']
