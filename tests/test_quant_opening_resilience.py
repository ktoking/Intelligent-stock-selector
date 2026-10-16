"""Offline opening-cycle fault injection. No actual broker/network orders."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from http.client import RemoteDisconnected
from types import SimpleNamespace

import pandas as pd
import pytest

from quant_agent import runner
from quant_agent.broker import executable_quote,QuoteUnavailable,ReadUnavailable
from quant_agent.config import initialize,Settings
from quant_agent.exchange_clock import us_calendar
from quant_agent.paper_activation import activate_batch
from quant_agent.pipeline import engine_hash
from quant_agent.store import Store
from research.daily_factor_lab.core import Policy
from research.daily_factor_lab.paper import data_prefix_hash
from tests.test_quant_agent import panel

OPEN=pd.Timestamp('2026-10-13T13:30:10Z')


def setup_open(tmp_path,monkeypatch,positions=2):
    s=Store(tmp_path,initialize(tmp_path,parallel_paper=True,paper_enabled=True,
        stock_account='offline-simulation',quote_push_enabled=False,evolution_enabled=False))
    p=panel();p.dates=pd.bdate_range(end='2026-10-12',periods=len(p.dates))
    p.metadata={'end':'2026-10-12','input_sha256':'offline-open-fixture'}
    ids=[]
    for i in range(3):
        policy=Policy('offline-'+str(i),positions=positions,rank_buffer=3,weight_cap=.4,annual_vol_target=1)
        spec={'policy':asdict(policy),'factor_weights':[],'engine_sha256':engine_hash(),'research_data_sha256':p.metadata['input_sha256']}
        identity=s.propose(spec)
        evidence={'passed':True,'seed_asof':'2026-10-12','seed_hash':data_prefix_hash(p,'2026-10-12'),'symbols':p.symbols}
        for a,b in [('proposed','backtested'),('backtested','validated'),('validated','shadow')]:s.transition(identity,a,b,evidence)
        ids.append(identity)
    activate_batch(s,ids,'offline fixture authorization')
    class Clock(pd.Timestamp):
        @classmethod
        def now(cls,tz=None):return OPEN
    monkeypatch.setattr(runner,'pd',SimpleNamespace(Timestamp=Clock,Timedelta=pd.Timedelta))
    monkeypatch.setattr(runner,'session_now',lambda:'2026-10-13')
    monkeypatch.setattr(runner,'latest_completed_session',lambda:'2026-10-12')
    monkeypatch.setattr(runner,'load_panel',lambda _:p)
    monkeypatch.setattr(runner,'executable_quote',lambda q,settings,**kw:executable_quote(q,settings,now=OPEN,**kw))
    return s,p,ids


class OfflineBroker:
    verified=True
    def __init__(self,p):self.panel=p;self.rows=[];self.calls=[];self.fail_read=False;self.fail_quotes=False;self.fail_submit=False
    def state(self):
        self.calls.append('state')
        if self.fail_read:self.fail_read=False;raise ReadUnavailable('Offline injected read disconnect')
        return {'cash':{'balance':100000},'positions':[],'orders':self.rows}
    def history(self,_):return self.rows
    def snapshots(self,symbols):
        self.calls.append('quotes')
        if self.fail_quotes:raise ReadUnavailable('Offline injected quote disconnect')
        rows=[]
        for symbol in symbols:
            price=float(self.panel.fields['close'][-1,self.panel.symbols.index(symbol)]) if symbol in self.panel.symbols else 100.
            rows.append({'code':symbol,'last_price':price,'prev_close_price':price,'bid_price':price*.999,'ask_price':price*1.001,
                'update_time':int(OPEN.timestamp()*1000),'data_date':'2026-10-13'})
        return [{**q,'last_price':None} if q['code']=='US.SPY' else q for q in rows]
    def submit(self,o):
        self.calls.append('submit')
        if self.fail_submit:raise RemoteDisconnected('Offline injected acknowledgement loss')
        identity='offline-ack-'+o['id']
        self.rows.append({'order_id':identity,'symbol':o['symbol'][3:],'qty':o['qty'],'side':1 if o['side']=='BUY' else 2,
            'status':2,'cum_qty':0,'avg_fill_price':0,'text':'qa:'+o['id']})
        return identity
    def cancel(self,identity):
        self.calls.append('cancel')
        next(r for r in self.rows if r['order_id']==identity)['status']=5
        return {'ret_code':0}


def test_opening_parallel_plans_ignore_invalid_unused_quote_and_are_idempotent(tmp_path,monkeypatch):
    s,p,ids=setup_open(tmp_path,monkeypatch);broker=OfflineBroker(p)
    runner.tick(s,broker,run_daily=False)
    orders=s.orders();assert orders and {o['strategy'] for o in orders}==set(ids)
    assert all(o['status']=='submitted' for o in orders) and not s.meta('paused')
    for identity in ids:
        budget=s.strategy_portfolio(identity)['budget']
        assert sum(o['qty']*o['price']+o['fee'] for o in orders if o['strategy']==identity)<=budget
        assert s.meta('paper_controls:'+identity)['sessions']==1
    runner.tick(s,broker,run_daily=False)
    assert len(s.orders())==len(orders) and broker.calls.count('submit')==len(orders)
    assert all(s.meta('paper_controls:'+identity)['sessions']==1 for identity in ids)


def test_wide_spread_in_top_rank_defers_only_that_order_and_retries_once(tmp_path,monkeypatch):
    s,p,ids=setup_open(tmp_path,monkeypatch);broker=OfflineBroker(p)
    snapshots=broker.snapshots;wide=[True]
    def market(symbols):
        return [{**q,'bid_price':q['last_price']*.98,'ask_price':q['last_price']*1.02}
            if q['code']=='US.AAA' and wide[0] else q for q in snapshots(symbols)]
    monkeypatch.setattr(broker,'snapshots',market)
    runner.tick(s,broker,run_daily=False)
    assert s.orders() and all(o['symbol']!='US.AAA' for o in s.orders())
    assert {o['strategy'] for o in s.orders()}==set(ids)
    assert all(r['status']=='partial' for r in s.meta('last_tick')['strategy_checks'])
    wide[0]=False;runner.tick(s,broker,run_daily=False)
    assert len([o for o in s.orders() if o['symbol']=='US.AAA'])==3
    count=broker.calls.count('submit');runner.tick(s,broker,run_daily=False)
    assert broker.calls.count('submit')==count
    assert all(s.meta('paper_controls:'+identity)['sessions']==1 for identity in ids)


def test_unplanned_session_reports_missed_window_instead_of_checked(tmp_path,monkeypatch):
    s,p,ids=setup_open(tmp_path,monkeypatch);broker=OfflineBroker(p)
    class AfterOpen(pd.Timestamp):
        @classmethod
        def now(cls,tz=None):return OPEN+pd.Timedelta(minutes=6)
    monkeypatch.setattr(runner,'pd',SimpleNamespace(Timestamp=AfterOpen,Timedelta=pd.Timedelta))
    runner.tick(s,broker,run_daily=False)
    assert not s.orders()
    assert all(r['status']=='missed_window' for r in s.meta('last_tick')['strategy_checks'])
    with s.transaction() as db:
        count=db.execute("SELECT COUNT(*) FROM events WHERE kind='paper_window_missed'").fetchone()[0]
    runner.tick(s,broker,run_daily=False)
    with s.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='paper_window_missed'").fetchone()[0]==count==3


def test_new_research_seed_and_running_paper_seed_remain_independent(tmp_path,monkeypatch):
    from copy import deepcopy
    s,p,ids=setup_open(tmp_path,monkeypatch);revised=deepcopy(p)
    revised.fields['turnover'][-1,0]*=1.01
    s.set_meta('execution_data_dir','observed');s.set_meta('current_data_dir','revised')
    monkeypatch.setattr(runner,'load_panel',lambda directory:revised if directory=='revised' else p)
    old_record=next(r for r in s.strategies() if r['id']==ids[0])
    new_record=deepcopy(old_record);new_record['id']='new-research-version'
    new_record['evidence']['seed_hash']=data_prefix_hash(revised,'2026-10-12')
    assert runner.checked_panel(s,old_record).fields['turnover'][-1,0]==p.fields['turnover'][-1,0]
    assert runner.checked_panel(s,new_record).fields['turnover'][-1,0]==revised.fields['turnover'][-1,0]


def test_read_disconnect_blocks_cycle_then_recovers_without_extra_orders(tmp_path,monkeypatch):
    s,p,ids=setup_open(tmp_path,monkeypatch);broker=OfflineBroker(p);broker.fail_read=True
    with pytest.raises(ReadUnavailable):runner.tick(s,broker,run_daily=False)
    assert s.orders()==[] and not s.meta('paused')
    runner.tick(s,broker,run_daily=False)
    assert {o['strategy'] for o in s.orders()}==set(ids)


def test_expired_cancel_runs_before_quote_outage(tmp_path,monkeypatch):
    s,p,ids=setup_open(tmp_path,monkeypatch);broker=OfflineBroker(p)
    s.reserve('expired',ids[0],'stock','US.AAA','BUY',1,100,'2026-10-12');s.claim('expired')
    broker_id=broker.submit(s.orders()[0]);s.submitted('expired',broker_id)
    broker.fail_quotes=True
    with pytest.raises(ReadUnavailable):runner.tick(s,broker,run_daily=False)
    assert broker.calls.index('cancel')<broker.calls.index('quotes')
    assert broker.rows[0]['status']==5 and not s.meta('paused')


def test_lost_acknowledgement_never_retries_or_executes_other_strategies(tmp_path,monkeypatch):
    s,p,_=setup_open(tmp_path,monkeypatch);broker=OfflineBroker(p);broker.fail_submit=True
    with pytest.raises(RemoteDisconnected):runner.tick(s,broker,run_daily=False)
    assert s.meta('paused') and len(s.orders())==1 and s.orders()[0]['status']=='unknown'
    broker.fail_submit=False;runner.tick(s,broker,run_daily=False)
    assert broker.calls.count('submit')==1 and len(s.orders())==1


def test_plan_and_control_transaction_rolls_back_on_event_write_failure(tmp_path,monkeypatch):
    s,p,_=setup_open(tmp_path,monkeypatch);broker=OfflineBroker(p);original=s.event
    def fail(kind,*args,**kwargs):
        if kind=='paper_plan':raise RuntimeError('Offline injected commit failure')
        return original(kind,*args,**kwargs)
    monkeypatch.setattr(s,'event',fail)
    with pytest.raises(RuntimeError):runner.tick(s,broker,run_daily=False)
    assert not s.orders()
    with s.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'paper_plan:%' OR key LIKE 'paper_controls:%'").fetchone()[0]==0


@pytest.mark.parametrize('value',[None,'not-a-date',float('nan')])
def test_invalid_timestamp_is_quote_deferral(value):
    with pytest.raises(QuoteUnavailable):executable_quote({'update_time':value,'bid_price':1,'ask_price':1.001},Settings(),now=OPEN)


def test_parallel_calendar_readers_dst_early_close_and_settlement():
    cal=us_calendar()
    def read(i):
        day='2026-11-27' if i%2 else '2026-11-09'
        return cal.session_open(day).isoformat(),cal.session_close(day).isoformat(),cal.next_session(day).date().isoformat()
    with ThreadPoolExecutor(max_workers=8) as pool:rows=list(pool.map(read,range(1600)))
    assert set(rows)=={('2026-11-27T14:30:00+00:00','2026-11-27T18:00:00+00:00','2026-11-30'),
                       ('2026-11-09T14:30:00+00:00','2026-11-09T21:00:00+00:00','2026-11-10')}
    with pytest.raises(TypeError):cal.bounds['2026-10-13']=(None,None)


def test_sleep_assertion_is_bound_to_this_service_not_an_unrelated_app(monkeypatch):
    from quant_agent import readiness
    monkeypatch.setattr(readiness.sys,'platform','darwin')
    monkeypatch.setattr(readiness.os,'getpid',lambda:444)
    monkeypatch.setattr(readiness.os,'getppid',lambda:1)
    text='''pid 445(caffeinate): [0x1] 00:01:00 PreventUserIdleSystemSleep named: "caffeinate command-line tool"
 Details: caffeinate asserting on behalf of '/python' (pid 444)
pid 8(powerd): [0x2] 00:00:20 PreventUserIdleSystemSleep named: "display is on"
'''
    monkeypatch.setattr(readiness.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout=text))
    assert readiness.power_guard()['active']
    text=text.replace('(pid 444)','(pid 777)')
    assert not readiness.power_guard()['active']


def test_cancel_uncertain_is_reconciled_then_retried_with_same_order_id(tmp_path,monkeypatch):
    s,p,ids=setup_open(tmp_path,monkeypatch);broker=OfflineBroker(p)
    s.reserve('expired',ids[0],'stock','US.AAA','BUY',1,100,'2026-10-12');s.claim('expired')
    identity=broker.submit(s.orders()[0]);s.submitted('expired',identity)
    attempts=[];cancel=broker.cancel
    def lost_ack(value):
        attempts.append(value)
        if len(attempts)==1:raise RemoteDisconnected('Offline cancel ack failure')
        return cancel(value)
    monkeypatch.setattr(broker,'cancel',lost_ack)
    runner.tick(s,broker,run_daily=False)
    assert s.meta('paused') and attempts==[identity]
    runner.tick(s,broker,run_daily=False)
    assert attempts==[identity]  # Bounded retry delay, no tight mutation loop.
    s.set_meta('cancel_attempts:expired',{'count':1,'last_at':'2026-10-13T13:28:00Z'})
    runner.tick(s,broker,run_daily=False)
    assert attempts==[identity,identity] and broker.rows[0]['status']==5
    assert broker.calls.count('submit')==1 and len(s.orders())==1
    assert s.meta('paused')  # A cancellation recovery must not clear a risk pause.


def test_wrong_quote_day_and_nan_metadata_cannot_trigger_orders_or_crash_tick(tmp_path,monkeypatch):
    s,p,_=setup_open(tmp_path,monkeypatch);broker=OfflineBroker(p);snapshots=broker.snapshots
    def old_day(symbols):
        return [{**q,'data_date':'2026-10-12','update_time':float('nan')} if q['code']=='US.AAA' else q for q in snapshots(symbols)]
    monkeypatch.setattr(broker,'snapshots',old_day)
    runner.tick(s,broker,run_daily=False)
    assert not s.orders() and not s.meta('paused')
    assert len(s.meta('last_tick')['strategy_checks'])==3
    assert all(r['status']=='blocked' for r in s.meta('last_tick')['strategy_checks'])


def test_launchagent_config_reload_waits_for_old_registration_to_disappear(tmp_path,monkeypatch):
    import plistlib
    from scripts import quant_agent_service as service
    monkeypatch.setattr(service.Path,'home',lambda:tmp_path)
    monkeypatch.setattr(service,'HOME',tmp_path/'runtime')
    monkeypatch.setattr(service.sys,'argv',['quant_agent_service.py','install'])
    monkeypatch.setattr(service.sys,'platform','darwin')
    service.HOME.mkdir();(service.HOME/'config.json').write_text('{}')
    path=tmp_path/'Library/LaunchAgents'/(service.LABEL+'.plist');path.parent.mkdir(parents=True)
    path.write_bytes(plistlib.dumps({'ProgramArguments':['old-python']}))
    calls=[];state={'retiring':False,'reads':0,'registered':True}
    def run(command,**kwargs):
        calls.append(command)
        if command[1]=='print':
            if state['retiring']:
                state['reads']+=1
                if state['reads']<3:return SimpleNamespace(returncode=0,stdout='state = exiting\nprogram = old-python',stderr='')
                state['registered']=False
            return SimpleNamespace(returncode=0 if state['registered'] else 113,stdout='state = running\nprogram = old-python',stderr='')
        if command[1]=='bootout':state['retiring']=True
        if command[1]=='bootstrap':state.update(retiring=False,registered=True)
        return SimpleNamespace(returncode=0,stdout='',stderr='')
    monkeypatch.setattr(service.subprocess,'run',run)
    monkeypatch.setattr(service.time,'sleep',lambda _:None)
    service.main()
    assert any(c[1]=='bootstrap' for c in calls)
    assert not any(c[1]=='kickstart' for c in calls)
    assert plistlib.loads(path.read_bytes())['ProgramArguments'][:2]==['/usr/bin/caffeinate','-i']


def test_bad_unused_rank_buffer_quote_does_not_block_selected_entries(tmp_path,monkeypatch):
    s,p,ids=setup_open(tmp_path,monkeypatch,positions=1);broker=OfflineBroker(p);snapshots=broker.snapshots
    def unused_bad(symbols):
        return [{**q,'ask_price':q['last_price']*2} if q['code']=='US.BBB' else q for q in snapshots(symbols)]
    monkeypatch.setattr(broker,'snapshots',unused_bad)
    runner.tick(s,broker,run_daily=False)
    assert {o['strategy'] for o in s.orders()}==set(ids)
    assert {o['symbol'] for o in s.orders()}=={'US.AAA'}
    assert not s.meta('paused')


def test_shadow_closing_quote_gap_does_not_pause_parallel_stock_execution(tmp_path,monkeypatch):
    s,p,ids=setup_open(tmp_path,monkeypatch);broker=OfflineBroker(p)
    spec=s.strategies()[0]['spec'];spec={**spec,'policy':{**spec['policy'],'name':'offline-observer'}}
    identity=s.propose(spec);evidence={**s.strategies()[0]['evidence'],'shadow_started_at':'2026-10-11T00:00:00Z'}
    for a,b in [('proposed','backtested'),('backtested','validated'),('validated','shadow')]:s.transition(identity,a,b,evidence)
    s.set_meta('shadow_pending:'+identity,{'session':'2026-10-12','orders':[],
        'state':{'cash':9000,'holdings':{'US.AAA':1},'peaks':{}},'data_hash':'offline-observed'})
    runner.tick(s,broker,run_daily=False)
    assert {o['strategy'] for o in s.orders()}==set(ids) and not s.meta('paused')
    status=next(r for r in s.meta('last_tick')['strategy_checks'] if r['id']==identity[:12])
    assert status['status']=='blocked' and 'closing marks' in status['reason']


def test_restart_recovers_committed_shadow_seal_without_duplicate_observation(tmp_path,monkeypatch):
    from quant_agent.config import canonical
    s,p,_=setup_open(tmp_path,monkeypatch);identity='offline-shadow'
    state={'cash':9000,'holdings':{'US.AAA':1},'peaks':{'US.AAA':200},'nav':9200,'order_count':0}
    s.set_meta('shadow_pending:'+identity,{'session':'2026-10-12','orders':[],
        'state':{'cash':9000,'holdings':{'US.AAA':1},'peaks':{}},'data_hash':'offline-observed'})
    with s.transaction() as db:
        db.execute('INSERT INTO shadow VALUES (?,?,?,?,?)',(identity,'2026-10-12','2026-10-12T20:05Z',canonical(state),'offline-observed'))
    runner.shadow_tick(s,{'id':identity},{},None)
    assert s.meta('shadow_pending:'+identity) is None and s.meta('shadow_state:'+identity)==state
    with s.transaction() as db:assert db.execute('SELECT COUNT(*) FROM shadow').fetchone()[0]==1


def test_quote_block_is_visible_separately_from_service_heartbeat(tmp_path,monkeypatch):
    s=Store(tmp_path,initialize(tmp_path,quote_push_enabled=False))
    s.set_meta('universe_scan',{'created_at':pd.Timestamp.now(tz='UTC').isoformat()})
    class Stop:
        cycles=0
        def is_set(self):return self.cycles>=2
        def wait(self,_):self.cycles+=1
    monkeypatch.setattr(runner,'activity',lambda *a,**k:{'mode':'market_open','session':'2026-10-13'})
    monkeypatch.setattr(runner,'daily_due',lambda _:False)
    monkeypatch.setattr(runner,'FutuSim',lambda _:object())
    monkeypatch.setattr('quant_agent.forecast_tracking.update_tracking',lambda _:None)
    monkeypatch.setattr(runner,'tick',lambda *a,**k:s.set_meta('last_tick',{'session':'2026-10-13','strategy_checks':[{'id':'fixture','stage':'paper','status':'blocked'}]}))
    runner.serve(s,Stop())
    assert s.meta('health')['status']=='ok' and s.meta('health')['execution_status']=='blocked'
    assert not s.meta('paused')


def test_new_shadow_with_expanded_members_can_resolve_its_own_cache(tmp_path,monkeypatch):
    from copy import deepcopy
    import numpy as np
    s,p,ids=setup_open(tmp_path,monkeypatch);broad=deepcopy(p)
    broad.symbols=p.symbols+['US.NEW']
    broad.stock_mask=np.append(p.stock_mask,True)
    broad.fields={k:np.column_stack([v,v[:,0]]) for k,v in p.fields.items()}
    record=deepcopy(next(r for r in s.strategies() if r['id']==ids[0]))
    record['id']='new-broad-shadow';record['evidence']['symbols']=broad.symbols
    record['evidence']['seed_hash']=data_prefix_hash(broad,record['evidence']['seed_asof'])
    s.set_meta('execution_cache:'+record['id'],'broad-cache')
    monkeypatch.setattr(runner,'load_panel',lambda directory:broad if directory=='broad-cache' else p)
    assert runner.execution_view(s,record,p).symbols==broad.symbols


def test_shadow_window_missed_retains_real_gate_reason(tmp_path,monkeypatch):
    s,p,ids=setup_open(tmp_path,monkeypatch);record=next(r for r in s.strategies() if r['id']==ids[0])
    record['evidence']['shadow_started_at']='2026-10-12T20:00Z'
    s.set_meta('shadow_outcome:2026-10-13:'+record['id'],{'status':'blocked','reason':'US.QMCO: Spread gate exceeded'})
    class AfterOpen(pd.Timestamp):
        @classmethod
        def now(cls,tz=None):return OPEN+pd.Timedelta(minutes=6)
    monkeypatch.setattr(runner,'pd',SimpleNamespace(Timestamp=AfterOpen,Timedelta=pd.Timedelta))
    for _ in range(2):
        result=runner.shadow_tick(s,record,{},'2026-10-13',p)
        assert result['status']=='missed_window' and 'QMCO' in result['reason']
    with s.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='shadow_window_missed'").fetchone()[0]==1
    assert not s.orders()
