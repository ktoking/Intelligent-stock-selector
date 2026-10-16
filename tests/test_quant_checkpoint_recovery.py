"""Recovery behavior, including real process exit, not a simulated completion flag."""
from dataclasses import replace
from pathlib import Path
import json
import os
import subprocess
import sys
import time
from urllib.error import HTTPError

import pytest

from quant_agent.config import initialize,canonical
from quant_agent.store import Store
from quant_agent.checkpoints import run_step,retry_failed
from quant_agent import runner


def make_store(path):return Store(path,initialize(path,quote_push_enabled=False,evolution_enabled=False))


def test_process_exit_preserves_completed_result_and_recovers_running_step(tmp_path):
    script='''
import os,sys
from pathlib import Path
from quant_agent.config import initialize
from quant_agent.store import Store
from quant_agent.checkpoints import run_step
home=Path(sys.argv[1]);s=Store(home,initialize(home))
def first():
 (home/'first-called').write_text('once')
 return {'proof':'persisted-before-exit'}
run_step(s,'first','2026-10-15','data-v1',first)
def crash():os._exit(23)
run_step(s,'second','2026-10-15','data-v1',crash)
'''
    result=subprocess.run([sys.executable,'-c',script,str(tmp_path)],cwd=Path(__file__).parents[1],capture_output=True,text=True)
    assert result.returncode==23
    s=Store(tmp_path,initialize(tmp_path))
    def forbidden():raise AssertionError('Completed action must not run again')
    assert run_step(s,'first','2026-10-15','data-v1',forbidden)=={'proof':'persisted-before-exit'}
    assert run_step(s,'second','2026-10-15','data-v1',lambda:{'resumed':True})=={'resumed':True}
    assert s.meta('task:second')['attempt']==2 and s.meta('task:second')['recovered_interruption']
    assert s.orders()==[]


def test_checkpoint_backoff_version_binding_and_failure_evidence(tmp_path):
    from quant_agent.checkpoints import StepDeferred
    s=make_store(tmp_path);calls=[]
    def fail():calls.append(1);raise TimeoutError('sensitive provider text is not a diagnostic')
    with pytest.raises(TimeoutError):run_step(s,'review','2026-10-15','v1',fail)
    with pytest.raises(StepDeferred):run_step(s,'review','2026-10-15','v1',fail)
    assert len(calls)==1
    retry_failed(s,'review')
    assert run_step(s,'review','2026-10-15','v1',lambda:42)==42
    assert run_step(s,'review','2026-10-15','v2',lambda:43)==43
    assert s.meta('task:review')['attempt']==1
    with s.read() as db:assert db.execute("SELECT COUNT(*) FROM checkpoints WHERE status='completed'").fetchone()[0]==2


def test_readers_do_not_take_a_writer_lock(tmp_path):
    s=make_store(tmp_path);s.set_meta('committed',123)
    with s.transaction() as db:
        db.execute("INSERT INTO meta VALUES ('not_committed','true')")
        start=time.monotonic()
        assert s.meta('committed')==123 and s.meta('not_committed') is None
        assert s.strategies()==[] and s.orders()==[]
        assert time.monotonic()-start<1


def test_http_logging_keeps_status_and_context_without_credentials(tmp_path,monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from quant_agent.broker import FutuSim,ReadUnavailable
    from quant_agent.observability import configure
    configure(tmp_path)
    settings=replace(initialize(tmp_path),stock_account='PRIVATE_ACCOUNT')
    b=FutuSim(settings);b.key=Ed25519PrivateKey.generate()
    def fail(*a,**kw):raise HTTPError('https://example.invalid/?token=PRIVATE_TOKEN',403,'PRIVATE_BODY',{},None)
    monkeypatch.setattr('urllib.request.urlopen',fail)
    with pytest.raises(ReadUnavailable):b.state()
    text=(tmp_path/'logs/runtime.jsonl').read_text()
    assert not any(x in text for x in ('PRIVATE_ACCOUNT','PRIVATE_TOKEN','PRIVATE_BODY','Authorization','BEGIN PRIVATE KEY'))
    row=[json.loads(line) for line in text.splitlines() if json.loads(line)['event']=='http_failed'][0]
    assert row['http_status']==403 and row['elapsed_ms']>=0 and row['request_id']
    assert row['route']=='/api/v1.0/sim-trade/{account}/cash-info'
    assert (tmp_path/'logs/runtime.jsonl').stat().st_mode&0o777==0o600


def test_research_scheduler_failure_cannot_pause_execution(tmp_path,monkeypatch):
    s=make_store(tmp_path);calls=[]
    class Stop:
        n=0
        def is_set(self):return self.n>=3
        def wait(self,_):self.n+=1
    monkeypatch.setattr(runner,'activity',lambda *a,**kw:{'mode':'startup_reconcile'})
    monkeypatch.setattr(runner,'tick',lambda *a,**kw:calls.append(1))
    monkeypatch.setattr(runner,'FutuSim',lambda _:object())
    def bad(*a):raise ValueError('broken optional research schedule')
    monkeypatch.setattr(runner,'daily_due',bad)
    monkeypatch.setattr('quant_agent.forecast_tracking.update_tracking',lambda s:None)
    runner.serve(s,Stop())
    assert len(calls)==3 and not s.meta('paused') and s.meta('health')['status']=='ok'
    assert s.meta('component:research_scheduler')['status']=='degraded'


def test_acknowledgement_recovery_never_clears_manual_or_risk_pause(tmp_path):
    from quant_agent.recovery import resume_confirmed_acknowledgement
    from tests.test_quant_agent import paper
    s=make_store(tmp_path);owner=paper(s)
    s.reserve('intent',owner,'stock','US.AAA','BUY',1,100,'2026-10-15');s.claim('intent');s.unknown('intent')
    assert not resume_confirmed_acknowledgement(s)
    s.submitted('intent','broker-known');s.fill('intent',1,99,'filled')
    s.pause('Risk breach')
    assert not resume_confirmed_acknowledgement(s) and s.meta('paused')
    s.pause('Order acknowledgement',category='order_acknowledgement',recoverable=True,orders=['intent'])
    assert resume_confirmed_acknowledgement(s) and not s.meta('paused')
    s.pause('Missing acknowledgement owner',category='order_acknowledgement',recoverable=True,orders=['missing'])
    assert not resume_confirmed_acknowledgement(s) and s.meta('paused')


def test_option_ack_loss_preserves_recoverable_pause_and_reconciles_once(tmp_path,monkeypatch):
    from tests.test_quant_supervisor_mock import rehearsal_store
    from scripts.mock_quant_opening import run_case
    from quant_agent.broker import FutuSim
    s=rehearsal_store(tmp_path);original=FutuSim.submit;lost=[]
    def submit(self,order):
        result=original(self,order)
        if order['bucket']=='option' and not lost:
            lost.append(order['id']);raise ConnectionError('Acknowledgement lost after broker accepted')
        return result
    monkeypatch.setattr(FutuSim,'submit',submit)
    result=run_case(tmp_path,s.settings,'option_eligible',http_mock=True)
    assert result['option_orders']==1 and lost and not result['paused']
    assert any(e['kind']=='acknowledgement_recovered' for e in result['events'])


def test_corrupt_old_factor_artifact_does_not_block_current_recovery(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from quant_agent import checkpoints,panel_cache
    s=make_store(tmp_path);directory=tmp_path/'artifacts';directory.mkdir()
    (directory/'evolution-corrupt.json').write_text('{partial')
    calls=[]
    monkeypatch.setattr(panel_cache,'load_panel',lambda _:SimpleNamespace(metadata={'input_sha256':'current'}))
    monkeypatch.setattr(checkpoints,'recover_unpublished_factor_code',lambda *a:calls.append(1))
    checkpoints.recover_factor_result(s)
    assert calls==[1] and s.meta('evolution_completed:current') is None
    with s.read() as db:assert db.execute("SELECT COUNT(*) FROM events WHERE kind='factor_artifact_read_failed'").fetchone()[0]==1


def test_validated_stage_resumes_after_crash_without_retesting(tmp_path,monkeypatch):
    from tests.test_quant_agent import panel
    from quant_agent import pipeline
    from quant_agent.config import digest
    from research.daily_factor_lab.paper import data_prefix_hash
    import numpy as np
    import pandas as pd
    s=make_store(tmp_path);p=panel()
    p.fields={k:np.tile(v,(2,1)) for k,v in p.fields.items()}
    p.dates=pd.bdate_range(end='2026-10-15',periods=720)
    p.metadata={'input_sha256':digest(p.fields['close'].tolist()),'end':'2026-10-15'}
    spec={'policy':{'name':'checkpoint-fixture'},'research_data_sha256':p.metadata['input_sha256'],'engine_sha256':pipeline.engine_hash()}
    key=s.propose(spec)
    protocol={'data':p.metadata,'engine_sha256':spec['engine_sha256']}
    path=tmp_path/'protocol.json';path.write_text(canonical(protocol))
    evidence={'passed':True,'independent_ledger':{k:True for k in ('train','validation','retrospective_test')},'protocol':str(path),'protocol_sha256':digest(protocol)}
    s.transition(key,'proposed','backtested',evidence);s.transition(key,'backtested','validated',evidence)
    monkeypatch.setattr(pipeline,'load_panel',lambda _:p)
    pipeline.evaluate(s)
    r=next(r for r in s.strategies() if r['id']==key)
    assert r['stage']=='shadow' and r['evidence']['seed_hash']==data_prefix_hash(p,'2026-10-15')
    assert s.orders()==[]


def test_report_failure_reuses_saved_ai_result_on_restart(tmp_path,monkeypatch):
    from tests.test_quant_agent import daily_fixture
    from quant_agent import briefing
    s=make_store(tmp_path);daily_fixture(s,monkeypatch);calls=[]
    response={'provider':'codex','model':'offline','result':{'review':'archived analysis','factor_ideas':[],'candidates':[]}}
    monkeypatch.setattr(runner,'ask',lambda *a,**kw:calls.append(1) or response)
    def fail(*a):raise RuntimeError('Report publication interrupted')
    monkeypatch.setattr(briefing,'persist_brief',fail)
    with pytest.raises(RuntimeError):runner.daily_jobs(s,{})
    assert len(calls)==1 and s.meta('task:review')['status']=='completed'
    restored=Store(s.home,s.settings)
    monkeypatch.setattr(briefing,'persist_brief',lambda *a:None)
    runner.daily_jobs(restored,{})
    assert len(calls)==1 and restored.meta('daily_completed:2026-10-09')


def test_malformed_success_response_and_business_rejection_are_read_failures(tmp_path):
    from quant_agent.broker import FutuSim,ReadUnavailable
    settings=replace(initialize(tmp_path),stock_account='offline')
    b=FutuSim(settings,transport=lambda *a:{'ret_code':0,'data':{}})
    with pytest.raises(ReadUnavailable,match='schema'):b.state()
    b=FutuSim(settings,transport=lambda *a:{'ret_code':-999,'ret_msg':'PRIVATE_BODY'})
    with pytest.raises(ReadUnavailable) as exc:b.snapshots(['US.QQQ'])
    assert exc.value.read_failure_kind=='business_rejection' and 'PRIVATE_BODY' not in str(exc.value)


def test_partial_history_download_resumes_only_missing_symbols(tmp_path,monkeypatch):
    from tests.test_quant_supervisor_mock import rehearsal_store
    from quant_agent import market_data
    from research.daily_factor_lab import refresh as history
    import pandas as pd
    s=rehearsal_store(tmp_path);source=Path(s.settings.data_dir);s.set_meta('current_data_dir',str(source))
    output=tmp_path/'market-data'/'2026-10-13-partial';calls=[]
    def bars(symbol):
        old=pd.read_pickle(source/(symbol[3:]+'.pkl')).iloc[-1].to_dict();old['date']=int(old['date']);new={**old,'date':20261013}
        return {'ret_code':0,'data':{'kline_list':[old,new]}}
    def interrupted(path,query):
        symbol=path.split('/')[-2];calls.append(symbol)
        if len(calls)==2:raise SystemExit(23)
        return bars(symbol)
    with pytest.raises(SystemExit):history.refresh(source,output,'2026-10-13',fetch=interrupted,sleep=lambda _:None)
    progress=json.loads((output/'refresh_report.json').read_text())
    assert [r['symbol'] for r in progress['success']]==['US.AAA']
    resumed=[]
    class Broker:
        def __init__(self,_):pass
        def request(self,method,path,query):
            symbol=path.split('/')[-2];resumed.append(symbol);return bars(symbol)
    monkeypatch.setattr(market_data,'FutuSim',Broker);monkeypatch.setattr(market_data,'latest_completed_session',lambda:'2026-10-13')
    market_data._refresh_cache(s)
    assert sorted(resumed)==['US.BBB','US.QQQ'] and s.meta('execution_data_blocked') is None


def test_failed_shadow_group_does_not_stop_stock_or_option_data_publication(tmp_path,monkeypatch):
    import shutil
    import pandas as pd
    from tests.test_quant_supervisor_mock import rehearsal_store
    from quant_agent import market_data,pipeline
    from research.daily_factor_lab.core import load_panel
    from research.daily_factor_lab.paper import data_prefix_hash
    s=rehearsal_store(tmp_path);source=Path(s.settings.data_dir);alternate=tmp_path/'alternate';latest=tmp_path/'latest'
    shutil.copytree(source,alternate);shutil.copytree(source,latest)
    frame=pd.read_pickle(alternate/'AAA.pkl');frame.loc[0,'open']+=.01;frame.to_pickle(alternate/'AAA.pkl')
    old=load_panel(alternate)
    for name in latest.glob('*.pkl'):
        frame=pd.read_pickle(name);last=frame.iloc[[-1]].copy();last['date']=20261013;pd.concat([frame,last],ignore_index=True).to_pickle(name)
    record=s.strategies(['paper'])[0];spec={**record['spec'],'research_data_sha256':old.metadata['input_sha256']}
    key=s.propose(spec);e={**record['evidence'],'seed_hash':data_prefix_hash(old,old.metadata['end'])}
    for a,b in [('proposed','backtested'),('backtested','validated'),('validated','shadow')]:s.transition(key,a,b,e)
    s.set_meta('execution_cache:'+key,str(alternate));s.set_meta('execution_data_dir',str(source))
    monkeypatch.setattr(market_data,'latest_completed_session',lambda:'2026-10-13')
    assert not market_data.synchronize_execution_cache(s,latest)
    assert not s.meta('paused')
    assert load_panel(s.meta('execution_data_dir')).metadata['end']=='2026-10-13'
    assert load_panel(s.meta('option_data_dir')).metadata['end']=='2026-10-13'
    assert s.meta('execution_cache:'+key)==str(alternate)
    assert s.meta('execution_data_blocked')['groups'][0]['strategies']==[key]


def test_expired_shadow_observation_is_archived_without_forward_credit(tmp_path):
    from tests.test_quant_supervisor_mock import rehearsal_store
    s=rehearsal_store(tmp_path);record=s.strategies(['paper'])[0]
    with s.transaction() as db:db.execute("UPDATE strategies SET stage='shadow' WHERE id=?",(record['id'],))
    record=next(r for r in s.strategies() if r['id']==record['id'])
    pending={'session':'2000-01-03','data_hash':'expired-fixture','state':{'holdings':{'US.AAA':1},'cash':9000},'orders':[]}
    s.set_meta('shadow_pending:'+record['id'],pending)
    result=runner.shadow_tick(s,record,{},None)
    assert result['status']=='expired' and s.meta('shadow_pending:'+record['id']) is None
    assert s.meta('shadow_expired:'+record['id']+':2000-01-03')==pending
    with s.read() as db:assert db.execute('SELECT COUNT(*) FROM shadow').fetchone()[0]==0
    assert next(r for r in s.strategies() if r['id']==record['id'])['stage']=='rejected'


def test_unknown_transition_rolls_back_with_pause_but_uncertain_intent_still_fences_orders(tmp_path,monkeypatch):
    from tests.test_quant_agent import paper
    s=make_store(tmp_path);owner=paper(s)
    s.reserve('intent',owner,'stock','US.AAA','BUY',1,100,'2026-10-15');s.claim('intent')
    original=s.event
    def fail(kind,*a,**kw):
        if kind=='pause':raise RuntimeError('Commit interrupted')
        return original(kind,*a,**kw)
    monkeypatch.setattr(s,'event',fail)
    with pytest.raises(RuntimeError):s.unknown('intent')
    assert not s.meta('paused') and s.orders()[0]['status']=='submitting'
    with pytest.raises(ValueError,match='Unresolved'):s.reserve('second',owner,'stock','US.BBB','BUY',1,100,'2026-10-15')


def test_corrupt_research_artifact_is_visible_without_breaking_dashboard(tmp_path):
    from tests.test_quant_supervisor_mock import rehearsal_store
    from quant_agent.web import public_status
    s=rehearsal_store(tmp_path);directory=tmp_path/'artifacts';directory.mkdir(exist_ok=True)
    (directory/'evolution-corrupt.json').write_text('{partial')
    data=public_status(s)
    assert data['recovery']['artifact_errors']==[{'file':'evolution-corrupt.json','error_type':'JSONDecodeError'}]
    assert data['research_evidence']['artifact_errors']


def test_failed_news_refresh_does_not_relabel_old_evidence_as_fresh(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from quant_agent import news
    from quant_agent.broker import ReadUnavailable
    s=make_store(tmp_path);old='2026-10-14T20:00:00+00:00'
    s.set_meta('news_packet',{'observed_at':old,'items':[]})
    def unavailable(*a,**kw):raise ReadUnavailable('News unavailable')
    monkeypatch.setattr(news,'FutuSim',lambda _:SimpleNamespace(request=unavailable))
    monkeypatch.setattr(news.requests,'get',unavailable)
    with pytest.raises(ReadUnavailable):news.refresh_news(s,['US.AAA'])
    value=s.meta('news_packet')
    assert value['observed_at']==old and value['attempted_at']!=old and value['successful_reads']==0


def test_held_option_exit_continues_with_valid_quote_when_other_stock_mark_is_missing(tmp_path,monkeypatch):
    from tests.test_quant_shared_options import shared
    from research.daily_factor_lab.core import load_panel
    from quant_agent import broker as broker_module
    import pandas as pd
    s,b=shared(tmp_path);owner=s.meta('option_paper_strategy')['id'];code='US.NVDA261030C220000'
    s.reserve('held-option',owner,'option',code,'BUY',1,6.25,'2026-10-12',100);s.claim('held-option');s.submitted('held-option','held-broker');s.fill('held-option',1,6.25,'filled')
    s.set_meta('option_contracts',{code:{'underlying':'US.NVDA','type':'CALL','expiry':'2026-10-30'}})
    b.option_capacity=lambda *a:{'max_cash_buy_qty_round_lot':'0','max_sell_qty_round_lot':'1'}
    p=load_panel(s.settings.data_dir)
    monkeypatch.setattr(runner,'latest_completed_session',lambda:p.metadata['end'])
    monkeypatch.setattr(broker_module,'session_now',lambda *a:'2026-10-13')
    from types import SimpleNamespace
    now=pd.Timestamp('2026-10-13T13:31:00Z')
    monkeypatch.setattr(broker_module,'datetime',SimpleNamespace(now=lambda *_:now.to_pydatetime()))
    quote={'last_price':3,'bid_price':3,'ask_price':3.01,'update_time':int(now.timestamp()*1000),
           'data_date':'2026-10-13','option_valid':True,'option_contract_multiplier':100,'option_contract_size':100}
    runner._held_option_exit_during_quote_gap(s,b,p,{code:quote},'2026-10-13')
    assert len(b.submits)==1 and b.submits[0]['side']=='SELL' and b.submits[0]['qty']==1
    assert sum(o['side']=='BUY' for o in s.orders())==1  # Only the original held position.


def test_cancel_retry_cap_cools_down_then_reuses_verified_working_order(tmp_path,monkeypatch):
    from tests.test_quant_opening_resilience import setup_open,OfflineBroker
    s,p,ids=setup_open(tmp_path,monkeypatch);b=OfflineBroker(p)
    s.reserve('expired',ids[0],'stock','US.AAA','BUY',1,100,'2026-10-12');s.claim('expired')
    broker_id=b.submit(s.orders()[0]);s.submitted('expired',broker_id)
    s.set_meta('cancel_attempts:expired',{'count':3,'last_at':'2026-10-13T13:20:00Z'})
    s.pause('Cancellation transport uncertain',category='cancellation',recoverable=True,orders=['expired'])
    runner.tick(s,b,run_daily=False)
    assert b.calls.count('submit')==1 and b.rows[0]['status']==5
    assert s.meta('cancel_attempts:expired')['count']==1 and s.meta('cancel_attempts:expired')['round']==1


@pytest.mark.parametrize('partial',[False,True])
def test_unpublished_factor_source_recovers_or_is_quarantined_without_execution(tmp_path,monkeypatch,partial):
    from types import SimpleNamespace
    from quant_agent import config,pipeline,evolution
    from quant_agent.checkpoints import recover_unpublished_factor_code
    s=make_store(tmp_path);monkeypatch.setattr(config,'ROOT',tmp_path)
    expression={'op':'rank','x':{'field':'returns'}}
    spec={'policy':{'name':'fixture'},'factor_expression':expression,'factor_name':'fixture','hypothesis':'fixture','falsification':'fixture',
          'research_data_sha256':'v1'};spec['engine_sha256']=pipeline.engine_hash(spec)
    key=s.propose(spec);path=tmp_path/'research/evolved_factors'/('factor_'+key[:12]+'.py');path.parent.mkdir(parents=True)
    source='''"""Generated factor candidate; live eligibility requires fresh validation."""
from quant_agent.evolution import expression_score

FACTOR_EXPRESSION = {'x': {'field': 'returns'}, 'op': 'rank'}

def score(fields, stock_mask=None):
    return expression_score(FACTOR_EXPRESSION, fields, stock_mask)
'''
    path.write_text('FACTOR_EXPRESSION = {' if partial else source)
    monkeypatch.setattr(evolution,'causal_checks',lambda *a:{'fixture_checks':True})
    recover_unpublished_factor_code(s,SimpleNamespace(metadata={'input_sha256':'v1'}))
    if partial:
        assert not path.exists() and list((tmp_path/'failed-candidate-code').glob('*.py'))
        assert s.meta('evolution_completed:v1') is None
    else:
        assert path.read_text()==source and s.meta('evolution_completed:v1')['strategy']==key
        assert (tmp_path/'artifacts'/('evolution-'+key[:16]+'.json')).exists()
    assert s.strategies()[0]['stage']=='proposed' and s.orders()==[]
