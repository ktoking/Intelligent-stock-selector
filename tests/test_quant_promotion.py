from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch

import pytest

from tests.test_quant_supervisor_mock import rehearsal_store
from quant_agent.promotion import replace_idle_slot,rule_signature
from quant_agent.config import digest
from quant_agent.research_evidence import research_pending,research_summary
from quant_agent.schedule import daily_due
from quant_agent.activity import strategy_activity

NOW='2026-10-13T12:00:00Z'


def slot(tmp_path):
    s=rehearsal_store(tmp_path);old=s.strategies(['paper'])[0]
    spec=deepcopy(old['spec']);spec['policy']['name']='replacement-fixture'
    new=s.propose(spec);e=deepcopy(old['evidence'])
    e['gates']={k:True for k in ['test_sample','test_activity','drawdown','validation_and_test_profit','high_cost_profit','delayed_profit','qqq_excess']}
    e['independent_ledger']={k:{'fixture':True} for k in ['train','validation','retrospective_test']}
    for a,b in [('proposed','backtested'),('backtested','validated'),('validated','shadow')]:s.transition(new,a,b,e)
    s.set_meta('promotion_policy',{'enabled':True,'authorization':'offline fixture','minimum_score_improvement':.1})
    item={'candidate':new,'incumbent':old['id'],'eligible':True,'improvement':1.,'data_sha256':spec['research_data_sha256'],
        'candidate_spec_sha256':digest(spec),'incumbent_spec_sha256':digest(old['spec'])}
    return s,old,new,item


def trade(s,owner,side,qty,price,key):
    with patch('quant_agent.store.utcnow',lambda:'2026-10-09T19:50:00+00:00'):
        s.reserve(key,owner,'stock','US.AAA',side,qty,price,'2026-10-09');s.claim(key);s.submitted(key,'mock-'+key);s.fill(key,qty,price,'filled')


@pytest.mark.parametrize('sell_price',[90,110])
def test_cash_transfer_preserves_realized_pnl_fees_and_budget(tmp_path,sell_price):
    s,old,new,item=slot(tmp_path);ident=old['id'];cap=s.strategy_portfolio(ident)['budget']
    trade(s,ident,'BUY',10,100,'buy');trade(s,ident,'SELL',10,sell_price,'sell')
    before=s.strategy_portfolio(ident);cash=s.portfolio()['cash'];option=s.portfolio('option')['cash']
    assert replace_idle_slot(s,item,now=NOW) is None
    after=s.strategy_portfolio(ident);replacement=s.strategy_portfolio(new)
    assert after['cash']==pytest.approx(0) and after['budget']==0
    assert after['performance_nav']==pytest.approx(before['nav'])
    assert replacement['cash']==pytest.approx(before['cash']) and replacement['initial_capital']==pytest.approx(before['cash'])
    assert replacement['budget']==cap and s.portfolio()['cash']==cash and s.portfolio('option')['cash']==option
    assert sum(s.strategy_portfolio(i)['cash'] for i in s.meta('paper_allocations')['stock'])==pytest.approx(cash)
    assert s.attributed_portfolio()['realized_pnl']==pytest.approx(before['realized_pnl'])
    assert strategy_activity(s,ident,'paper')['summary']['initial_capital']==cap
    assert strategy_activity(s,ident,'paper')['summary']['nav']==pytest.approx(before['nav'])
    assert replace_idle_slot(s,item,now=NOW) is not None


def test_held_positions_and_pending_orders_cannot_be_replaced(tmp_path):
    s,old,new,item=slot(tmp_path);ident=old['id'];trade(s,ident,'BUY',1,100,'buy')
    assert '持仓' in replace_idle_slot(s,item,now=NOW)
    assert next(r for r in s.strategies() if r['id']==new)['stage']=='shadow'
    s.reserve('working',ident,'stock','US.BBB','BUY',1,100,'2026-10-12')
    assert '未完成订单' in replace_idle_slot(s,item,now=NOW)


def test_unsettled_cash_and_uncertain_receipts_block_transfer(tmp_path):
    s,old,new,item=slot(tmp_path)
    s.set_meta('unsettled',[{'bucket':'stock','amount':100,'settles':'2026-10-14T13:30:00+00:00'}])
    assert '交收' in replace_idle_slot(s,item,now=NOW)
    s.set_meta('unsettled',[]);s.reserve('unknown',old['id'],'stock','US.AAA','BUY',1,100,'2026-10-12');s.claim('unknown')
    assert '回执' in replace_idle_slot(s,item,now=NOW)


def test_open_market_version_drift_and_failed_gates_block_promotion(tmp_path):
    s,old,new,item=slot(tmp_path)
    assert '交易时段' in replace_idle_slot(s,item,now='2026-10-13T13:31:00Z')
    assert '版本' in replace_idle_slot(s,{**item,'candidate_spec_sha256':'wrong'},now=NOW)
    with s.transaction() as db:
        import json
        e=json.loads(db.execute('SELECT evidence FROM strategies WHERE id=?',(new,)).fetchone()[0]);e['gates']['high_cost_profit']=False
        db.execute('UPDATE strategies SET evidence=? WHERE id=?',(json.dumps(e),new))
    assert '验证' in replace_idle_slot(s,item,now=NOW)


def test_review_completion_does_not_hide_missing_research_or_recovery(tmp_path):
    s,old,new,item=slot(tmp_path)
    s.settings=replace(s.settings,daily_research=True)
    s.set_meta('daily_completed:2026-10-12',True)
    assert research_pending(s,'2026-10-12')
    assert daily_due(s,NOW)
    summary=research_summary(s,'2026-10-12');assert summary['pending']
    assert next(x for x in summary['steps'] if x['key']=='evolution')['status']=='pending'
    s.set_meta('research_cycle_completed:2026-10-12',s.settings.data_dir)
    assert not research_pending(s,'2026-10-12') and not daily_due(s,NOW)


def test_renaming_a_rule_does_not_make_it_a_distinct_strategy(tmp_path):
    s,old,new,item=slot(tmp_path)
    candidate=next(r for r in s.strategies() if r['id']==new)
    assert rule_signature(candidate)==rule_signature(old)


def test_daily_research_runs_while_execution_paused_and_finishes_missing_steps(tmp_path,monkeypatch):
    from quant_agent import runner,briefing,evolution,promotion
    from research.daily_factor_lab.core import load_panel
    s,old,new,item=slot(tmp_path);s.settings=replace(s.settings,daily_research=True,evolution_enabled=True)
    s.pause('offline execution pause');s.set_meta('daily_completed:2026-10-12',True)
    s.set_meta('universe_scanned:2026-10-12',True);s.set_meta('news_completed:2026-10-12',True)
    p=load_panel(s.settings.data_dir);calls=[]
    packet={'created':'offline','strategies':[],'orders':[],'paused':True,'news':[],'data':p.metadata}
    response={'model':'offline','result':{'review':'offline review','factor_ideas':['offline hypothesis']}}
    monkeypatch.setattr(runner,'latest_completed_session',lambda:'2026-10-12')
    monkeypatch.setattr(runner,'propose',lambda *a,**kw:calls.append('discovery'))
    monkeypatch.setattr(runner,'evaluate',lambda *a:calls.append('backtest'))
    monkeypatch.setattr(runner,'ask',lambda *a,**kw:response)
    monkeypatch.setattr(briefing,'review_packet',lambda *a,**kw:packet)
    monkeypatch.setattr(briefing,'validate_brief',lambda *a:None)
    monkeypatch.setattr(briefing,'persist_brief',lambda *a:None)
    def generate(*a):
        calls.append('factor')
        return {'strategy':new,'name':'offline','hypothesis':'offline'}
    monkeypatch.setattr(evolution,'evolve',generate)
    monkeypatch.setattr(promotion,'plan_promotions',lambda *a:calls.append('promotion'))
    runner.daily_jobs(s,{})
    assert all(k in calls for k in ['discovery','backtest','factor','promotion'])
    assert not research_pending(s,'2026-10-12') and s.meta('daily_completed:2026-10-12')
    assert s.meta('paused') and s.orders()==[]


def test_supervisor_applies_qualified_idle_promotion_then_opens_with_new_owner(tmp_path):
    from scripts.mock_quant_opening import run_case
    s,old,new,item=slot(tmp_path)
    s.set_meta('promotion_queue',[{**item,'status':'ready'}])
    result=run_case(tmp_path,s.settings,'promotion_then_open',http_mock=True)
    assert result['result']=='PASS'
    assert next(r for r in s.strategies() if r['id']==old['id'])['stage']=='retired'
    assert next(r for r in s.strategies() if r['id']==new)['stage']=='paper'
    assert any(o['strategy']==new for o in s.orders()) and not any(o['strategy']==old['id'] for o in s.orders())
