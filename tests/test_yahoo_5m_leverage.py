from tests.test_yahoo_5m_morning_execution_audit import setup
from scripts.yahoo_5m_morning_execution_audit import reconstruct


def test_leverage_interest_and_loss_are_in_cash():
    d,f=setup();d['SOXL'].loc[d['SOXL'].index[6],['open','low','close']]=97.
    r=reconstruct(d,f,['2026-07-13'],fee_bps=0,slip_bps=0,size_multiplier=2,financing_apr=.12)
    t=r['trades'][0]
    assert t['borrowed']>0 and t['financing']>0
    assert abs(t['financing']-t['borrowed']*.12/365)<1e-9
    assert abs(t['pnl']-(-3*t['qty']-4-t['financing']))<1e-8


def test_margin_barrier_and_initial_margin_rejection():
    d,f=setup();d['SOXL'].loc[d['SOXL'].index[6],'low']=98.
    r=reconstruct(d,f,['2026-07-13'],fee_bps=0,slip_bps=0,size_multiplier=3,maintenance_margin=.34)
    assert r['trades'][0]['reason']=='margin_liquidation'
    rejected=reconstruct(d,f,['2026-07-13'],size_multiplier=3,maintenance_margin=.5)
    assert not rejected['trades']
