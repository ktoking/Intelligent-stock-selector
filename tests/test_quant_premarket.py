"""Pre-open clock, evidence freshness, crash recovery and execution isolation."""
from copy import deepcopy
from types import SimpleNamespace
import json

import pandas as pd
import pytest

from quant_agent import premarket as pre
from quant_agent.config import initialize
from quant_agent.store import Store

SESSION='2026-10-21'
NOW='2026-10-21T13:20:00+00:00'


@pytest.fixture
def store(tmp_path):return Store(tmp_path,initialize(tmp_path,quote_push_enabled=False,evolution_enabled=False))


@pytest.mark.parametrize('at,expected',[
 ('2026-10-21T13:19:59Z',None),('2026-10-21T13:20Z','brief'),
 ('2026-10-21T13:29:59Z','brief'),('2026-10-21T13:30Z',None),
 ('2026-10-24T13:20Z',None),('2026-11-09T14:20Z','brief'),('2026-11-09T13:20Z',None)])
def test_preopen_window_uses_exchange_session_and_dst(store,at,expected):
    value=pre.due(store,at)
    assert (value['phase'] if value else None)==expected


def test_daily_brief_once_final_refresh_and_retry_cadence(store):
    store.set_meta('premarket_job:'+SESSION+':brief',{'status':'completed'})
    assert pre.due(store,'2026-10-21T13:25Z') is None
    assert pre.due(store,'2026-10-21T13:28Z')['phase']=='refresh'
    store.set_meta('premarket_job:'+SESSION+':refresh',{'status':'partial','retry_after':'2026-10-21T13:29Z'})
    assert pre.due(store,'2026-10-21T13:28:30Z') is None
    assert pre.due(store,'2026-10-21T13:29Z')['phase']=='refresh'
    store.set_meta('premarket_job:'+SESSION+':refresh',{'status':'completed'})
    assert pre.due(store,'2026-10-21T13:29:30Z') is None


def snapshot(now=NOW):
    return dict(code='US.QQQ',last_price=100,prev_close_price=99,data_date='2026-10-20',
        update_time=int(pd.Timestamp(now).timestamp()*1000),pre_price=101,pre_change_rate=1,pre_change_val=1,
        overnight_price=102,overnight_change_rate=2,overnight_change_val=2,
        after_price=100.5,after_change_rate=.5,after_change_val=.5)


def test_segments_never_reuse_regular_price_and_old_pre_fields_not_called_current():
    raw=snapshot();row=pre.quote_rows([raw],SESSION,NOW)[0]
    assert row['regular_price']==100 and row['segments']['pre']['price']==101
    assert row['segments']['pre']['status']=='indicative'
    assert not row['segments']['pre']['segment_timestamp_verified']
    assert row['segments']['overnight']['status']=='cached'
    raw.update(pre_price=99,pre_change_val=1)
    assert pre.quote_rows([raw],SESSION,NOW)[0]['segments']['pre']['status']=='reference_mismatch'
    for k in list(raw):
        if k.startswith(('pre_','overnight_','after_')):raw.pop(k)
    assert pre.quote_rows([raw],SESSION,NOW)[0]['segments']=={}


def test_old_or_future_snapshot_timestamp_does_not_assert_fresh():
    for at in ('2026-10-20T20:00Z','2026-10-21T14:00Z'):
        row=pre.quote_rows([snapshot(at)],SESSION,NOW)[0]
        assert row['segments']['pre']['status']=='cached' and not row['segments']['pre']['snapshot_recent']


def event(at='2026-10-21T14:00Z'):
    return {'event_text':'美国<em>CPI</em>','event_time':int(pd.Timestamp(at).timestamp()),
        'announce':'3.2%','predictive':'3.1%','previous':'3.0%','star':5,'country':'美国',
        'unique_id':'cpi-one','detail_url':'https://news.futunn.com/new-calendar/detail?id=1'}


def test_future_announcement_is_suppressed_and_release_is_not_backdated():
    row=pre.normalize_event(event(),SESSION,NOW)
    assert row['title']=='美国CPI' and row['status']=='scheduled' and row['actual'] is None
    row=pre.normalize_event(event('2026-10-21T12:30Z'),SESSION,NOW)
    assert row['actual']=='3.2%' and row['available_at']==NOW and row['status']=='released'
    assert pre.normalize_event(event('2026-10-20T12:30Z'),SESSION,NOW) is None


def test_calendar_paginates_and_marks_incomplete_coverage(store):
    calls=[]
    def request(method,path,query):
        calls.append(query)
        return {'ret_code':0,'data':[event()], 'pagination':{'has_more':True,'next_key':str(len(calls))}}
    rows=pre.collect_calendar(store,SESSION,SimpleNamespace(request=request),clock=lambda:NOW)
    assert len(calls)==3 and rows['truncated'] and len(rows['events'])==1
    assert calls[0]['date']=='20261021' and calls[0]['timezone']=='America/New_York' and calls[1]['next_key']=='1'
    with pytest.raises(ValueError,match='pagination stalled'):
        pre.collect_calendar(store,SESSION,SimpleNamespace(request=lambda *a:{'data':[],'pagination':{'has_more':True,'next_key':'same'}}),clock=lambda:NOW)


def collectors(calls,calendar_failure=False):
    def quotes():calls.append('quotes');return {'observed_at':NOW,'rows':pre.quote_rows([snapshot()],SESSION,NOW),'missing':[]}
    def news():calls.append('news');return {'observed_at':NOW,'items':[],'errors':[]}
    def calendar():
        calls.append('calendar')
        if calendar_failure:raise TimeoutError('public endpoint unavailable')
        return {'observed_at':NOW,'events':[pre.normalize_event(event(),SESSION,NOW)],'truncated':False}
    return dict(quotes=quotes,news=news,calendar=calendar)


def analysis(calls):
    def run(s,packet,timeout):
        calls.append('ai');assert packet['evidence']
        return {'status':'completed','input_asof':packet['asof'],'observed_at':NOW,'model':'fixture','result':{'headline':'观察条件', 'key_changes':[], 'opening_watch':[]}}
    return run


def test_partial_data_retries_resume_components_and_keep_ledger_unchanged(store):
    calls=[];cash=store.portfolio()['cash']
    first=pre.run(store,SESSION,clock=lambda:NOW,collectors=collectors(calls,True),analysis_fn=analysis(calls))
    assert first['status']=='partial' and 'calendar' in store.meta('premarket_job:'+SESSION+':brief')['errors']
    assert store.meta('premarket')['quotes']['rows'] and calls.count('ai')==1
    restored=Store(store.home,store.settings)
    recovered=pre.run(restored,SESSION,clock=lambda:'2026-10-21T13:21:01Z',collectors=collectors(calls),analysis_fn=analysis(calls))
    assert recovered['status']=='completed' and calls.count('quotes')==calls.count('news')==calls.count('ai')==1
    assert calls.count('calendar')==2 and not restored.meta('paused')
    assert restored.orders()==[] and restored.portfolio()['cash']==cash
    pre.run(restored,SESSION,clock=lambda:'2026-10-21T13:22Z',collectors=collectors(calls),analysis_fn=analysis(calls))
    assert calls.count('calendar')==2


def test_final_refresh_changes_facts_without_second_ai_call(store):
    calls=[]
    pre.run(store,SESSION,clock=lambda:NOW,collectors=collectors(calls),analysis_fn=analysis(calls))
    result=pre.run(store,SESSION,'refresh',clock=lambda:'2026-10-21T13:28Z',collectors=collectors(calls),analysis_fn=analysis(calls))
    assert calls.count('quotes')==2 and calls.count('ai')==1
    assert result['analysis']['input_asof']==NOW and result['phase']=='refresh'
    assert pre.opening_context(store,SESSION,cutoff='2026-10-21T13:30Z')['use']=='research_context_only'
    assert pre.opening_context(store,SESSION,cutoff='2026-10-21T13:27Z') is None


def test_preview_does_not_complete_real_schedule_or_authorize_order_context(store):
    calls=[]
    result=pre.run(store,SESSION,preview=True,clock=lambda:'2026-10-21T10:00Z',collectors=collectors(calls),analysis_fn=analysis(calls))
    assert result['preview'] and store.meta('premarket') is None
    assert store.meta('premarket_job:'+SESSION+':brief') is None and pre.opening_context(store,SESSION) is None
    assert pre.due(store,NOW)['phase']=='brief'
    assert pre.run(store,SESSION,clock=lambda:'2026-10-21T13:31Z')['status']=='outside_window'


def test_two_ai_failures_do_not_stall_monitoring_or_pause(store):
    calls=[]
    def fail(*a,**kw):calls.append('ai');raise RuntimeError('model unavailable')
    for at in (NOW,'2026-10-21T13:21Z','2026-10-21T13:22Z'):
        result=pre.run(store,SESSION,clock=lambda:at,collectors=collectors(calls),analysis_fn=fail)
    assert calls.count('ai')==2 and calls.count('quotes')==1 and store.meta('premarket')['analysis']['status']=='failed'
    assert not store.meta('paused') and store.meta('premarket_job:'+SESSION+':brief')['status']=='completed'


def test_analysis_rejects_unknown_evidence_and_suppresses_after_open_facts(store,monkeypatch):
    from quant_agent import ai
    monkeypatch.setattr(ai,'ask',lambda *a,**kw:{'result':{'key_changes':[{'evidence_ids':['made-up']}],'opening_watch':[]}})
    packet={'evidence':[]}
    with pytest.raises(ValueError,match='unknown evidence'):pre.analyze(store,packet)
    value={'session':SESSION,'opening':'2026-10-21T13:30Z','updated_at':'2026-10-21T13:31Z',
        'quotes':{'observed_at':'2026-10-21T13:30:01Z','rows':[{'symbol':'US.QQQ'}]},
        'calendar':{'events':[{'id':'late','available_at':'2026-10-21T13:31Z'}]}}
    assert pre.analysis_packet(value)['evidence']==[]


def test_optional_context_error_cannot_block_reservation(store,monkeypatch):
    from tests.test_quant_agent import paper
    ident=paper(store)
    def bad(*a,**kw):raise RuntimeError('optional report unavailable')
    monkeypatch.setattr(pre,'opening_context',bad)
    assert store.reserve('fixture',ident,'stock','US.TEST','BUY',1,100,SESSION)
    assert len(store.orders())==1 and not store.meta('paused')


def test_optional_worker_is_dispatched_after_execution_read_error(tmp_path,monkeypatch):
    from tests.test_quant_worker_isolation import supervisor_fixture
    from quant_agent import runner
    from quant_agent.broker import ReadUnavailable
    s,calls,stop=supervisor_fixture(tmp_path,monkeypatch,mode='pre_open')
    monkeypatch.setattr(pre,'due',lambda *a,**kw:{'session':SESSION,'phase':'brief'})
    monkeypatch.setattr(pre,'run',lambda *a,**kw:calls.append('premarket') or {'status':'completed'})
    def unavailable(*a,**kw):raise ReadUnavailable('network read')
    monkeypatch.setattr(runner,'tick',unavailable)
    runner.serve(s,stop)
    assert calls.count('premarket')==3 and not s.meta('paused')


def test_old_retry_does_not_overwrite_final_refresh_and_display_ages_quotes(store):
    calls=[]
    pre.run(store,SESSION,clock=lambda:NOW,collectors=collectors(calls,True),analysis_fn=analysis(calls))
    fresh=collectors(calls)
    fresh['quotes']=lambda:{'observed_at':'2026-10-21T13:28:00+00:00','rows':pre.quote_rows([snapshot('2026-10-21T13:28Z')],SESSION,'2026-10-21T13:28:00+00:00'),'missing':[]}
    pre.run(store,SESSION,'refresh',clock=lambda:'2026-10-21T13:28:00+00:00',collectors=fresh,analysis_fn=analysis(calls))
    value=pre.run(store,SESSION,clock=lambda:'2026-10-21T13:29:00+00:00',collectors=collectors(calls),analysis_fn=analysis(calls))
    assert value['quotes']['observed_at']=='2026-10-21T13:28:00+00:00'
    viewed=pre.view(store,{'now':'2026-10-21T13:35Z','next_open':'2026-10-22T13:30Z','next_session':'2026-10-22'})
    assert viewed['quotes']['rows'][0]['segments']['pre']['status']=='cached'
    assert value['quotes']['rows'][0]['segments']['pre']['status']=='indicative'


def test_research_registry_contains_premarket_citations_but_not_preview(store,monkeypatch):
    from quant_agent.briefing import review_packet,validate_brief
    from tests.test_quant_agent import panel
    from tests.test_quant_briefing import response
    calls=[];pre.run(store,SESSION,clock=lambda:NOW,collectors=collectors(calls),analysis_fn=analysis(calls))
    store.set_meta('brief_context',{'version':2,'technical_scan':{'candidates':[]},'evidence':[],'markets':[],'headlines':[]})
    packet=review_packet(store,panel=panel())
    assert packet['premarket_context']['use']=='research_context_only'
    assert 'quote:US.QQQ' in {r['id'] for r in packet['brief_context']['evidence']}
    result=response();result['result']['brief']['us_scan']={k:[] for k in ('bottom','catalyst','strong','avoid')}
    result['result']['brief']['key_events']=[{'evidence_ids':['quote:US.QQQ']}]
    validate_brief(result,packet)
    store.set_meta('premarket',{**store.meta('premarket'),'preview':True})
    assert pre.research_context(store) is None


def test_component_checkpoint_survives_interruption_mid_collection(store,monkeypatch):
    import threading
    saved=threading.Event();original=store.set_meta;calls=[]
    def set_meta(k,v,*args,**kwargs):
        original(k,v,*args,**kwargs)
        if k=='premarket_job:'+SESSION+':brief' and v.get('parts',{}).get('quotes'):saved.set()
    monkeypatch.setattr(store,'set_meta',set_meta)
    functions=collectors(calls)
    def crash():
        assert saved.wait(3)
        raise SystemExit('fixture process interruption')
    functions['calendar']=crash
    with pytest.raises(SystemExit):pre.run(store,SESSION,clock=lambda:NOW,collectors=functions,analysis_fn=analysis(calls))
    assert 'quotes' in store.meta('premarket_job:'+SESSION+':brief')['parts']
    restored=Store(store.home,store.settings)
    pre.run(restored,SESSION,clock=lambda:'2026-10-21T13:21Z',collectors=collectors(calls),analysis_fn=analysis(calls))
    assert calls.count('quotes')==1 and not restored.meta('paused')


def test_interrupted_last_model_attempt_is_not_left_running_forever(store):
    calls=[]
    pre.run(store,SESSION,clock=lambda:NOW,collectors=collectors(calls),analysis_fn=analysis(calls))
    value=store.meta('premarket');value['analysis']={'status':'running','attempts':2}
    store.set_meta('premarket_session:'+SESSION,value)
    job=store.meta('premarket_job:'+SESSION+':brief');job['status']='running';store.set_meta('premarket_job:'+SESSION+':brief',job)
    def forbidden(*a,**kw):raise AssertionError('third model request')
    result=pre.run(store,SESSION,clock=lambda:'2026-10-21T13:21Z',collectors=collectors(calls),analysis_fn=forbidden)
    assert result['analysis']['status']=='failed' and not store.meta('paused')


def test_inflight_final_refresh_uses_last_published_order_evidence(store):
    calls=[]
    value=pre.run(store,SESSION,clock=lambda:NOW,collectors=collectors(calls),analysis_fn=analysis(calls))
    first=pre.opening_context(store,SESSION,cutoff='2026-10-21T13:30Z')
    store.set_meta('premarket_session:'+SESSION,{**value,'status':'collecting','updated_at':'2026-10-21T13:29Z'})
    assert pre.opening_context(store,SESSION,cutoff='2026-10-21T13:30Z')==first
