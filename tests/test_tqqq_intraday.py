import numpy as np
import pandas as pd
from research.tqqq_intraday import signals, simulate, load


def test_factors_do_not_read_future():
    data=load()
    full,e=signals(data)
    partial,pe=signals({k:v.iloc[:500] for k,v in data.items()})
    for name in full:
        pd.testing.assert_series_equal(full[name].iloc[:500],partial[name])
    pd.testing.assert_series_equal(e.iloc[:500],pe)


def test_flat_prices_lose_only_costs_and_close_daily():
    index=pd.date_range('2026-09-21 09:30',periods=78,freq='5min',tz='America/New_York')
    f=pd.DataFrame(dict(open=100.,high=100.,low=100.,close=100.,volume=1000.),index=index)
    sig=pd.Series(False,index=index);sig.iloc[5]=True
    stats,trades,daily=simulate({'TQQQ':f},sig,f.close,['2026-09-21'])
    assert len(trades)==1
    assert '10:00' in trades[0]['entry_time']
    assert '15:50' in trades[0]['exit_time']
    assert abs(trades[0]['pnl']+15.8)<1e-7
    assert stats['return_pct']<0


def test_no_signal_no_trade():
    data=load();f=data['TQQQ']
    stats,trades,_=simulate(data,pd.Series(False,index=f.index),f.close,['2026-09-21'])
    assert stats['return_pct']==0 and not trades
