"""Strict admission and session confirmation. Offline fixtures, no broker calls."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
import json

import pandas as pd
import pytest

from quant_agent import account_selection as selector
from quant_agent.champion import queue_rotation,rotation_tick,pending
from quant_agent.config import digest
from tests.test_quant_champion import configured


class Memory:
    def __init__(self):self.data={'promotion_policy':{'confirmation_sessions':3},'account_champion':'daily'}
    def meta(self,k,default=None):return self.data.get(k,default)
    def set_meta(self,k,v):self.data[k]=v


def records():
    return {k:{'id':k,'spec':{'execution_timeframe':tf,'policy':{'name':k,'positions':2},
        'engine_sha256':'same','minute_family':k},'evidence':{'symbols':['US.QQQ']}}
        for k,tf in [('daily','1d'),('minute','5m'),('other','1d')]}


def rank(leader='minute'):
    return [{'strategy':k,'eligible':True,'return_pct':10-i,'score':i} for i,k in enumerate([leader]+[k for k in records() if k!=leader])]


def choose(store,day,ranking=None,values=None):
    with patch.object(selector,'latest_completed_session',return_value=day):
        return selector.confirmation(store,values or records(),ranking or rank(),day)


def test_three_completed_sessions_survive_restart_and_ignore_repeated_scan():
    s=Memory()
    chosen,c=choose(s,'2026-10-16');assert chosen['strategy']=='daily' and c['sessions']==1
    for _ in range(5):assert choose(s,'2026-10-16')[1]['sessions']==1
    restored=Memory();restored.data=deepcopy(s.data)
    assert choose(restored,'2026-10-19')[1]['sessions']==2  # weekend does not reset
    winner,c=choose(restored,'2026-10-20');assert winner['strategy']=='minute' and c['sessions']==3


def test_lost_lead_or_missing_session_resets_confirmation():
    s=Memory();choose(s,'2026-10-16')
    assert choose(s,'2026-10-20')[1]['sessions']==1  # Monday not evaluated
    choose(s,'2026-10-21',rank('daily'))
    assert choose(s,'2026-10-22')[1]['sessions']==1
    choose(s,'2026-10-23',rank('other'))
    assert choose(s,'2026-10-26')[1]['sessions']==1


def test_exact_tie_retains_incumbent_but_lower_robust_score_does_not_override_return():
    s=Memory();ranking=rank();ranking[0]['return_pct']=ranking[1]['return_pct']
    assert choose(s,'2026-10-16',ranking)[0]['strategy']=='daily'
    for day in ['2026-10-19','2026-10-20','2026-10-21']:winner,c=choose(s,day)
    assert winner['strategy']=='minute' and winner['score']==0


def test_no_valid_incumbent_first_admission_does_not_wait():
    s=Memory();s.set_meta('account_champion',None)
    winner,c=choose(s,'2026-10-16')
    assert winner['strategy']=='minute' and c['status']=='initial_admission'


def test_refresh_keeps_same_rule_streak_and_rule_change_resets():
    s=Memory();choose(s,'2026-10-16');r=records();r['minute']['spec']['research_data_sha256']='new-data'
    r['minute']['spec']['minute_factor']={'expression':'a','base_family':'trend_follow','code_path':'one'}
    # Formula change restarts, but file paths and dataset identities don't.
    assert choose(s,'2026-10-19',values=r)[1]['sessions']==1
    r['minute']['spec']['minute_factor']['code_path']='two'
    assert choose(s,'2026-10-20',values=r)[1]['sessions']==2


def test_future_or_incomplete_session_cannot_increment():
    s=Memory()
    with patch.object(selector,'latest_completed_session',return_value='2026-10-16'):
        with pytest.raises(ValueError,match='latest completed'):selector.confirmation(s,records(),rank(),'2026-10-19')
    assert 'champion_confirmation' not in s.data


def test_activation_supersedes_old_admission_preserves_fills_cash_and_budget(tmp_path):
    s,old,new,b=configured(tmp_path,held=True);prior=pending(s);cash=s.portfolio()['cash'];orders=s.orders()
    budget=s.settings.stock_budget;selector.activate(s,3)
    assert s.meta('champion_rotation')['status']=='superseded'
    assert s.meta('superseded_rotation:'+prior['id'])['id']==prior['id']
    assert s.orders()==orders and s.portfolio()['cash']==cash and s.settings.stock_budget==budget
    assert not s.meta('paused') and s.portfolio()['positions']
    assert queue_rotation(s,{'champion':new,'data_sha256':'unified-proof-missing'},'legacy') is None
    assert queue_rotation(s,{'champion':None,'data_sha256':'minute-no-winner'},'legacy') is None
    with pytest.raises(ValueError,match='champion|Strict unified'):
        s.reserve('forbidden',old['id'],'stock','US.AAA','BUY',1,100,'2026-10-13')
    assert not b.submits


def test_old_reserved_buy_cannot_submit_after_strict_activation(tmp_path):
    s,old,new,b=configured(tmp_path)
    # A plan persisted before the new policy must also be fenced at claim.
    with s.transaction() as db:
        db.execute("INSERT INTO orders(id,strategy,bucket,symbol,side,qty,price,multiplier,fee,status,created,session) VALUES (?,?, 'stock','US.AAA','BUY',1,100,1,.3,'reserved','2026-10-13','2026-10-13')",('old-buy',old['id']))
    selector.activate(s)
    assert not s.claim('old-buy')
    assert rotation_tick(s,b,{},'2026-10-13') and pending(s)['status']=='blocked'
    assert s.orders()[0]['status']=='cancelled' and not b.submits


def test_failed_experiment_is_ineligible_even_with_continuous_paper(tmp_path):
    s,old,new,b=configured(tmp_path);r=next(r for r in s.strategies() if r['id']==new)
    assert selector.qualified(r)
    r['evidence'].update(paper_experiment=True,passed=False)
    assert not selector.qualified(r)
    r['evidence'].update(paper_experiment=False,passed=True)
    r['evidence']['independent_ledger']['train']=False
    assert not selector.qualified(r)


def test_minute_raw_adjusted_basis_mismatch_stays_shadow():
    panel=SimpleNamespace(symbols=['US.QQQ'],dates=pd.DatetimeIndex(['2026-10-16']),
        fields={k:[[100.]] for k in ('open','high','low','close')})
    import numpy as np
    panel.fields={k:np.array(v) for k,v in panel.fields.items()}
    data={'bars':{'US.QQQ':[dict(session='2026-10-16',open=100.,high=100.,low=100.,close=100.)]}}
    windows={'train':['2026-10-16','2026-10-16'],'retrospective_test':['2026-10-16','2026-10-16']}
    selector.minute_basis(panel,data,windows)
    data['bars']['US.QQQ'][0]['close']=101
    with pytest.raises(ValueError,match='同价核验'):selector.minute_basis(panel,data,windows)


def test_daily_champion_executes_while_minute_research_enabled(tmp_path):
    from quant_agent import runner
    s,old,new,b=configured(tmp_path);s.set_meta('intraday_enabled',True);selector.activate(s)
    s.set_meta('champion_rotation',None);s.set_meta('account_champion',old['id'])
    # Reaching panel validation proves daily execution no longer returns legacy_hold.
    with patch.object(runner,'checked_panel',side_effect=RuntimeError('reached-daily-path')),patch.object(runner.pd,'Timestamp',wraps=pd.Timestamp) as stamp:
        stamp.now.return_value=pd.Timestamp('2026-10-13T13:31:00Z')
        with pytest.raises(RuntimeError,match='reached-daily-path'):runner.paper_tick(s,b,old,{},'2026-10-13')


def test_shared_screen_calls_both_engines_on_identical_dates_and_costs(tmp_path,monkeypatch):
    from quant_agent import engine,intraday_engine
    from quant_agent.panel_cache import load_panel
    s,old,new,b=configured(tmp_path);selector.activate(s);panel=load_panel(s.settings.data_dir)
    asof=panel.metadata['end'];r=next(r for r in s.strategies() if r['id']==new)
    spec=deepcopy(r['spec']);spec.update(execution_timeframe='5m',minute_family='opening_range')
    from quant_agent.intraday import engine_hash
    spec['engine_sha256']=engine_hash()
    minute=s.propose(spec)
    for before,after in [('proposed','backtested'),('backtested','validated'),('validated','shadow')]:s.transition(minute,before,after,r['evidence'])
    calls=[]
    def run(kind,days,cost,delay):
        calls.append((kind,days,cost,delay))
        metric={'return_pct':40 if kind=='5m' else 20,'max_drawdown_pct':1.,'orders':20,'trading_days':len(days),'sharpe':0.}
        return {'metrics':metric,'independent_ledger':True,'curve':[],'orders':[]}
    def daily(p,policy,start,end,cost,signal_lag=1):return run('1d',[str(d.date()) for d in panel.dates if start<=str(d.date())<=end],cost,signal_lag)
    def minute_replay(data,family,days,cost,delay_bars=0,signal_fn=None):return run('5m',days,cost,delay_bars)
    monkeypatch.setattr(selector,'latest_completed_session',lambda:asof)
    monkeypatch.setattr(selector,'qualified',lambda r:True)
    monkeypatch.setattr(selector,'_daily',lambda *a:(panel,None))
    data={'bars':{'US.QQQ':[{'session':str(d.date())} for d in panel.dates]}}
    monkeypatch.setattr(selector,'minute_view',lambda *a:data)
    monkeypatch.setattr(selector,'minute_basis',lambda *a:None)
    monkeypatch.setattr(engine,'simulate',daily);monkeypatch.setattr(intraday_engine,'replay',minute_replay)
    monkeypatch.setattr(__import__('scripts.verify_daily_factor_run',fromlist=['audit_ledger']),'audit_ledger',lambda *a:True)
    result=selector.screen(s,panel)
    assert result['champion']==minute and result['ranking'][0]['timeframe']=='5m'
    assert result['cost_bps']==s.settings.fee_reserve_bps
    for label,w in result['windows'].items():
        wanted=[str(d.date()) for d in panel.dates if w[0]<=str(d.date())<=w[1]]
        assert any(c[:3]==('1d',wanted,30) for c in calls) and any(c[:3]==('5m',wanted,30) for c in calls)
    assert selector.authorized_screen(s,result)
    from quant_agent.research_evidence import append_research_review
    from pathlib import Path
    (Path(s.settings.data_dir)/'completed.json').write_text(json.dumps({'input_sha256':panel.metadata['input_sha256']}))
    (s.home/'reports').mkdir(exist_ok=True)
    report=s.home/'reports'/(asof+'.md');report.write_text('# Fixture daily review')
    append_research_review(s,asof)
    assert '统一比较回顾收益' in report.read_text()
    assert not b.submits and not s.orders()
    # Exercise the actual funding transaction and reservation/claim fences.
    assert not rotation_tick(s,b,{},'2026-10-13')
    assert s.meta('account_champion')==minute
    s.reserve('strict-buy',minute,'stock','US.AAA','BUY',1,100,'2026-10-13')
    assert s.claim('strict-buy')
    assert s.portfolio()['cash']>=0
