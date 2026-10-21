"""Persistent execution read failures cannot starve independent research workers."""
from types import SimpleNamespace

import pytest

from quant_agent import runner
from quant_agent.broker import AccountIntegrityError, QuoteUnavailable, ReadUnavailable
from quant_agent.config import initialize
from quant_agent.store import Store, utcnow


def supervisor_fixture(tmp_path, monkeypatch, *, mode='market_open'):
    from quant_agent import forecast_tracking, intraday, news, universe

    store=Store(tmp_path,initialize(tmp_path,quote_push_enabled=False,evolution_enabled=False))
    store.set_meta('intraday_enabled',True)
    calls=[]

    class ImmediateThread:
        def __init__(self, target, args=(), **kwargs):
            self.target,self.args=target,args
        def start(self):self.target(*self.args)
        def is_alive(self):return False

    class Stop:
        cycles=0
        def is_set(self):return self.cycles>=3
        def wait(self, _):self.cycles+=1

    def scan(*args, **kwargs):
        calls.append('radar');store.set_meta('universe_scan',{'created_at':utcnow()})

    def refresh_news(*args):
        calls.append('news');store.set_meta('news_packet',{'observed_at':utcnow()})

    def daily_jobs(*args):
        calls.append('research');store.set_meta('test_research_completed',True)

    monkeypatch.setattr(runner.threading,'Thread',ImmediateThread)
    monkeypatch.setattr(runner,'activity',lambda *args,**kwargs:{'mode':mode,'session':'2026-10-19' if mode=='market_open' else None})
    monkeypatch.setattr(runner,'FutuSim',lambda _:SimpleNamespace(snapshots=lambda symbols:[]))
    monkeypatch.setattr(runner,'daily_due',lambda s:not s.meta('test_research_completed',False))
    monkeypatch.setattr(runner,'daily_jobs',daily_jobs)
    monkeypatch.setattr(forecast_tracking,'update_tracking',lambda *args:None)
    monkeypatch.setattr(universe,'scan',scan)
    monkeypatch.setattr(news,'refresh_news',refresh_news)
    monkeypatch.setattr(intraday,'observe',lambda *args:calls.append('minute'))
    return store,calls,Stop()


@pytest.mark.parametrize('error',[QuoteUnavailable,ReadUnavailable,AccountIntegrityError])
@pytest.mark.parametrize('risk_paused',[False,True])
def test_open_market_workers_continue_after_persistent_execution_failure(tmp_path,monkeypatch,error,risk_paused):
    store,calls,stop=supervisor_fixture(tmp_path,monkeypatch)
    if risk_paused:store.pause('Existing risk breach',category='risk')
    original_policy=store.meta('pause_policy')

    def broken_tick(*args,**kwargs):
        calls.append('tick');raise error('Controlled execution failure')

    monkeypatch.setattr(runner,'tick',broken_tick)
    runner.serve(store,stop)
    assert calls.count('tick')==3
    assert calls.count('minute')==3
    assert calls.count('radar')==calls.count('news')==calls.count('research')==1
    assert store.meta('health')['status']==('error' if error is AccountIntegrityError else 'degraded')
    assert store.meta('component:execution_reads')['failures']==3
    assert store.orders()==[]
    if risk_paused:
        assert store.meta('paused') and store.meta('pause_policy')==original_policy
        assert store.meta('pause_reason')=='Existing risk breach'
    else:
        assert store.meta('paused')==(error is AccountIntegrityError)


def test_post_close_research_runs_when_account_read_is_unavailable(tmp_path,monkeypatch):
    store,calls,stop=supervisor_fixture(tmp_path,monkeypatch,mode='daily_research')

    def broken_tick(*args,**kwargs):raise ReadUnavailable('Controlled read outage')

    monkeypatch.setattr(runner,'tick',broken_tick)
    runner.serve(store,stop)
    assert calls.count('research')==1
    assert not any(c in calls for c in ['radar','news','minute'])
    assert store.meta('health')['status']=='degraded'
    assert not store.meta('paused') and not store.orders()


def test_execution_recovery_clears_incident_without_clearing_risk_pause(tmp_path,monkeypatch):
    store,calls,stop=supervisor_fixture(tmp_path,monkeypatch)
    store.pause('Existing risk breach',category='risk')

    def tick(*args,**kwargs):
        calls.append('tick')
        if calls.count('tick')<3:raise QuoteUnavailable('Controlled stale quote')
        return {}

    monkeypatch.setattr(runner,'tick',tick)
    runner.serve(store,stop)
    assert calls.count('minute')==3 and calls.count('research')==1
    assert store.meta('component:execution_reads')['status']=='ok'
    assert store.meta('health')['status']=='ok'
    assert store.meta('health')['execution_status']=='paused'
    assert store.meta('pause_reason')=='Existing risk breach'
    assert not store.orders()


def test_idle_heartbeat_cannot_report_unrecovered_execution_as_healthy(tmp_path,monkeypatch):
    store,calls,stop=supervisor_fixture(tmp_path,monkeypatch)
    monkeypatch.setattr(runner,'activity',lambda *args,**kwargs:{'mode':'market_open' if stop.cycles==0 else 'idle'})

    def broken_tick(*args,**kwargs):raise AccountIntegrityError('Controlled account mismatch')

    monkeypatch.setattr(runner,'tick',broken_tick)
    runner.serve(store,stop)
    assert store.meta('health')['mode']=='idle'
    assert store.meta('health')['status']=='error'
    assert store.meta('health')['error_type']=='AccountIntegrityError'
    assert store.meta('paused') and not store.orders()
