"""Continuous simulation, definite rejection recovery and real causal factor replay."""
from copy import deepcopy
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from quant_agent import broker, intraday, minute_factors as factors, simulation_policy as policy
from quant_agent.config import initialize
from quant_agent.store import Store
from tests.test_quant_agent import paper
from tests.test_quant_intraday import session_rows


def store_at(tmp_path):
    return Store(tmp_path,initialize(tmp_path,paper_enabled=True,stock_account='offline-sim',continuous_paper=True,
        stock_market_fallback=True,quote_push_enabled=False,evolution_enabled=True))


def factor():
    return {'name':'relative_strength_test','hypothesis':'观察相对QQQ强势','falsification':'成本或延迟失败则不支持',
        'base_family':'relative_trend','expression':{'nodes':[{'op':'field','field':'relative_return6','a':None,'b':None,'x':None,'window':None}],'root':0}}


def dataset():
    from quant_agent.intraday_data import normalize
    bars={s:[] for s in ['US.QQQ','US.AAA','US.BBB']}
    for day in pd.bdate_range(end='2026-10-19',periods=24):
        for j,s in enumerate(bars):
            rows=normalize(session_rows(str(day.date())),now='2026-10-20T00:00Z')
            for i,r in enumerate(rows):
                for k in ['open','high','low','close']:r[k]*=1+j*.0001*i
            bars[s].extend(rows)
    return {'bars':bars}


def test_continuous_risk_warns_and_only_resumes_reconciled_drawdown(tmp_path):
    s=store_at(tmp_path);s.pause('Shared-account risk breach: drawdown=2.02%, daily_loss=2.02%',category='risk')
    before=s.portfolio();assert policy.resume_drawdown_pause(s,{'cash':{'balance':100}})
    policy.risk_breach(s,'Risk breach: drawdown=20%, daily_loss=5%')
    assert not s.meta('paused') and s.portfolio()==before
    s.pause('Operator pause',category='manual')
    assert not policy.resume_drawdown_pause(s,{'cash':{'balance':100}})
    policy.risk_breach(s,'Risk breach: drawdown=20%, daily_loss=5%')
    assert s.meta('paused') and s.meta('pause_reason')=='Operator pause'


def test_market_fallback_only_stocks_regular_session_and_estimated_reserve(tmp_path,monkeypatch):
    s=store_at(tmp_path)
    monkeypatch.setattr(policy,'session_now',lambda:'2026-10-19')
    price,kind=policy.stock_price(s,'US.AAA',{},'BUY',100)
    assert price==110 and kind==3
    price,kind=policy.stock_price(s,'US.AAA',{},'SELL',100)
    assert price==100 and kind==3
    with pytest.raises(broker.QuoteUnavailable):policy.stock_price(s,'US.AAA',{},'BUY')
    monkeypatch.setattr(policy,'session_now',lambda:None)
    with pytest.raises(broker.QuoteUnavailable):policy.stock_price(s,'US.AAA',{},'SELL',100)


def test_market_payload_cash_capacity_and_fill_not_treated_as_limit(tmp_path,monkeypatch):
    s=store_at(tmp_path);owner=paper(s);requests=[]
    def transport(method,path,query,body):
        requests.append((method,path,query,body))
        return {'ret_code':0,'data':{'max_cash_buy_qty_round_lot':'2'} if method=='GET' else {'order_id':'sim-market-1'}}
    b=broker.FutuSim(s.settings,transport=transport);b.verified=True
    monkeypatch.setattr(broker,'session_now',lambda *args:'2026-10-19')
    s.reserve('m',owner,'stock','US.AAA','BUY',1,110,'2026-10-19',order_type=3);assert s.claim('m')
    s.submitted('m',b.submit(s.orders()[0]))
    assert requests[0][2]['order_type']==3
    assert requests[1][3]['order_type']==3 and 'price' not in requests[1][3]
    s.fill('m',1,111,'filled')
    assert s.portfolio()['positions']['US.AAA']['qty']==1
    with pytest.raises(ValueError):s.reserve('o',owner,'option','US.OPT','BUY',1,1,'2026-10-19',100,order_type=3)


def test_capacity_read_failure_before_post_does_not_create_unknown_pause(tmp_path,monkeypatch):
    s=store_at(tmp_path);owner=paper(s);posts=[]
    def transport(method,path,query,body):
        if method=='GET':raise broker.ReadUnavailable('Controlled cash query outage')
        posts.append(body);raise AssertionError('Must not post')
    b=broker.FutuSim(s.settings,transport=transport);b.verified=True
    monkeypatch.setattr(broker,'session_now',lambda *args:'2026-10-19')
    s.reserve('m',owner,'stock','US.AAA','BUY',1,110,'2026-10-19',order_type=3);s.claim('m')
    with pytest.raises(broker.ReadUnavailable) as exc:b.submit(s.orders()[0])
    s.submission_failed('m',exc.value)
    assert s.orders()[0]['status']=='rejected' and not s.meta('paused') and not posts


def test_definite_broker_rejection_does_not_become_lost_ack(tmp_path,monkeypatch):
    s=store_at(tmp_path);owner=paper(s)
    b=broker.FutuSim(s.settings,transport=lambda *args:{'ret_code':-1,'ret_msg':'offline rejected'})
    b.verified=True;s.reserve('reject',owner,'stock','US.AAA','BUY',1,100,'2026-10-19');s.claim('reject')
    with pytest.raises(broker.OrderRejected) as exc:b.submit(s.orders()[0])
    s.submission_failed('reject',exc.value)
    assert not s.meta('paused') and s.orders()[0]['status']=='rejected'


def test_legacy_frozen_caller_cannot_pause_after_definite_rejection(tmp_path):
    s=store_at(tmp_path);owner=paper(s)
    b=broker.FutuSim(s.settings,transport=lambda *args:{'ret_code':-1,'ret_msg':'offline rejected'})
    b.verified=True;b.on_definite_rejection=s.submission_failed
    s.reserve('legacy',owner,'stock','US.AAA','BUY',1,100,'2026-10-19');s.claim('legacy')
    with pytest.raises(broker.OrderRejected):
        try:b.submit(s.orders()[0])
        except Exception:
            s.unknown('legacy');raise
    assert not s.meta('paused') and s.orders()[0]['status']=='rejected'


def test_minute_factor_causality_replay_and_independent_cash(tmp_path):
    data=dataset();f=factor();checks=factors.causal_checks(data,f)
    assert checks['prefix_invariance'] and checks['future_perturbation']
    days=sorted({r['session'] for r in data['bars']['US.QQQ']})
    report={'windows':{'train':[days[0],days[1]],'validation':[days[2],days[3]],'retrospective_test':[days[4],days[-1]]},
        'cost_bps':30,'trials':[{'benchmark':{'return_pct':0}}]}
    result=factors.evaluate(data,f,report,store_at(tmp_path).settings)
    assert all(result['independent_ledger'].values()) and result['stats']['retrospective_test']['orders']>=10
    fn=factors.signal_function(data,f);symbol='US.AAA';rows=[r for r in data['bars'][symbol] if r['session']==days[0]]
    first=next(i for i in range(len(rows)) if fn(symbol,rows[:i+1],False)=='BUY')
    run=intraday.engine.replay(data,f['name'],[days[0]],signal_fn=fn)
    assert run['trades'][0]['time_ms']==rows[first]['end_ms']
    assert run['trades'][0]['price']==rows[first+1]['open']


def test_minute_factor_checkpoint_reuses_actual_generation_and_validation(tmp_path,monkeypatch):
    s=store_at(tmp_path);data=dataset();days=sorted({r['session'] for r in data['bars']['US.QQQ']});calls=[]
    report={'asof':days[-1],'data_sha256':'fixture','engine_sha256':intraday.engine_hash(),'data_path':'fixture',
        'windows':{'train':[days[0],days[1]],'validation':[days[2],days[3]],'retrospective_test':[days[4],days[-1]]},
        'cost_bps':30,'symbols':list(data['bars']),'trials':[{'family':'opening_range','stats':{},'stress':{},'delayed':{},'failed_gates':[],'benchmark':{'return_pct':0}}]}
    s.set_meta('intraday_research',report)
    monkeypatch.setattr(factors,'ROOT',tmp_path)
    monkeypatch.setattr(factors.intraday_data,'load',lambda *args:data)
    def ask(*args,**kwargs):calls.append('model');return {'result':factor(),'model':'fixture-model','reasoning_effort':'high'}
    monkeypatch.setattr(factors,'ask',ask)
    one=factors.evolve(s,['test evidence']);two=factors.evolve(s,['test evidence'])
    assert one==two and calls==['model'] and len(s.meta('minute_factor_registry'))==1
    assert s.meta('minute_factor_latest')['code_path']
    assert s.meta('minute_factor_cycle:'+days[-1]) and not s.orders()


def test_factor_future_reference_is_rejected_before_backtest():
    f=factor();f['expression']['nodes'][0]={'op':'lag','field':None,'a':None,'b':None,'x':0,'window':1}
    with pytest.raises(ValueError,match='future node'):factors.causal_checks(dataset(),f)


def test_persistent_stale_held_quote_keeps_reconciled_tick_running(tmp_path,monkeypatch):
    from quant_agent import runner
    from tests.test_quant_opening_resilience import setup_open,OfflineBroker
    s,p,ids=setup_open(tmp_path,monkeypatch)
    s.settings=replace(s.settings,continuous_paper=True,stock_market_fallback=True)
    s.set_meta('intraday_enabled',True)
    symbol=p.symbols[0];price=float(p.fields['close'][-1,0])
    s.reserve('held',ids[0],'stock',symbol,'BUY',1,price,'2026-10-12');s.claim('held');s.submitted('held','held-ack');s.fill('held',1,price,'filled')
    b=OfflineBroker(p);snapshots=b.snapshots
    b.state=lambda:{'cash':{'balance':100000},'positions':[{'symbol':symbol,'qty':1}],'orders':[]}
    b.snapshots=lambda syms:[{**q,'update_time':1} if q['code']==symbol else q for q in snapshots(syms)]
    runner.tick(s,b,run_daily=False)
    assert s.meta('held_quote_quality')['status']=='degraded'
    assert s.meta('last_tick')['session']=='2026-10-13' and not s.meta('paused')
    assert len(s.orders())==1


def test_profitability_failure_is_explicit_in_experimental_promotion(tmp_path):
    from quant_agent.champion import valid_record,pending
    s=store_at(tmp_path);data=dataset();days=sorted({r['session'] for r in data['bars']['US.QQQ']})
    report={'asof':days[-1],'data_sha256':'fixture','engine_sha256':intraday.engine_hash(),'data_path':'fixture',
        'windows':{'train':[days[0],days[1]],'validation':[days[2],days[3]],'retrospective_test':[days[4],days[-1]]},
        'cost_bps':30,'symbols':list(data['bars']),'rules':'offline experiment','report_path':'offline protocol','trials':[{'benchmark':{'return_pct':0}}]}
    trial=factors.evaluate(data,factor(),report,s.settings)
    trial['eligible']=False;trial['gates']['high_cost_profit']=False
    s.set_meta('intraday_research',report)
    intraday.consider_promotion(s,report,trial)
    candidate=s.strategies()[0]
    assert candidate['stage']=='shadow' and candidate['evidence']['paper_experiment']
    assert candidate['evidence']['passed'] is False and valid_record(candidate)
    assert pending(s)['target']==candidate['id'] and not s.orders()


def test_continuous_rotation_retries_confirmed_rejections_after_backoff(tmp_path,monkeypatch):
    from quant_agent import champion
    from tests.test_quant_champion import configured,NOW
    s,old,new,b=configured(tmp_path,True);s.settings=replace(s.settings,continuous_paper=True,stock_market_fallback=True)
    rotation=s.meta('champion_rotation')
    for i in range(5):
        key='rejected-exit-'+str(i);s.reserve(key,old['id'],'stock','US.AAA','SELL',10,100,'2026-10-13');s.claim(key);s.fill(key,0,0,'rejected')
        rotation['order_ids'].append(key)
    s.set_meta('champion_rotation',rotation);now=pd.Timestamp(NOW)
    monkeypatch.setattr(champion,'utcnow',lambda:now.isoformat())
    monkeypatch.setattr(champion,'executable_quote',lambda *args:(99.99,100.01))
    champion.rotation_tick(s,b,{'US.AAA':{'last_price':100}},'2026-10-13')
    assert not b.submits
    now+=pd.Timedelta(minutes=4);champion.rotation_tick(s,b,{'US.AAA':{}},'2026-10-13');assert not b.submits
    now+=pd.Timedelta(minutes=2);champion.rotation_tick(s,b,{'US.AAA':{}},'2026-10-13')
    assert len(b.submits)==1 and b.submits[0]['qty']==10


def test_missing_daily_research_cache_cannot_starve_account_exit_routing(tmp_path,monkeypatch):
    from quant_agent import runner,champion
    from tests.test_quant_opening_resilience import setup_open,OfflineBroker
    s,p,ids=setup_open(tmp_path,monkeypatch)
    s.settings=replace(s.settings,continuous_paper=True,stock_market_fallback=True)
    s.set_meta('intraday_enabled',True)
    b=OfflineBroker(p);calls=[]
    def missing(*args):raise FileNotFoundError('Controlled missing research cache')
    monkeypatch.setattr(runner,'load_panel',missing)
    monkeypatch.setattr(champion,'enabled',lambda _:True)
    monkeypatch.setattr(champion,'rotation_tick',lambda *args:calls.append('exit-route') or True)
    runner.tick(s,b,run_daily=False)
    assert calls==['exit-route'] and not s.meta('paused')
    assert s.meta('last_tick')['session']=='2026-10-13'
