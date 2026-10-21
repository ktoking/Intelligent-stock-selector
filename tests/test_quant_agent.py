"""Economic, causal, recovery and endpoint boundaries for the quant supervisor."""
from dataclasses import replace
import json
import sqlite3
from threading import Thread

import numpy as np
import pandas as pd
import pytest

from quant_agent.broker import BrokerError, FutuSim, executable_quote, session_now
from quant_agent.config import Settings, initialize
from quant_agent.engine import empty, plan, simulate
from quant_agent.pipeline import shadow_record
from quant_agent.runner import executor_lock, reconcile
from quant_agent.store import Store
from quant_agent.features import compile_spec, validate_weights
from quant_agent.ai import validate
from quant_agent.universe import mover
from quant_agent.dataset import frozen_view
from research.daily_factor_lab.core import Panel, Policy
from scripts.verify_daily_factor_run import audit_ledger


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path,initialize(tmp_path,quote_push_enabled=False,evolution_enabled=False))


def paper(store):
    key=store.propose({"policy":{"name":"test"}})
    for start,end in (("proposed","backtested"),("backtested","validated"),("validated","shadow"),("shadow","paper")):
        store.transition(key,start,end,{})
    return key


def test_budgets_pending_fees_and_duplicate_intent(store):
    strategy=paper(store)
    assert store.reserve("first",strategy,"stock","US.AAA","BUY",50,100,"2026-10-12")
    assert not store.reserve("first",strategy,"stock","US.AAA","BUY",50,100,"2026-10-12")
    with pytest.raises(ValueError,match="capital cap"):
        store.reserve("second",strategy,"stock","US.BBB","BUY",50,100,"2026-10-12")
    with pytest.raises(ValueError,match="another intent"):
        store.reserve("first",strategy,"stock","US.BBB","BUY",50,100,"2026-10-12")
    with pytest.raises(ValueError,match="capital cap"):
        store.reserve("option",strategy,"option","US.X-CALL","BUY",1,10,"2026-10-12",100)
    assert store.reserve("option",strategy,"option","US.X-CALL","BUY",1,9,"2026-10-12",100)
    with pytest.raises(ValueError,match="Uncovered"):
        store.reserve("short",strategy,"option","US.X-PUT","SELL",1,2,"2026-10-12",100)


def test_partial_fill_replay_and_cancel_release(store):
    strategy=paper(store)
    store.reserve("x",strategy,"stock","US.AAA","BUY",80,100,"2026-10-12")
    assert store.claim("x") and not store.claim("x")
    store.submitted("x","broker-x")
    store.fill("x",20,99,"partial")
    cash=store.portfolio()["cash"]
    store.fill("x",20,99,"partial")
    assert store.portfolio()["cash"]==cash
    with pytest.raises(ValueError,match="capital cap"):
        store.reserve("y",strategy,"stock","US.BBB","BUY",30,100,"2026-10-12")
    store.fill("x",20,99,"cancelled")
    assert store.reserve("y",strategy,"stock","US.BBB","BUY",30,100,"2026-10-12")
    assert store.portfolio()["positions"]["US.AAA"]["qty"]==20
    with pytest.raises(ValueError,match="Nonmonotonic"):
        store.fill("x",19,99,"cancelled")


def test_concurrent_reservations_cannot_overspend(store):
    strategy=paper(store); success=[]
    def reserve(key):
        try:
            success.append(store.reserve(key,strategy,"stock","US.AAA","BUY",60,100,"2026-10-12"))
        except ValueError:
            success.append(False)
    threads=[Thread(target=reserve,args=(str(i),)) for i in range(2)]
    for t in threads:t.start()
    for t in threads:t.join()
    assert success.count(True)==1


def test_unknown_submission_blocks_new_orders_and_restart(store):
    strategy=paper(store)
    store.reserve("x",strategy,"stock","US.AAA","BUY",1,100,"2026-10-12")
    store.claim("x");store.unknown("x")
    restored=Store(store.home,store.settings)
    assert restored.meta("paused")
    assert restored.orders()[0]["status"]=="unknown"
    with pytest.raises(ValueError,match="paused"):
        restored.reserve("y",strategy,"stock","US.BBB","BUY",1,100,"2026-10-12")
    assert not restored.claim("x")


def test_no_stage_skip_no_live_endpoint_or_unverified_account(store):
    strategy=store.propose({"policy":{"name":"test"}})
    with pytest.raises(ValueError,match="Forbidden"):
        store.transition(strategy,"proposed","paper",{})
    with pytest.raises(ValueError,match="paper gates"):
        store.reserve("x",strategy,"stock","US.AAA","BUY",1,100,"2026-10-12")
    broker=FutuSim(replace(Settings(),stock_account="123",paper_enabled=True))
    for method,path in [("POST","/api/v1.0/accounts/123/orders"),("POST","/api/v1.0/sim-trade/123/../accounts/123/orders")]:
        with pytest.raises(BrokerError,match="allowlist"):
            broker.request(method,path,body={})
    with pytest.raises(BrokerError,match="Verified"):
        broker.request("POST","/api/v1.0/sim-trade/123/orders",body={})


def test_current_quotes_calendar_and_early_close():
    settings=Settings();now=pd.Timestamp("2026-10-12T13:31:00Z")
    q={"update_time":int(now.timestamp()*1000),"bid_price":99.99,"ask_price":100.01}
    assert executable_quote(q,settings,now)==(99.99,100.01)
    with pytest.raises(BrokerError,match="Stale"):
        executable_quote(q,settings,now+pd.Timedelta(seconds=91))
    assert session_now("2026-10-10T15:00Z") is None
    assert session_now("2026-11-27T17:59Z")=="2026-11-27"
    assert session_now("2026-11-27T18:00Z") is None
    with pytest.raises(BrokerError,match="Spread"):
        executable_quote({**q,"ask_price":110},settings,now)
    with pytest.raises(BrokerError,match="unavailable"):
        executable_quote({**q,"ask_price":0},settings,now)


def panel():
    dates=pd.bdate_range("2024-01-01",periods=360)
    x=np.arange(360);c=np.column_stack([100+x*.2,70+x*.1,200+x*.15])
    fields={"open":c*1.001,"close":c,"returns":np.vstack([np.zeros(3),c[1:]/c[:-1]-1]),
            "vol20":np.full_like(c,.02),"liquid20":np.full_like(c,20e6),"mom63":np.full_like(c,.2),
            "momentum_risk":np.tile([3.,2.,1.],(360,1))}
    fields.update(high=c*1.01,low=c*.99,volume=np.full_like(c,1e6),turnover=np.full_like(c,100e6))
    for k,m in ((20,.96),(50,.92),(60,.9),(100,.88),(150,.8),(200,.75)):
        fields["ema"+str(k)]=c*m
    return Panel(dates,["US.AAA","US.BBB","US.QQQ"],np.array([True,True,False]),fields,{})


def test_engine_independently_reconciles_and_future_does_not_change_prefix():
    p=panel();policy=Policy("test",positions=2,rank_buffer=2,annual_vol_target=1,weight_cap=.4)
    start,end=map(lambda x:str(x.date()),(p.dates[220],p.dates[290]))
    run=simulate(p,policy,start,end)
    assert run["orders"]
    audit=audit_ledger(p,run["curve"],run["orders"],25)
    assert audit["final_equity"]==pytest.approx(run["curve"].equity.iloc[-1])
    for v in p.fields.values():v[291:]*=5
    changed=simulate(p,policy,start,end)
    assert changed["orders"]==run["orders"]
    pd.testing.assert_frame_equal(changed["curve"],run["curve"])
    with pytest.raises(ValueError,match="precede"):
        plan(p,220,policy,empty(),str(p.dates[220].date()),{"US.AAA":144,"US.BBB":92,"US.QQQ":233})


def test_historical_replay_cannot_fill_future_shadow_requirement(store):
    record={"id":"test","evidence":{"shadow_started_at":"2026-10-10T00:00:00Z"}}
    with pytest.raises(ValueError,match="actually observed"):
        shadow_record(store,record,"2026-10-09",{"nav":11000},"hash",observed="2026-10-10T01:00Z")
    with pytest.raises(ValueError,match="actually observed"):
        shadow_record(store,record,"2026-10-12",{"nav":11000},"hash",observed="2026-10-12T13:00Z")


def test_twenty_real_shadow_sessions_not_nineteen(store):
    import exchange_calendars as xc
    key=store.propose({"policy":{"name":"test"}})
    evidence={"shadow_started_at":"2026-10-10T00:00:00Z"}
    for start,end in (("proposed","backtested"),("backtested","validated"),("validated","shadow")):
        store.transition(key,start,end,evidence)
    record=store.strategies()[0];cal=xc.get_calendar("XNYS")
    days=cal.sessions_in_range("2026-10-12","2026-11-12")[:20]
    for i,day in enumerate(days):
        shadow_record(store,record,str(day.date()),{"nav":10010+i*10,"order_count":1},"hash",observed=cal.session_close(day)+pd.Timedelta(minutes=1))
        assert store.strategies()[0]["stage"] == ("shadow" if i<19 else "paper")
    assert len(store.orders())==0


def test_single_executor_lock(store):
    with executor_lock(store):
        with pytest.raises(RuntimeError,match="already running"):
            with executor_lock(store):pass


def test_reconcile_finds_ambiguous_submission_by_marker(store):
    strategy=paper(store)
    store.reserve("x",strategy,"stock","US.AAA","BUY",2,100,"2026-10-12")
    store.claim("x");store.unknown("x")
    remote_order={"order_id":"broker-x","text":"qa:x","symbol":"AAA","side":1,"qty":"2","status":4,"cum_qty":"2","avg_fill_price":"99"}
    class Fake:
        verified=True
        def state(self):return {"orders":[remote_order],"positions":[{"symbol":"AAA","qty":"2","pstn_type":0}],"cash":{"balance":"999802"}}
        def history(self,begin):return [remote_order]
    reconcile(store,Fake())
    assert store.orders()[0]["status"]=="filled"
    assert store.portfolio()["positions"]["US.AAA"]["qty"]==2
    assert store.meta("paused")  # Reconciliation does not silently override a safety pause.


def test_external_positions_block_executor(store):
    class Fake:
        verified=True
        def state(self):return {"orders":[],"positions":[{"symbol":"NVDA","qty":"1"}],"cash":{"balance":"1000000"}}
    with pytest.raises(BrokerError,match="differ"):
        reconcile(store,Fake())


def test_factor_dsl_is_causal_and_rejects_arbitrary_or_invalid_weights():
    p=panel()
    spec={"policy":{"name":"blend","positions":2,"rank_buffer":2},"factor_weights":[{"factor":"momentum_risk","weight":1.0}]}
    compiled,policy=compile_spec(p,spec)
    old=compiled.fields["momentum_blend"][:250].copy()
    p.fields["momentum_risk"][250:]*=-1
    changed,_=compile_spec(p,spec)
    np.testing.assert_allclose(changed.fields["momentum_blend"][:250],old,equal_nan=True)
    assert np.isnan(compiled.fields["momentum_blend"][:,2]).all()
    assert policy.factor=="momentum_blend"
    with pytest.raises(ValueError,match="sum to one"):
        validate_weights([{"factor":"near_high","weight":.7}])
    with pytest.raises(ValueError,match="Invalid"):
        validate_weights([{"factor":"__import__('os')","weight":1}])


def test_sell_proceeds_not_reused_same_session_and_t1_reservation(store):
    p=panel();policy=Policy("test",positions=1,rank_buffer=1,annual_vol_target=1)
    p.fields["momentum_risk"][-2]=[1,5,0]
    old={**empty(),"cash":0,"holdings":{"US.AAA":50},"peaks":{"US.AAA":170}}
    orders,_=plan(p,358,policy,old,"2025-06-01",{"US.AAA":170,"US.BBB":105,"US.QQQ":250})
    assert all(o['side']=='SELL' for o in orders)
    strategy=paper(store)
    store.reserve("buy",strategy,"stock","US.AAA","BUY",90,100,"2026-10-12")
    store.claim("buy");store.submitted("buy","B");store.fill("buy",90,100,"filled")
    store.reserve("sell",strategy,"stock","US.AAA","SELL",90,100,"2026-10-12")
    store.claim("sell");store.submitted("sell","S");store.fill("sell",90,100,"filled")
    with pytest.raises(ValueError,match="capital cap"):
        store.reserve("rebuy",strategy,"stock","US.AAA","BUY",90,100,"2026-10-12")


def test_ai_schema_rejects_instruction_like_extra_fields():
    with pytest.raises(Exception):
        validate({"candidates":[],"review":"","factor_ideas":[],"execute_order":True})


def test_anomalies_need_stock_classification_liquidity_and_no_order_intent():
    quote={"code":"US.AAA","last_price":106,"prev_close_price":100,"turnover":40e6,"data_date":"2026-10-09"}
    assert mover(quote) is None
    row=mover(quote,stock_type="STOCK")
    assert row["change_pct"]==pytest.approx(6)
    assert row["status"]=="research_candidate_not_order"
    assert mover({**quote,"turnover":1e6},stock_type="STOCK") is None
    assert mover({**quote,"stock_type":"ETF"},stock_type="STOCK") is None


def test_adding_market_movers_cannot_change_frozen_strategy_membership():
    p=panel()
    from research.daily_factor_lab.paper import data_prefix_hash
    record={"evidence":{"symbols":["US.AAA","US.QQQ"]}}
    view=frozen_view(p,record)
    initial=data_prefix_hash(view,str(p.dates[-1].date()))
    p.fields["close"][:,1]*=100
    assert data_prefix_hash(frozen_view(p,record),str(p.dates[-1].date()))==initial
    assert view.symbols==["US.AAA","US.QQQ"]


def test_full_history_chunks_cannot_silently_truncate(monkeypatch):
    from quant_agent.market_data import full_symbol
    monkeypatch.setattr('quant_agent.market_data.time.sleep',lambda _:None)
    class Fake:
        def request(self,method,path,query):
            days=pd.bdate_range(query['start'],query['end'])
            return {'data':{'kline_list':[{'date':int(d.strftime('%Y%m%d')),'close':100} for d in days]}}
    frame=full_symbol(Fake(),'US.AAA','2023-09-01','2026-10-09')
    assert len(frame)>750 and int(frame.date.iloc[-1])==20261009
    class Truncated:
        def request(self,*args):return {'data':{'kline_list':[{}]*370}}
    with pytest.raises(ValueError,match="truncated"):
        full_symbol(Truncated(),'US.AAA','2026-09-01','2026-10-09')


def test_ai_rationale_does_not_duplicate_same_executable_strategy(store):
    spec={"policy":{"name":"test"},"factor_weights":[],"origin":"fixed_catalog","hypothesis":"control"}
    key=store.propose(spec)
    assert store.propose({**spec,"origin":"ai","hypothesis":"new explanation"})==key
    assert len(store.strategies())==1


def test_only_one_strategy_can_own_paper_executor(store):
    first=paper(store)
    second=store.propose({"policy":{"name":"another"}})
    for start,end in (("proposed","backtested"),("backtested","validated"),("validated","shadow")):
        store.transition(second,start,end,{})
    with pytest.raises(ValueError,match="owns"):
        store.transition(second,"shadow","paper",{})
    assert len(store.strategies(['paper']))==1


def daily_fixture(store, monkeypatch):
    from quant_agent import runner
    p=panel()
    p.metadata={"end":"2026-10-09","input_sha256":"daily-test"}
    monkeypatch.setattr(runner,"load_panel",lambda _:p)
    monkeypatch.setattr(runner,"latest_completed_session",lambda:"2026-10-09")
    store.set_meta("universe_scanned:2026-10-09",True)
    store.set_meta('news_completed:2026-10-09',True)
    return runner


def test_daily_review_retries_failed_cached_ai_without_overwriting_evidence(store,monkeypatch):
    runner=daily_fixture(store,monkeypatch)
    def unavailable(*args,**kwargs):
        raise TimeoutError("test timeout")
    monkeypatch.setattr(runner,"ask",unavailable)
    runner.daily_jobs(store,{})
    assert not store.meta("daily_completed:2026-10-09")
    assert store.meta("daily_retry_after:2026-10-09")
    failed=next((store.home/"artifacts").glob("daily-*.json"))
    assert json.loads(failed.read_text())["ai"] is None
    calls=[]
    response={"provider":"codex","model":"test","result":{"review":"复盘恢复","factor_ideas":[],"candidates":[]}}
    def available(*args,**kwargs):
        calls.append(args)
        return response
    monkeypatch.setattr(runner,"ask",available)
    runner.daily_jobs(store,{})
    assert not calls  # Retry backoff remains in force.
    from quant_agent.checkpoints import retry_failed
    retry_failed(store)
    runner.daily_jobs(store,{})
    assert len(calls)==1 and store.meta("daily_completed:2026-10-09")
    assert json.loads(failed.read_text())["ai"] is None
    assert len(list((store.home/"artifacts").glob("daily-*-retry-*.json")))==1
    assert "复盘恢复" in (store.home/"reports/2026-10-09.md").read_text()


def test_trial_cap_does_not_stop_daily_review_or_existing_validation(store,monkeypatch):
    runner=daily_fixture(store,monkeypatch)
    store.settings=replace(store.settings,daily_research=True,max_trials=1)
    store.propose({"policy":{"name":"bounded"},'research_data_sha256':'daily-test'})
    def forbidden(*args,**kwargs):
        raise AssertionError("must not generate beyond the trial cap")
    monkeypatch.setattr(runner,"propose",forbidden)
    evaluated=[]
    monkeypatch.setattr(runner,"evaluate",lambda s:evaluated.append(s))
    response={"provider":"codex","model":"test","result":{"review":"继续监控","factor_ideas":[],"candidates":[]}}
    monkeypatch.setattr(runner,"ask",lambda *args,**kwargs:response)
    runner.daily_jobs(store,{})
    assert evaluated==[store]
    assert store.meta("research_completed:daily-test")
    assert store.meta("daily_completed:2026-10-09")
    assert len(store.strategies())==1


def test_calendar_scheduler_weekend_dst_and_early_close(store):
    from quant_agent.schedule import activity,calendar_state,daily_due
    store.set_meta('daily_completed:2026-10-09',True)
    weekend=activity(store,'2026-10-10T10:00Z')
    assert weekend['mode']=='idle'
    assert weekend['next_open']=='2026-10-12T13:30:00+00:00'
    assert calendar_state('2026-11-07T10:00Z')['next_open']=='2026-11-09T14:30:00+00:00'
    assert activity(store,'2026-10-12T13:26Z')['mode']=='pre_open'
    store.set_meta('preflight_session','2026-10-12')
    assert activity(store,'2026-10-12T13:27Z')['mode']=='idle'
    assert activity(store,'2026-10-12T13:31Z')['mode']=='market_open'
    assert not daily_due(store,'2026-11-27T18:14Z')
    assert daily_due(store,'2026-11-27T18:15Z')
    store.set_meta('daily_retry_after:2026-11-27','2026-11-27T19:00Z')
    assert not daily_due(store,'2026-11-27T18:30Z')


def test_closed_market_keeps_order_recovery_active(store):
    from quant_agent.schedule import activity
    strategy=paper(store)
    store.reserve('unresolved',strategy,'stock','US.AAA','BUY',1,100,'2026-10-09')
    assert activity(store,'2026-10-10T10:00Z')['mode']=='order_recovery'


def test_idle_supervisor_does_not_poll_broker_or_launch_daily_worker(store,monkeypatch):
    from quant_agent import runner
    calls=[]
    class Stop:
        cycles=0
        def is_set(self):return self.cycles>=3
        def wait(self,_):self.cycles+=1
    monkeypatch.setattr(runner,'activity',lambda s,boot=False:{'mode':'startup_reconcile' if boot else 'idle'})
    monkeypatch.setattr(runner,'daily_due',lambda s:False)
    monkeypatch.setattr(runner,'tick',lambda *args,**kwargs:calls.append('tick'))
    monkeypatch.setattr(runner,'FutuSim',lambda _:object())
    runner.serve(store,Stop())
    assert calls==['tick']
    assert store.meta('health')['mode']=='idle'


def test_forward_dashboard_has_no_synthetic_equity_and_no_private_order_ids(store):
    from quant_agent.web import chart_data,public_status
    key=store.propose({'policy':{'name':'test'},'research_data_sha256':'test'})
    data=chart_data(store,key,'forward')
    assert data['curve']==[] and data['benchmark_curve']==[]
    snapshot=public_status(store)
    assert snapshot['strategies'][0]['shadow_sessions']==0
    assert snapshot['live_enabled'] is False
    assert {j['key'] for j in snapshot['tasks']}=={'refresh','scan','news','discovery','backtest','review','evolution','factor_validation','promotion','premarket'}


def test_index_pages_require_complete_unique_membership():
    from quant_agent.indices import constituents
    class Pages:
        calls=[]
        def request(self,method,path,query):
            self.calls.append(query)
            if 'next_key' not in query:
                return {'data':{'stock_list':[{'symbol':'US.AAA'},{'symbol':'US.BRK.B'}]},'pagination':{'has_more':True,'next_key':'page2','total':3}}
            return {'data':{'stock_list':[{'symbol':'US.CCC'}]},'pagination':{'has_more':False,'total':3}}
    broker=Pages()
    assert constituents(broker,'US..NDX',2,5)==['US.AAA','US.BRK.B','US.CCC']
    assert broker.calls[1]['next_key']=='page2'
    class Missing:
        def request(self,*args):return {'data':{'stock_list':[{'symbol':'US.AAA'}]},'pagination':{'has_more':False,'total':3}}
    with pytest.raises(ValueError,match='incomplete'):constituents(Missing(),'US..NDX',1,5)
    class Stuck:
        def request(self,*args):return {'data':{'stock_list':[]},'pagination':{'has_more':True,'next_key':'same'}}
    with pytest.raises(ValueError,match='advance'):constituents(Stuck(),'US..NDX',1,5)


def test_news_cannot_enter_analysis_before_first_observation(store):
    from quant_agent.news import normalize,evidence_at
    observed='2026-10-10T10:00:00+00:00'
    row={'news_id':'1','title':'<em>消息</em>','publish_time':pd.Timestamp('2026-10-09T15:00Z').timestamp()*1000,'url':'https://news.futunn.com/post/1'}
    n=normalize(row,'US.NVDA',observed,'test')
    assert n['title']=='消息' and n['content_level']=='headline_only'
    assert normalize({**row,'publish_time':pd.Timestamp('2026-10-11T00:00Z').timestamp()},'US.NVDA',observed,'test') is None
    assert normalize({**row,'url':'javascript:alert(1)'},'US.NVDA',observed,'test') is None
    store.set_meta('news_packet',{'items':[n]})
    assert evidence_at(store,'2026-10-10T17:59:59+08:00')==[]
    assert evidence_at(store,'2026-10-10T18:00:00+08:00')==[n]


def test_paper_fills_account_for_partial_replay_and_realized_profit(store):
    from quant_agent.runner import mark_portfolio
    strategy=paper(store)
    for key,side,qty,price in [('b','BUY',10,100),('s','SELL',5,110)]:
        store.reserve(key,strategy,'stock','US.AAA',side,qty,price,'2026-10-12')
        store.claim(key);store.submitted(key,'remote-'+key)
        store.fill(key,qty,price,'filled');store.fill(key,qty,price,'filled')
    p=store.paper_activity()
    assert p['fill_count']==2 and len(p['fills'])==2
    assert p['estimated_fees']==pytest.approx(4.65)
    assert p['realized_pnl']==pytest.approx(46.85)
    assert p['unrealized_pnl']==pytest.approx(48.5)
    assert p['total_pnl']==pytest.approx(p['realized_pnl']+p['unrealized_pnl'])
    mark_portfolio(store,{'US.AAA':{'last_price':111}},{'cash':{'balance':1e6}})
    p=store.paper_activity()
    assert p['curve'][-1]['nav']==pytest.approx(10100.35)
    assert p['total_pnl']==pytest.approx(100.35)


def test_new_factor_expressions_are_causal_and_cannot_execute_code():
    from quant_agent.evolution import validate_expression,causal_checks,compile_strategy
    from quant_agent.pipeline import engine_hash
    p=panel();x=np.arange(len(p.dates));p.fields['returns']=np.column_stack([np.sin(x/4)*.01,np.cos(x/7)*.01,x*0.])
    expr={'op':'div','a':{'op':'mean','window':5,'x':{'field':'returns'}},'b':{'field':'vol20'}}
    checks=causal_checks(expr,p)
    assert checks['prefix_invariance'] and checks['future_perturbation']
    compiled,policy=compile_strategy(p,{'factor_expression':expr,'policy':{'name':'new'}})
    assert policy.factor=='momentum_blend' and compiled.fields['momentum_blend'].shape==(360,3)
    for bad in [{'field':'__import__'},{'op':'lag','window':-1,'x':{'field':'returns'}},{'op':'eval','x':{'field':'returns'}}]:
        with pytest.raises(ValueError):validate_expression(bad)
    deep={'field':'returns'}
    for _ in range(15):deep={'op':'lag','window':1,'x':deep}
    with pytest.raises(ValueError,match='15 nodes'):validate_expression(deep)
    assert engine_hash()==engine_hash({'policy':{'name':'old'}})
    assert engine_hash({'factor_expression':expr})!=engine_hash()


def test_factor_graph_rejects_future_links_and_redundant_ranks():
    from quant_agent.evolution import expression_tree,validate_expression
    nodes=[{'op':'field','field':'returns','a':None,'b':None,'x':None,'window':None},
           {'op':'mean','field':None,'a':None,'b':None,'x':0,'window':5}]
    assert expression_tree({'nodes':nodes,'root':1})=={'op':'mean','x':{'field':'returns'},'window':5}
    with pytest.raises(ValueError,match='precede'):
        expression_tree({'nodes':[nodes[0],{**nodes[1],'x':1}],'root':1})
    with pytest.raises(ValueError,match='Redundant'):
        validate_expression({'op':'rank','x':{'op':'rank','x':{'field':'returns'}}})


def test_trial_cap_applies_per_data_version_and_preserves_all_trials(store):
    store.settings=replace(store.settings,max_trials=1)
    store.propose({'policy':{'name':'first'},'research_data_sha256':'v1'})
    with pytest.raises(ValueError,match='Trial cap'):
        store.propose({'policy':{'name':'second'},'research_data_sha256':'v1'})
    store.propose({'policy':{'name':'third'},'research_data_sha256':'v2'})
    assert len(store.strategies())==2


def test_websocket_handles_deployed_ack_envelope(monkeypatch):
    from quant_agent.push import handshake,unpack
    monkeypatch.setattr('quant_agent.push.auth_frame',lambda:{'id':'a','action':'auth','data':{}})
    class Socket:
        sent=[]
        messages=iter([{'id':'a','session_id':'test'},{'ret_code':0,'data':{'code':0,'id':'focus-sub','message':''}}])
        def send(self,value):self.sent.append(json.loads(value))
        def recv(self):return json.dumps(next(self.messages))
    s=Socket();handshake(s,['US.NVDA'])
    assert s.sent[-1]['quote']==['US.NVDA'] and 'order_book' not in s.sent[-1]
    with pytest.raises(RuntimeError):unpack({'ret_code':-1,'data':{}})


def test_websocket_signing_is_verified_without_real_credentials(tmp_path,monkeypatch):
    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    from quant_agent.push import auth_frame
    key=Ed25519PrivateKey.generate();path=tmp_path/'test.key'
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    monkeypatch.setenv('FUTUNN_PRIVATE_KEY_FILE',str(path));monkeypatch.setenv('FUTUNN_APP_KEY','fake')
    d=auth_frame()['data']
    key.public_key().verify(base64.b64decode(d['authorization']),f"{d['timestamp_ms']}\n{d['nonce']}\nWEBSOCKET\nws/auth".encode())
    assert d['auth_type']=='appkey'


def test_broad_scan_batches_400_and_reports_missing_quotes(store,monkeypatch):
    from quant_agent import universe
    names=['US.A'+str(i) for i in range(405)];batches=[]
    idx={'observed_at':'2026-10-10T00:00Z','indexes':{'nasdaq100':{'members':names[:101]},'sp500':{'members':names[50:]}}}
    store.set_meta('index_universes',idx);store.set_meta('watchlist_symbols',[names[0]])
    monkeypatch.setattr(universe,'load_panel',lambda _:panel())
    monkeypatch.setattr(universe.time,'sleep',lambda _:None)
    class Broker:
        def snapshots(self,symbols):
            batches.append(symbols)
            return [{'code':s,'last_price':100,'prev_close_price':100,'turnover':40e6} for s in symbols if s!=names[404]]
    monkeypatch.setattr(universe,'FutuSim',lambda _:Broker())
    result=universe.scan(store,refresh_members=False)
    assert [len(b) for b in batches]==[400,5]
    assert result['missing_symbols']==[names[404]] and result['scanned']==405
    assert result['hits']==[]


def test_evolution_failure_retries_before_daily_completion(store,monkeypatch):
    from quant_agent import evolution
    runner=daily_fixture(store,monkeypatch)
    store.settings=replace(store.settings,evolution_enabled=True)
    response={'provider':'codex','model':'gpt-6.1-sol','reasoning_effort':'high','result':{'review':'没有成交，无执行归因','factor_ideas':[],'candidates':[]}}
    monkeypatch.setattr(runner,'ask',lambda *a,**kw:response)
    def failed(*a):raise TimeoutError('test')
    monkeypatch.setattr(evolution,'evolve',failed)
    runner.daily_jobs(store,{})
    assert not store.meta('daily_completed:2026-10-09')
    assert store.meta('task:evolution')['status']=='failed'
    from quant_agent.checkpoints import retry_failed
    retry_failed(store)
    key=store.propose({'policy':{'name':'new'},'research_data_sha256':'daily-test'})
    monkeypatch.setattr(evolution,'evolve',lambda *a:{'strategy':key,'name':'new','hypothesis':'test'})
    monkeypatch.setattr(runner,'evaluate',lambda s:[])
    runner.daily_jobs(store,{})
    assert store.meta('daily_completed:2026-10-09')
    assert store.meta('ai_runtime')['reasoning_effort']=='high'


def test_graph_node_reuse_survives_serialization_without_false_size_rejection():
    from quant_agent.evolution import expression_score,validate_expression
    def n(op,**kw):return dict(op=op,field=None,a=None,b=None,x=None,window=None)|kw
    expr={'nodes':[n('field',field='returns'),n('mean',x=0,window=5),n('mean',x=0,window=20),n('sub',a=1,b=2),
                   n('abs',x=3),n('lag',x=4,window=1),n('mean',x=5,window=3),n('field',field='vol20'),
                   n('div',a=6,b=7),n('field',field='mom63'),n('rank',x=9),n('rank',x=8),n('sub',a=10,b=11),
                   n('mean',x=12,window=2),n('lag',x=13,window=1)],'root':14}
    restored=json.loads(json.dumps(expr));validate_expression(restored)
    p=panel()
    np.testing.assert_allclose(expression_score(expr,p.fields,p.stock_mask),expression_score(restored,p.fields,p.stock_mask),equal_nan=True)
