import pytest

from scripts.yahoo_5m_factor_lab import simulate
from tests.test_yahoo_5m_exits import fixture


def test_minimum_commission_is_reserved_and_charged_on_both_sides():
    data,feats,cfg=fixture()
    result=simulate(data,feats,cfg,['2026-07-13'],initial_equity=100, cost_bps=0,
        slippage_bps=0,minimum_fee_usd=2)
    assert result['metrics']['trades']==1
    assert result['daily'][-1]['equity']==pytest.approx(96)
    assert result['trades'][0]['entry_cost']==2
    assert result['trades'][0]['exit_cost']==2
    assert all(b['cash']>=0 for b in result['bars'])


def test_unaffordable_minimum_fee_prevents_zero_quantity_orders():
    data,feats,cfg=fixture()
    result=simulate(data,feats,cfg,['2026-07-13'],initial_equity=1,minimum_fee_usd=2)
    assert result['metrics']['trades']==0
    assert result['daily'][-1]['equity']==1
