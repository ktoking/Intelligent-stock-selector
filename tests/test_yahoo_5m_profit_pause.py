import pytest
from tests.test_yahoo_5m_exits import fixture
from scripts.yahoo_5m_factor_lab import simulate


def test_profit_pause_optional_but_loss_protection_retained():
    d,f,c=fixture();raw=d['SOXL'];raw.loc[raw.index[6],['high','close']]=104.
    capped=simulate(d,f,c,['2026-07-13'],reward_multiple=None)
    uncapped=simulate(d,f,c,['2026-07-13'],reward_multiple=None,daily_profit_pause=None)
    assert capped['trades'][0]['reason']=='daily_limit_next_open'
    assert uncapped['trades'][0]['reason']=='session_close'
    raw.loc[raw.index[7],'low']=98
    assert simulate(d,f,c,['2026-07-13'],reward_multiple=None,daily_profit_pause=None)['trades'][0]['reason']=='stop'
    with pytest.raises(ValueError):simulate(d,f,c,['2026-07-13'],daily_profit_pause=-1)
