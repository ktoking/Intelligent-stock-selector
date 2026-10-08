import pandas as pd

from scripts.yahoo_5m_factor_lab import simulate
from tests.test_yahoo_5m_factor_lab import synthetic


def fixture():
    data=synthetic()
    for f in data.values():f.loc[:,['open','high','low','close']]=100.
    feats={}
    for s in ('SOXL','TQQQ'):
        f=pd.DataFrame({'score':0.,'entry_window':False,'stop_fraction':.01,
            'breakout':False,'previous_score':0.},index=data[s].index)
        if s=='SOXL':f.loc[f.index[4],['score','entry_window','breakout']]=[5.,True,True]
        feats[s]=f
    return data,feats,{'family':'breakout','threshold':3,'risk':.01}


def test_holding_limit_exits_at_scheduled_open_not_previous_close():
    d,f,c=fixture();d['SOXL'].loc[d['SOXL'].index[8],['open','low','close']]=99.5
    r=simulate(d,f,c,['2026-07-13'],max_holding_bars=3,reward_multiple=None,slippage_bps=0,cost_bps=0)
    t=r['trades'][0]
    assert t['entry_time']==str(d['SOXL'].index[5])
    assert t['exit_time']==str(d['SOXL'].index[8])
    assert t['exit_price']==99.5
    assert t['reason']=='holding_limit_next_open'


def test_disabling_profit_target_keeps_stop_active():
    d,f,c=fixture()
    d['SOXL'].loc[d['SOXL'].index[6],'high']=103
    d['SOXL'].loc[d['SOXL'].index[7],'low']=98
    r=simulate(d,f,c,['2026-07-13'],reward_multiple=None,slippage_bps=0,cost_bps=0)
    t=r['trades'][0]
    assert t['exit_time']==str(d['SOXL'].index[7])
    assert t['exit_price']==99
    assert t['reason']=='stop'
