"""Daily and minute research coexist without granting a second account owner."""
from dataclasses import replace

import pytest

from quant_agent import briefing,evolution,intraday,minute_factors,promotion,runner
from quant_agent.config import digest
from quant_agent.research_evidence import daily_cycle_version,research_pending,research_summary
from tests.test_quant_promotion import slot


def configured(tmp_path,monkeypatch,fail=None):
    store,old,new,_=slot(tmp_path)
    store.settings=replace(store.settings,daily_research=True,evolution_enabled=True,auto_refresh=False)
    store.set_meta('intraday_enabled',True)
    store.set_meta('daily_completed:2026-10-12',True)
    store.set_meta('research_cycle_completed:2026-10-12',str(store.settings.data_dir))
    store.set_meta('universe_scanned:2026-10-12',True)
    store.set_meta('news_completed:2026-10-12',True)
    store.set_meta('champion_rotation',{'target':'unchanged-minute-candidate','status':'queued','order_ids':[]})
    calls=[];panel=runner.load_panel(store.settings.data_dir)
    report={'asof':'2026-10-12','data_sha256':'minute-fixture','engine_sha256':intraday.engine_hash()}
    store.set_meta('intraday_research',report)
    packet={'created':'fixture','strategies':[],'orders':[],'paused':False,'news':[],'data':panel.metadata}
    response={'model':'offline','result':{'review':'fixture review','factor_ideas':['separate daily/minute hypothesis']}}
    def minute_research(*a,**kw):
        calls.append('minute_backtest')
        if fail=='minute':raise ValueError('controlled minute failure')
    def propose(*a,**kw):
        calls.append('daily_discovery')
        if fail=='daily':raise ValueError('controlled daily failure')
    def daily_factor(*a):
        calls.append('daily_factor');return {'strategy':new,'name':'daily-fixture','hypothesis':'daily only'}
    def minute_factor(*a):
        calls.append('minute_factor')
        store.set_meta('minute_factor_cycle:2026-10-12',digest([report['data_sha256'],intraday.engine_hash(),'minute-factor-v1']))
        return {'name':'minute-fixture','status':'rejected','hypothesis':'minute only','stats':{}}
    monkeypatch.setattr(runner,'latest_completed_session',lambda:'2026-10-12')
    monkeypatch.setattr(runner,'propose',propose)
    monkeypatch.setattr(runner,'evaluate',lambda *a:calls.append('daily_backtest'))
    monkeypatch.setattr(runner,'ask',lambda *a,**kw:response)
    monkeypatch.setattr(briefing,'review_packet',lambda *a,**kw:dict(packet))
    monkeypatch.setattr(briefing,'validate_brief',lambda *a:None)
    monkeypatch.setattr(briefing,'persist_brief',lambda *a:None)
    monkeypatch.setattr(evolution,'evolve',daily_factor)
    monkeypatch.setattr(intraday,'research',minute_research)
    monkeypatch.setattr(intraday,'view',lambda *a:{'enabled':True})
    monkeypatch.setattr(minute_factors,'evolve',minute_factor)
    monkeypatch.setattr(promotion,'plan_promotions',lambda *a:pytest.fail('Daily research must not replace the queued account owner'))
    return store,calls


def test_finished_minute_cycle_cannot_hide_unrun_daily_cycle(tmp_path,monkeypatch):
    store,calls=configured(tmp_path,monkeypatch)
    assert research_pending(store,'2026-10-12')
    before=(store.meta('champion_rotation'),store.meta('paper_allocations'))
    runner.daily_jobs(store,{})
    assert {'daily_discovery','daily_backtest','daily_factor','minute_backtest','minute_factor'}<=set(calls)
    assert store.meta('daily_research_cycle:2026-10-12')==daily_cycle_version(store)
    assert store.meta('daily_completed:2026-10-12') and not research_pending(store,'2026-10-12')
    assert before==(store.meta('champion_rotation'),store.meta('paper_allocations')) and not store.orders()
    keys={s['key'] for s in research_summary(store,'2026-10-12')['steps']}
    assert {'backtest','evolution','factor_validation','minute_backtest','minute_factor_validation'}<=keys
    count=len(calls);runner.daily_jobs(store,{})
    assert len(calls)==count  # Completed coexistence cycle is not repeated.


@pytest.mark.parametrize('fail',['daily','minute'])
def test_failure_in_one_research_lane_does_not_skip_the_other(tmp_path,monkeypatch,fail):
    store,calls=configured(tmp_path,monkeypatch,fail)
    runner.daily_jobs(store,{})
    assert {'daily_discovery','daily_backtest','daily_factor','minute_backtest','minute_factor'}<=set(calls)
    assert not store.meta('daily_completed:2026-10-12')
    assert bool(store.meta('daily_research_cycle:2026-10-12'))==(fail!='daily')
    assert not store.orders() and store.meta('champion_rotation')['target']=='unchanged-minute-candidate'


def test_daily_checkpoint_version_ignores_minute_refresh(tmp_path,monkeypatch):
    store,_=configured(tmp_path,monkeypatch)
    before=daily_cycle_version(store)
    store.set_meta('intraday_research',{'data_sha256':'another-minute-data-version'})
    assert daily_cycle_version(store)==before
