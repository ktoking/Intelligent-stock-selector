import numpy as np
import pandas as pd
import pytest

from scripts.yahoo_5m_factor_lab import features, closed_15m_trend, stop_fill, simulate, load_data


def synthetic():
    index = pd.DatetimeIndex([t for d in pd.bdate_range('2026-07-13',periods=6)
        for t in pd.date_range(str(d.date())+' 13:30',periods=78,freq='5min',tz='UTC')])
    rng=np.random.default_rng(13)
    out={}
    for symbol in ('SOXL','TQQQ','SMH','QQQ'):
        c=100*np.exp(np.cumsum(rng.normal(.0001,.002,len(index))))
        out[symbol]=pd.DataFrame({'open':c,'high':c*1.003,'low':c*.997,'close':c,
            'volume':rng.uniform(100,1000,len(index))},index=index)
    return out


def test_future_candles_do_not_change_prior_factor_values():
    data=synthetic();altered={s:f.copy() for s,f in data.items()}
    for f in altered.values():f.iloc[400:,:4]*=5;f.iloc[400:,4]*=100
    a,b=features(data),features(altered)
    for s in a:pd.testing.assert_frame_equal(a[s].iloc[:400],b[s].iloc[:400])


def test_incomplete_15minute_candle_is_not_available():
    f=synthetic()['SOXL'].iloc[:6].copy()
    f['close']=[100,100,100,100,100,200]
    trend=closed_15m_trend(f)
    assert trend.iloc[:5].eq(0).all()
    assert trend.iloc[5]==1


def test_stop_gap_and_ambiguous_bar_are_conservative():
    assert stop_fill(100,110,90,95,105)==(95,'stop_first')
    assert stop_fill(90,110,89,95,105)==(90,'gap_stop')
    assert stop_fill(110,111,106,95,105)==(105,'target')


def test_missing_minute_bar_fails_instead_of_becoming_zero_return(tmp_path):
    data=synthetic();data['SOXL']=data['SOXL'].drop(data['SOXL'].index[100])
    p=tmp_path/'bars.pkl';pd.to_pickle(data,p)
    with pytest.raises(ValueError,match='missing/extra'):load_data(p)


def test_next_bar_entry_no_leverage_and_session_liquidation():
    data=synthetic()
    for f in data.values():f.loc[:,['open','high','low','close']]=100.
    feats={}
    for s in ('SOXL','TQQQ'):
        f=pd.DataFrame({'score':0.,'entry_window':False,'stop_fraction':.01,
            'breakout':False,'pullback':False,'previous_score':0.},index=data[s].index)
        f.iloc[4,f.columns.get_loc('score')]=5
        f.iloc[4,f.columns.get_loc('entry_window')]=True
        f.iloc[4,f.columns.get_loc('breakout')]=True
        feats[s]=f
    cfg={'family':'breakout','threshold':3,'risk':.01}
    r=simulate(data,feats,cfg,['2026-07-13'],cost_bps=10,slippage_bps=0)
    assert len(r['trades'])==1  # Both symbols signaled; capital cannot be spent twice.
    assert r['trades'][0]['entry_time']==str(data['SOXL'].index[5])
    assert r['trades'][0]['reason']=='session_close'
    assert r['daily'][0]['return']<0
    assert r['daily'][0]['cash']==r['daily'][0]['equity']
    assert all(b['cash']>=0 for b in r['bars'])


def test_zero_trade_days_are_in_daily_denominator():
    data=synthetic();feats=features(data)
    cfg={'family':'breakout','threshold':9,'risk':.01}
    r=simulate(data,feats,cfg,['2026-07-13','2026-07-14'])
    assert r['metrics']['sessions']==2
    assert r['metrics']['return_pct']==0
    assert r['metrics']['trades']==0
