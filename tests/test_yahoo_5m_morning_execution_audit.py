from tests.test_yahoo_5m_exits import fixture
from scripts.yahoo_5m_morning_execution_audit import reconstruct


def setup():
    data,fs,_=fixture()
    for f in fs.values():f['morning_base']=f.breakout
    return data,fs


def test_independent_gap_stop_and_minimum_fees():
    data,fs=setup();data['SOXL'].loc[data['SOXL'].index[6],['open','low','close']]=97.
    r=reconstruct(data,fs,['2026-07-13'],fee_bps=0,slip_bps=0)
    t=r['trades'][0]
    assert t['entry_index']==5 and t['exit_index']==6
    assert t['reason']=='gap_stop' and t['exit_price']==97
    assert abs(t['pnl']-(-3*t['qty']-4))<1e-8


def test_independent_pause_exits_next_open_and_delay_moves_entry():
    data,fs=setup();data['SOXL'].loc[data['SOXL'].index[6],['high','close']]=104.
    data['SOXL'].loc[data['SOXL'].index[7],['open','high','low','close']]=103.
    r=reconstruct(data,fs,['2026-07-13'],fee_bps=0,slip_bps=0)
    t=r['trades'][0]
    assert t['exit_index']==7 and t['exit_price']==103 and t['reason']=='daily_limit_next_open'
    later=reconstruct(data,fs,['2026-07-13'],fee_bps=0,slip_bps=0,delay=1,whole_shares=True)
    assert later['trades'][0]['entry_index']==6
    assert later['trades'][0]['qty'].is_integer()
