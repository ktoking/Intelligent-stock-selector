"""Offline brokerage fixtures only: capital, liquidation and promotion ordering."""
from dataclasses import asdict
from unittest.mock import patch
import json

import pandas as pd
import pytest

from quant_agent.capital import migrate_account_capital,TREASURY,adjusted_curve,strategy_performance
from quant_agent.champion import queue_rotation,rotation_tick,pending,REQUIRED
from quant_agent.config import Settings,digest
from quant_agent.store import Store
from tests.test_quant_promotion import slot,trade

NOW='2026-10-13T13:31:00+00:00'


class SimFixture:
    verified=True
    def __init__(self,store):self.store=store;self.submits=[];self.cancels=[]
    def state(self):
        return {'cash':{'balance':1000000,'total_asset':1000000},'positions':[
            {'symbol':s[3:],'qty':p['qty'],'pstn_type':0} for s,p in self.store.portfolio()['positions'].items()],
            'orders':[{'order_id':o['broker_id'],'symbol':o['symbol'][3:],'qty':o['qty'],
                'side':1 if o['side']=='BUY' else 2,'status':4,'cum_qty':o['filled'],
                'avg_fill_price':o['fill_value']/o['filled'] if o['filled'] else 0} for o in self.store.orders() if o['broker_id']]}
    def submit(self,order):self.submits.append(order);return 'mock-'+order['id']
    def cancel(self,ident):self.cancels.append(ident)


def configured(tmp_path,held=False):
    s,old,new,item=slot(tmp_path)
    if held:trade(s,old['id'],'BUY',10,100,'held')
    broker=SimFixture(s)
    migrate_account_capital(s,broker,1000000,'offline test authorization')
    s.set_meta('promotion_policy',{'enabled':True,'mode':'account_champion','authorization':'offline'})
    result={'champion':new,'data_sha256':item['data_sha256']}
    queue_rotation(s,result,'offline artifact')
    return s,old,new,broker


def quote():return {'last_price':100,'bid_price':99.99,'ask_price':100.01,'update_time':int(pd.Timestamp(NOW).timestamp()*1000),'data_date':'2026-10-13'}


def test_expansion_preserves_fills_and_records_funding_once(tmp_path):
    s,old,new,b=configured(tmp_path,True)
    assert s.settings.stock_budget==999000 and s.settings.option_budget==1000
    assert len(s.orders())==1 and s.portfolio()['positions']['US.AAA']['qty']==10
    assert s.strategy_portfolio(TREASURY)['cash']==989000
    assert sum(s.strategy_portfolio(i)['cash'] for i in s.meta('paper_allocations')['stock'])==pytest.approx(s.portfolio()['cash'])
    prior=s.portfolio()['cash'];migrate_account_capital(s,b,1000000,'offline retry')
    assert s.portfolio()['cash']==prior
    restored=Store(s.home,Settings.load(s.home));assert restored.settings.stock_budget==999000


def test_capital_migration_recovers_config_after_database_commit(tmp_path):
    s,old,new,b=configured(tmp_path)
    config=s.home/'config.json';v=json.loads(config.read_text());v['stock_budget']=10000;v['capital_mode']='fixed';config.write_text(json.dumps(v))
    assert Settings.load(s.home).stock_budget==999000
    assert json.loads(config.read_text())['capital_mode']=='broker_equity'


def test_unaffordable_expansion_does_not_change_budget(tmp_path):
    s,old,new,item=slot(tmp_path);b=SimFixture(s)
    original=b.state;b.state=lambda:{**original(),'cash':{'balance':5000,'total_asset':5000}}
    with pytest.raises(ValueError,match='cannot fund'):migrate_account_capital(s,b,1000000,'offline')
    assert s.settings.stock_budget==10000 and s.meta('capital_flows') is None


def test_pending_rotation_and_unpromoted_factor_cannot_buy(tmp_path):
    s,old,new,b=configured(tmp_path)
    with pytest.raises(ValueError,match='champion'):s.reserve('blocked',old['id'],'stock','US.AAA','BUY',1,100,'2026-10-13')
    with pytest.raises(ValueError):s.reserve('factor',new,'stock','US.AAA','BUY',1,100,'2026-10-13')
    assert not b.submits and s.orders()==[]


def test_liquidation_partial_cancel_restart_and_settlement_before_promotion(tmp_path):
    s,old,new,b=configured(tmp_path,True)
    with patch('quant_agent.champion.utcnow',lambda:NOW),patch('quant_agent.store.utcnow',lambda:NOW),patch('quant_agent.broker.session_now',lambda now=None:'2026-10-13'),patch('quant_agent.broker.executable_quote',return_value=(99.99,100.01)),patch('quant_agent.champion.executable_quote',return_value=(99.99,100.01)):
        assert rotation_tick(s,b,{'US.AAA':quote()},'2026-10-13')
        assert len(b.submits)==1 and b.submits[0]['side']=='SELL'
        order=s.orders()[-1];s.fill(order['id'],4,99.99,'partial')
        # Recreate Store: the pending intent must prevent duplicate sells.
        s=Store(s.home,s.settings);assert rotation_tick(s,b,{'US.AAA':quote()},'2026-10-13')
        assert len(b.submits)==1
        # Cancelled after a partial fill; only the remaining six may be sold.
        s.fill(order['id'],4,99.99,'cancelled')
        rotation_tick(s,b,{'US.AAA':quote()},'2026-10-13')
        assert len(b.submits)==2 and b.submits[-1]['qty']==6
        last=b.submits[-1];s.fill(last['id'],6,99.99,'filled')
        assert rotation_tick(s,b,{'US.AAA':quote()},'2026-10-13')
        assert pending(s)['status']=='settling' and next(r for r in s.strategies() if r['id']==new)['stage']=='shadow'
        with pytest.raises(ValueError):s.reserve('early',new,'stock','US.AAA','BUY',1,100,'2026-10-13')
    with patch('quant_agent.champion.utcnow',lambda:'2026-10-14T13:31:00+00:00'):
        assert not rotation_tick(s,b,{},'2026-10-14')
    assert s.meta('account_champion')==new and s.meta('champion_rotation')['status']=='completed'
    assert all(not s.strategy_portfolio(i)['positions'] for i in s.meta('champion_rotation')['outgoing'])
    assert s.strategy_portfolio(new)['budget']==999000
    assert len(s.orders())==3 and s.strategy_portfolio(old['id'])['realized_pnl']<0
    assert sum(s.strategy_portfolio(i)['cash'] for i in s.meta('paper_allocations')['stock'])==pytest.approx(s.portfolio()['cash'])
    assert not rotation_tick(s,b,{},'2026-10-14')


def test_cancel_ack_waits_for_readback_and_books_late_fill(tmp_path):
    s,old,new,b=configured(tmp_path)
    # A normal working buy predating the new policy must be cancelled first.
    with s.transaction() as db:
        db.execute("INSERT INTO orders(id,strategy,bucket,symbol,side,qty,price,multiplier,fee,status,broker_id,created,session) VALUES (?,?, 'stock','US.AAA','BUY',1,100,1,.3,'submitted','mock-late',?,'2026-10-13')",('late',old['id'],NOW))
    assert rotation_tick(s,b,{},'2026-10-13') and b.cancels==['mock-late']
    assert not b.submits and s.orders()[-1]['status']=='submitted'
    s.fill('late',1,100,'filled')
    with patch('quant_agent.champion.executable_quote',return_value=(99.99,100.01)):
        rotation_tick(s,b,{'US.AAA':quote()},'2026-10-13')
    assert len(b.submits)==1 and b.submits[0]['side']=='SELL' and b.submits[0]['qty']==1


def test_stale_quote_and_off_session_never_send_liquidation(tmp_path):
    s,old,new,b=configured(tmp_path,True)
    assert rotation_tick(s,b,{},None) and not b.submits
    with patch('quant_agent.champion.executable_quote',side_effect=__import__('quant_agent.broker',fromlist=['QuoteUnavailable']).QuoteUnavailable('stale')):
        assert rotation_tick(s,b,{'US.AAA':quote()},'2026-10-13') and not b.submits
    assert pending(s)['status']=='liquidating'


def test_unknown_liquidation_stops_without_blind_resubmission(tmp_path):
    s,old,new,b=configured(tmp_path,True)
    def fail(order):b.submits.append(order);raise TimeoutError()
    b.submit=fail
    with patch('quant_agent.champion.executable_quote',return_value=(99.99,100.01)):
        with pytest.raises(TimeoutError):rotation_tick(s,b,{'US.AAA':quote()},'2026-10-13')
    assert s.meta('paused') and s.orders()[-1]['status']=='unknown'
    rotation_tick(s,b,{},'2026-10-13');assert len(b.submits)==1


def test_deposit_and_withdrawal_do_not_create_investment_returns():
    rows=[{'observed':'2026-10-12','nav':9900},{'observed':'2026-10-14','nav':998900},{'observed':'2026-10-16','nav':0}]
    flows=[{'observed':'2026-10-13','amount':989000},{'observed':'2026-10-15','amount':-998900}]
    curve=adjusted_curve(rows,flows,10000)
    assert [r['return_pct'] for r in curve]==pytest.approx([-1,-1,-1])


def test_empty_account_rotation_moves_cash_once_without_orders(tmp_path):
    s,old,new,b=configured(tmp_path)
    cash=s.portfolio()['cash'];option=s.portfolio('option')['cash']
    assert not rotation_tick(s,b,{},'2026-10-13')
    assert s.strategy_portfolio(new)['cash']==pytest.approx(cash)
    assert s.portfolio('option')['cash']==option and not b.submits and not b.cancels
    assert sum(v for v in s.meta('paper_allocations')['stock'].values())==999000
    ret,curve=strategy_performance(s,new,s.strategy_portfolio(new));assert ret==pytest.approx(0)


@pytest.mark.parametrize('case',['normal','partial_fill_restart'])
def test_full_supervisor_executes_account_scale_only_after_promotion(tmp_path,case):
    from scripts.mock_quant_opening import run_case
    s,old,new,b=configured(tmp_path)
    rotation_tick(s,b,{},'2026-10-13')
    result=run_case(s.home,s.settings,case,http_mock=True)
    assert result['result']=='PASS' and not result['paused']
    stock=[o for o in s.orders() if o['bucket']=='stock']
    assert stock and {o['strategy'] for o in stock}=={new}
    assert sum(o['qty']*o['price'] for o in stock if o['side']=='BUY')>10000
    assert s.portfolio()['cash']>=0


def test_no_qualified_strategy_holds_cash_with_no_buy_permission(tmp_path):
    from quant_agent.champion import CASH
    s,old,new,b=configured(tmp_path)
    queue_rotation(s,{'champion':None,'data_sha256':'offline'},'offline cash decision')
    assert pending(s)['target']==CASH
    assert not rotation_tick(s,b,{},'2026-10-13')
    assert s.meta('account_champion') is None and not s.strategies(['paper'])
    assert s.strategy_portfolio(TREASURY)['cash']==pytest.approx(s.portfolio()['cash'])
    with pytest.raises(ValueError):s.reserve('no-winner',old['id'],'stock','US.AAA','BUY',1,100,'2026-10-13')
    assert not b.submits


def test_retained_winner_keeps_own_holdings_and_cooldown_state(tmp_path):
    s,old,new,b=configured(tmp_path,True)
    candidate=next(r for r in s.strategies() if r['id']==new)
    with s.transaction() as db:
        evidence={**old['evidence'],'gates':{k:True for k in REQUIRED},'independent_ledger':candidate['evidence']['independent_ledger']}
        db.execute('UPDATE strategies SET evidence=? WHERE id=?',(json.dumps(evidence),old['id']))
    controls={'sessions':7,'peaks':{'US.AAA':110},'blocked':{'US.BBB':9}}
    s.set_meta('paper_controls:'+old['id'],controls)
    queue_rotation(s,{'champion':old['id'],'data_sha256':'offline'},'offline retained winner')
    assert not rotation_tick(s,b,{},'2026-10-13')
    own=s.strategy_portfolio(old['id'])
    assert own['positions']['US.AAA']['qty']==10 and own['budget']==999000
    assert s.meta('paper_controls:'+old['id'])==controls and s.meta('paper_rebalance_required:'+old['id'])
    assert not b.submits and len(s.orders())==1


def test_rotation_records_post_funding_nav_when_tick_shares_timestamp(tmp_path):
    from quant_agent.capital import strategy_performance
    from quant_agent.web import public_status
    s,old,new,b=configured(tmp_path)
    stamp='2026-10-16T13:30:10+00:00'
    before={i:s.strategy_portfolio(i) for i in s.meta('paper_allocations')['stock']}
    with s.transaction() as db:
        for i,owner in before.items():
            db.execute('INSERT INTO strategy_paper_nav VALUES (?,?,?,?,?,?,?,?)',
                (i,stamp,'2026-10-16',owner['nav'],owner['cash'],owner['unrealized_pnl'],owner['realized_pnl'],owner['budget']))
    with patch('quant_agent.champion.utcnow',lambda:stamp),patch('quant_agent.capital.utcnow',lambda:'2026-10-16T13:31:10+00:00'):
        assert not rotation_tick(s,b,{},'2026-10-16')
        for i in before:
            owner=s.strategy_portfolio(i)
            result,_=strategy_performance(s,i,owner)
            assert result==pytest.approx(0)
        assert public_status(s)['paused'] is False
        assert strategy_performance(s,new,s.strategy_portfolio(new))[0]==pytest.approx(0)
    with s.transaction() as db:
        assert db.execute('SELECT nav FROM strategy_paper_nav WHERE strategy=? AND observed=?',(old['id'],stamp)).fetchone()[0]==0


def test_rotation_resumes_reserved_intent_after_process_stops_before_claim(tmp_path,monkeypatch):
    s,old,new,b=configured(tmp_path,True);claim=s.claim
    def interrupted(_):raise SystemExit(23)
    monkeypatch.setattr(s,'claim',interrupted)
    with patch('quant_agent.champion.executable_quote',return_value=(99.99,100.01)):
        with pytest.raises(SystemExit):rotation_tick(s,b,{'US.AAA':quote()},'2026-10-13')
    pending_order=s.orders(active=True)[0]
    assert pending_order['status']=='reserved' and not b.submits
    restored=Store(s.home,s.settings)
    with patch('quant_agent.champion.executable_quote',return_value=(99.99,100.01)):
        rotation_tick(restored,b,{'US.AAA':quote()},'2026-10-13')
    assert len(b.submits)==1 and restored.orders(active=True)[0]['id']==pending_order['id']
