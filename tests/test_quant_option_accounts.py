"""Unbound independent accounts have unknown assets, never a fabricated balance."""
from dataclasses import replace
from unittest.mock import patch
import json

import pytest

from quant_agent.capital import detach_unbound_option_capital,TREASURY
from quant_agent.config import Settings
from quant_agent.store import Store
from quant_agent.web import public_status
from tests.test_quant_champion import configured


def test_unused_shared_reserve_returns_to_stock_and_is_not_profit(tmp_path):
    s,old,new,b=configured(tmp_path)
    cash=s.portfolio()['cash'];amount=s.portfolio('option')['cash'];orders=s.orders();specs=[r['spec'] for r in s.strategies()]
    detach_unbound_option_capital(s,'offline user requests independent US option account')
    assert s.settings.stock_budget==1000000 and s.settings.option_budget==0
    assert not s.settings.option_paper_enabled and s.settings.option_funding_mode=='independent_pending'
    assert s.portfolio()['cash']==pytest.approx(cash+amount) and s.portfolio('option')['cash']==0
    assert sum(s.strategy_portfolio(i)['cash'] for i in s.meta('paper_allocations')['stock'])==pytest.approx(s.portfolio()['cash'])
    assert sum(s.strategy_portfolio(i,'option')['cash'] for i in s.meta('paper_allocations')['option'])==0
    assert sum(s.meta('paper_allocations')['stock'].values())==1000000
    assert s.orders()==orders and [r['spec'] for r in s.strategies()]==specs
    assert s.meta('capital_flows')[-1]['amount']==1000
    restored=Store(s.home,Settings.load(s.home));assert restored.settings.option_funding_mode=='independent_pending'
    cash=s.portfolio()['cash'];detach_unbound_option_capital(s,'offline repeated request');assert s.portfolio()['cash']==cash


def test_independent_account_requires_identity_before_any_option_buy(tmp_path):
    s,old,new,b=configured(tmp_path);detach_unbound_option_capital(s,'offline')
    owner=s.meta('option_paper_strategy')['id']
    with pytest.raises(ValueError):s.reserve('blocked-option',owner,'option','US.NVDA261030C220000','BUY',1,1,'2026-10-15',100)
    with pytest.raises(ValueError):replace(s.settings,option_paper_enabled=True)
    assert not s.meta('option_account_status')['balance_verified']


def test_funds_cannot_detach_when_option_history_exists(tmp_path):
    s,old,new,b=configured(tmp_path);owner=s.meta('option_paper_strategy')['id']
    s.reserve('existing',owner,'option','US.NVDA261030C220000','BUY',1,1,'2026-10-15',100)
    with pytest.raises(ValueError,match='history'):detach_unbound_option_capital(s,'offline')
    assert s.settings.option_budget==1000 and s.portfolio('option')['cash']==1000


def test_capital_settings_recovery_includes_option_disabled_flag(tmp_path):
    s,old,new,b=configured(tmp_path);previous=json.loads((s.home/'config.json').read_text())
    detach_unbound_option_capital(s,'offline');(s.home/'config.json').write_text(json.dumps(previous))
    settings=Settings.load(s.home)
    assert settings.stock_budget==1000000 and settings.option_budget==0
    assert settings.option_funding_mode=='independent_pending' and not settings.option_paper_enabled


def test_pending_account_public_balance_is_unknown_not_one_thousand(tmp_path):
    s,old,new,b=configured(tmp_path);detach_unbound_option_capital(s,'offline')
    # No forecast/network requirement: inspect the normal API representation.
    value=public_status(s)
    assert value['paper_activity']['option']['nav'] is None
    assert value['paper_activity']['option']['budget'] is None
    assert value['option_account_status']['status']=='awaiting_binding'
    assert '待绑定' in value['options_status']
    assert value['holdings']['option']['nav'] is None and value['holdings']['option']['available'] is False
