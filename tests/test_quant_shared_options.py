"""Shared simulated account: 90/10 capital, versioned authorization, no orders on migration."""
from dataclasses import replace
from unittest.mock import patch
import json

import pytest

from quant_agent.capital import detach_unbound_option_capital,enable_shared_option_ratio,update_broker_capital,stock_cap,option_cap
from quant_agent.config import Settings,digest
from quant_agent.store import Store
from quant_agent.web import public_status
from tests.test_quant_champion import configured,SimFixture


class CapacityFixture(SimFixture):
    def __init__(self,store,settings):super().__init__(store);self.settings=settings
    def snapshots(self,codes):return [{'code':c,'option_valid':True,'option_contract_multiplier':100,'last_price':6.2,'ask_price':6.25,'data_date':'2026-10-14'} for c in codes]
    def option_capacity(self,code,price):return {'max_cash_buy_qty_round_lot':'1000','max_sell_qty_round_lot':'0'}


def shared(tmp_path,detach=True):
    s,old,new,b=configured(tmp_path)
    if detach:detach_unbound_option_capital(s,'offline independent request')
    s.set_meta('option_capability',{'probe_contract':'US.NVDA261030C220000'})
    settings=replace(s.settings,stock_budget=900000.,option_budget=100000.,option_funding_mode='shared_ratio',option_equity_fraction=.1,option_paper_enabled=True)
    b=CapacityFixture(s,settings);enable_shared_option_ratio(s,b,'offline 90/10 shared account authorization')
    return s,b


@pytest.mark.parametrize('detach',[True,False])
def test_shared_allocation_preserves_total_cash_orders_and_stock_versions(tmp_path,detach):
    s,b=shared(tmp_path,detach)
    assert s.settings.stock_budget==900000 and s.settings.option_budget==100000
    assert s.portfolio()['cash']+s.portfolio('option')['cash']==1000000
    assert s.portfolio('option')['cash']==100000
    assert sum(s.meta('paper_allocations')['stock'].values())==900000
    assert sum(s.meta('paper_allocations')['option'].values())==100000
    for bucket in ['stock','option']:
        assert sum(s.strategy_portfolio(i,bucket)['cash'] for i in s.meta('paper_allocations')[bucket])==pytest.approx(s.portfolio(bucket)['cash'])
    assert s.meta('option_account_status')['status']=='shared_verified'
    assert s.meta('option_paper_archive') and not s.orders() and not b.submits
    assert Store(s.home,Settings.load(s.home)).settings.option_equity_fraction==.1
    prior=s.portfolio()['cash'];enable_shared_option_ratio(s,b,'offline retry');assert s.portfolio()['cash']==prior


def test_ten_percent_cap_tracks_equity_and_cash_without_margin(tmp_path):
    s,b=shared(tmp_path);update_broker_capital(s,{'cash':{'balance':700000,'total_asset':800000,'max_power_long':1600000}})
    assert option_cap(s)==pytest.approx(80000)
    assert stock_cap(s)==pytest.approx(700000) # option book is 100k, so total stays within equity
    owner=s.meta('option_paper_strategy')['id']
    with pytest.raises(ValueError):s.reserve('too-large',owner,'option','US.NVDA261030C220000','BUY',1,801,'2026-10-15',100)
    update_broker_capital(s,{'cash':{'balance':1000000,'total_asset':1000000}})
    assert s.reserve('premium-over-old-cap',owner,'option','US.NVDA261030C220000','BUY',1,20,'2026-10-15',100)
    assert s.portfolio('option')['cash']==100000  # reservation is not a fill


def test_global_broker_cash_gate_blocks_other_bucket_double_spending(tmp_path):
    s,b=shared(tmp_path)
    s.set_meta('broker_capital',{'observed_at':__import__('quant_agent.store',fromlist=['utcnow']).utcnow(),
        'cash':1500,'option_equity_cap':100000,'option_cash_cap':100000,'stock_equity_cap':900000,'stock_cash_cap':900000})
    owner=s.meta('option_paper_strategy')['id']
    with s.transaction() as db:
        db.execute("INSERT INTO orders(id,strategy,bucket,symbol,side,qty,price,multiplier,fee,status,created,session) VALUES ('other','stock-fixture','stock','US.AAA','BUY',10,100,1,3,'submitted','2026-10-15','2026-10-15')")
    with pytest.raises(ValueError,match='cash/capital'):s.reserve('over-shared-cash',owner,'option','US.NVDA261030C220000','BUY',1,6.25,'2026-10-15',100)


def test_expired_broker_observation_cannot_authorize_option_buy(tmp_path):
    s,b=shared(tmp_path);owner=s.meta('option_paper_strategy')['id']
    value=s.meta('broker_capital');value['observed_at']='2020-01-01T00:00:00+00:00';s.set_meta('broker_capital',value)
    assert option_cap(s)==0
    with pytest.raises(ValueError):s.reserve('stale',owner,'option','US.NVDA261030C220000','BUY',1,1,'2026-10-15',100)


def test_option_funding_is_not_return_and_is_visible_in_api(tmp_path):
    s,b=shared(tmp_path)
    with s.transaction() as db:
        db.execute("INSERT INTO option_paper_nav VALUES ('2026-10-14','2026-10-14',1000,1000,0,0)")
        db.execute("INSERT INTO option_paper_nav VALUES ('2099-01-01','2099-01-01',100000,100000,0,0)")
    a=s.option_activity();assert a['return_pct']==pytest.approx(0)
    assert a['curve'][-1]['performance_equity']==pytest.approx(1000)
    status=public_status(s)
    assert status['paper_activity']['option']['nav']==100000 and status['option_equity_fraction']==.1
    assert '10%' in status['options_status'] and status['option_account_status']['authorized']


def test_recovery_restores_shared_ratio_settings(tmp_path):
    s,b=shared(tmp_path);path=s.home/'config.json';v=json.loads(path.read_text())
    v.update(stock_budget=1000000,option_budget=0,option_paper_enabled=False,option_funding_mode='independent_pending',option_equity_fraction=0)
    path.write_text(json.dumps(v));loaded=Settings.load(s.home)
    assert loaded.option_paper_enabled and loaded.option_equity_fraction==.1


def test_full_supervisor_can_execute_option_under_shared_ten_percent_mandate(tmp_path):
    from scripts.mock_quant_opening import run_case
    from quant_agent.champion import rotation_tick
    s,b=shared(tmp_path);rotation_tick(s,b,{},'2026-10-13')
    r=run_case(s.home,s.settings,'option_eligible',http_mock=True)
    assert r['result']=='PASS' and r['option_orders']==1
    assert not s.meta('paused') and s.portfolio('option')['cash']<100000


def test_shared_account_daily_risk_includes_option_losses(tmp_path):
    from quant_agent.runner import mark_portfolio
    s,b=shared(tmp_path);owner=s.meta('option_paper_strategy')['id'];code='US.NVDA261030C220000'
    s.reserve('option-loss',owner,'option',code,'BUY',1,900,'2026-10-15',100)
    s.claim('option-loss');s.submitted('option-loss','mock-loss');s.fill('option-loss',1,900,'filled')
    risk=mark_portfolio(s,{code:{'last_price':630}},b.state())
    assert risk['account_daily_loss_pct']>2 and risk['daily_loss_pct']==pytest.approx(0)
    assert s.meta('paused') and 'Shared-account risk breach' in s.meta('pause_reason')
