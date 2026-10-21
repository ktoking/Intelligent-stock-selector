"""Real incident regressions: changing heartbeat cannot create new AI retries."""
from datetime import datetime,timezone,timedelta
import json

import pytest

from quant_agent import runner,checkpoints
from quant_agent.config import initialize
from quant_agent.store import Store
from tests.test_quant_agent import daily_fixture


def test_review_retries_keep_one_input_and_one_checkpoint_despite_new_health_time(tmp_path,monkeypatch):
    store=Store(tmp_path,initialize(tmp_path,daily_research=False,auto_refresh=False,evolution_enabled=False))
    daily_fixture(store,monkeypatch);calls=[]
    def unavailable(*a,**kw):calls.append(a);raise TimeoutError('controlled model failure')
    monkeypatch.setattr(runner,'ask',unavailable)
    store.set_meta('health',{'time':'2026-10-10T01:00Z','status':'ok'})
    runner.daily_jobs(store,{})
    with store.read() as db:
        first=[dict(r) for r in db.execute("SELECT * FROM checkpoints WHERE job='review'")]
    assert len(first)==1 and len(calls)==1
    # Simulate a scheduler wake while the same input is still backing off.
    # Only clear the outer scheduling hint, not the durable step's deadline.
    store.set_meta('daily_retry_after:2026-10-09',None)
    store.set_meta('health',{'time':'2026-10-10T01:01Z','status':'ok'})
    store.set_meta('option_paper_status',{'observed_at':'2026-10-10T01:01Z','status':'closed'})
    runner.daily_jobs(store,{})
    with store.read() as db:
        second=[dict(r) for r in db.execute("SELECT * FROM checkpoints WHERE job='review'")]
    assert second==first and len(calls)==1
    assert store.meta('task:review')['status']=='waiting'
    assert store.meta('task:review')['retry_after']==first[0]['retry_after']
    assert len(list((tmp_path/'artifacts').glob('review-input-*.json')))==1
    assert len(list((tmp_path/'artifacts').glob('daily-*.json')))==1


def test_waiting_step_restores_its_real_deadline_without_false_failure_event(tmp_path):
    store=Store(tmp_path,initialize(tmp_path))
    def fail():raise ValueError('fixture')
    with pytest.raises(ValueError):checkpoints.run_step(store,'fixture','2026-10-20','v1',fail)
    previous=store.meta('task:fixture')
    with pytest.raises(checkpoints.StepDeferred):
        with runner.job_trace(store,'fixture','2026-10-20'):
            checkpoints.run_step(store,'fixture','2026-10-20','v1',fail)
    state=store.meta('task:fixture')
    assert state['status']=='waiting' and state['retry_after']==previous['retry_after']
    assert state['checkpoint']==previous['checkpoint'] and state['attempt']==1


def test_settlement_reason_survives_closed_session(tmp_path,monkeypatch):
    from tests.test_quant_champion import configured
    from quant_agent import champion
    store,old,new,broker=configured(tmp_path)
    rotation=store.meta('champion_rotation');rotation.update(status='settling',reason='waiting settlement')
    store.set_meta('champion_rotation',rotation)
    store.set_meta('unsettled',[{'bucket':'stock','amount':100,'settles':'2099-10-21T13:30:00+00:00'}])
    champion.rotation_tick(store,broker,{},None)
    assert store.meta('champion_rotation')['status']=='settling'
    assert 'T+1' in store.meta('champion_rotation')['reason']
    assert '2099-10-21' in store.meta('champion_rotation')['reason']
    assert not broker.submits


def test_review_backoff_reuses_completed_minute_research(tmp_path,monkeypatch):
    from tests.test_quant_timeframe_coexistence import configured
    store,calls=configured(tmp_path,monkeypatch)
    attempts=[]
    def offline(*a,**kw):attempts.append(1);raise TimeoutError('controlled')
    monkeypatch.setattr(runner,'ask',offline)
    runner.daily_jobs(store,{})
    store.set_meta('daily_retry_after:2026-10-12',None)
    store.set_meta('health',{'time':'changed heartbeat'})
    runner.daily_jobs(store,{})
    assert attempts==[1] and calls.count('minute_backtest')==1
    assert store.meta('task:review')['status']=='waiting'


@pytest.mark.parametrize('text,code',[
    ('refresh token has expired','authentication'),('Usage limit reached','rate_limit'),
    ('context_length_exceeded','context_limit'),('invalid_json_schema','schema'),
    ('failed to connect to upstream','network'),('other failure','cli_exit')])
def test_cli_failure_diagnostics_are_classified_without_retaining_raw_text(text,code):
    from quant_agent.ai import codex_failure_code
    assert codex_failure_code(text)==code
