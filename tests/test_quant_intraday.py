"""Minute time causality, next-open accounting and mid-session broker-paper fences."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json

import pandas as pd
import pytest

from quant_agent import intraday_data as data,intraday_engine as engine,intraday
from quant_agent.config import canonical,digest


def session_rows(day='2026-10-16'):
    cal=data.us_calendar();opening=cal.session_open(day);rows=[]
    for i,t in enumerate(pd.date_range(opening+pd.Timedelta(minutes=5),cal.session_close(day),freq='5min')):
        # First 15m range then rising, liquid breakout. Last bars turn lower.
        p=100+i*.08 if i<45 else 103.6-(i-45)*.12
        rows.append({'time_key':int(t.timestamp()*1000),'date':int(day.replace('-','')),
            'open':p-.01,'high':p+.04,'low':p-.04,'close':p,'volume':1000 if i<5 else 1500,'turnover':p*1500})
    return rows


def test_close_time_grid_completed_only_and_raw_ohlcv_validation():
    rows=session_rows();normalized=data.normalize(rows,now='2026-10-16T13:42:00Z')
    assert [r['end_ms'] for r in normalized]==[r['time_key'] for r in rows[:2]]
    bad=deepcopy(rows);bad[0]['high']=1
    with pytest.raises(ValueError,match='OHLCV'):data.normalize(bad,now='2026-10-17T00:00Z')
    with pytest.raises(ValueError,match='Conflicting'):data.normalize(rows+[dict(rows[0],close=100.01)],now='2026-10-17T00:00Z')
    complete=data.normalize(rows,now='2026-10-17T00:00Z')
    assert len(data.complete_days(complete)['2026-10-16'])==78
    assert data.complete_days(complete[:-1])=={}


def test_futu_minute_end_is_next_date_and_truncation_is_not_silent():
    calls=[]
    def request(method,path,query):
        calls.append(query);return {'data':{'kline_list':session_rows()}}
    result=data.fetch(SimpleNamespace(request=request),'US.QQQ','2026-10-16','2026-10-16',now='2026-10-17T00:00Z')
    assert len(result)==78 and calls[0]['end']=='2026-10-17' and calls[0]['ktype']==6 and calls[0]['autype']==0
    with pytest.raises(ValueError,match='truncated'):
        data.fetch(SimpleNamespace(request=lambda *a:{'data':{'kline_list':session_rows(),'next_time':1}}),'US.QQQ','2026-10-16','2026-10-16')


def test_intraday_replay_fills_next_open_and_independent_cash_ledger():
    rows=data.normalize(session_rows(),now='2026-10-17T00:00Z');value={'bars':{'US.QQQ':rows}}
    run=engine.replay(value,'opening_range',['2026-10-16'])
    assert run['trades'] and run['independent_ledger']
    first=run['trades'][0];signal_index=next(i for i,r in enumerate(rows) if r['end_ms']==first['time_ms'])
    assert first['price']==rows[signal_index+1]['open'] and signal_index>=5
    assert run['trades'][-1]['side']=='SELL' and run['curve'][-1]['equity']<10000*1.03
    delayed=engine.replay(value,'opening_range',['2026-10-16'],delay_bars=1)
    assert delayed['trades'][0]['time_ms']>=first['time_ms']+300000
    assert sum(t['qty']*(1 if t['side']=='BUY' else -1) for t in run['trades'])==0
    assert len(run['minute_curve'])==78
    assert run['metrics']['max_drawdown_pct']>=engine.metrics(run['curve'],run['trades'])['max_drawdown_pct']


def test_signal_prefix_does_not_depend_on_later_candles():
    rows=data.normalize(session_rows(),now='2026-10-17T00:00Z');prefix=deepcopy(rows[:12])
    before={f:engine.decision(prefix,f) for f in engine.FAMILIES}
    for row in rows[12:]:row.update(close=1,high=1,low=1,open=1,volume=99999999)
    assert before=={f:engine.decision(rows[:12],f) for f in engine.FAMILIES}


def minute_fixture(tmp_path,monkeypatch):
    from tests.test_quant_shared_options import shared
    from quant_agent.champion import rotation_tick
    from quant_agent import broker
    s,b=shared(tmp_path);rotation_tick(s,b,{},'2026-10-13');owner=s.meta('account_champion')
    record=next(r for r in s.strategies() if r['id']==owner)
    record['spec'].update(execution_timeframe='5m',minute_family='opening_range',minute_cost_bps=s.settings.fee_reserve_bps)
    record['evidence']['symbols']=['US.AAA']
    now=pd.Timestamp('2026-10-13T14:00:20Z')
    monkeypatch.setattr(intraday,'verify',lambda r:{})
    monkeypatch.setattr(intraday.engine,'decision',lambda *a,**k:'BUY')
    class Timestamp(pd.Timestamp):
        @classmethod
        def now(cls,**kwargs):return now
    monkeypatch.setattr(intraday,'pd',SimpleNamespace(Timestamp=Timestamp,Timedelta=pd.Timedelta))
    monkeypatch.setattr(intraday,'executable_quote',lambda *a:(99.99,100.01))
    s.set_meta('intraday_observation',{'status':'ok','session':'2026-10-13','boundary':'2026-10-13T14:00:00Z','bars':{'US.AAA':[{'close':100}]}})
    return s,b,record


def test_mid_session_5m_order_uses_existing_ledger_and_never_resubmits(tmp_path,monkeypatch):
    s,b,record=minute_fixture(tmp_path,monkeypatch)
    outcome=intraday.execute(s,b,record,{'US.AAA':{}},'2026-10-13')
    assert outcome['orders']==1 and len(b.submits)==1 and s.orders()[0]['bucket']=='stock'
    assert s.meta('minute_order:'+s.orders()[0]['id'])['signal_close']=='2026-10-13T14:00:00Z'
    intraday.execute(s,b,record,{'US.AAA':{}},'2026-10-13')
    assert len(b.submits)==1


def test_mid_session_risk_pause_is_not_bypassed(tmp_path,monkeypatch):
    s,b,record=minute_fixture(tmp_path,monkeypatch);s.pause('Risk breach',category='risk')
    assert intraday.execute(s,b,record,{},'2026-10-13')['status']=='paused'
    assert b.submits==[] and s.orders()==[]


@pytest.mark.parametrize('lost',[False,True])
def test_mid_session_signed_http_fill_and_lost_ack_recovery(tmp_path,monkeypatch,lost):
    from contextlib import ExitStack
    from dataclasses import replace
    from scripts.mock_quant_opening import Wire,http_adapter
    from research.daily_factor_lab.core import load_panel
    from quant_agent.runner import reconcile
    s,_,record=minute_fixture(tmp_path,monkeypatch)
    s.settings=replace(s.settings,stock_account='mock-opening')
    s.set_meta('bound_account',digest(['mock-opening',100]))
    wire=Wire(s.settings,load_panel(s.settings.data_dir),SimpleNamespace(now=pd.Timestamp('2026-10-13T14:00:20Z')),
        'accepted_ack_lost' if lost else 'normal')
    with ExitStack() as stack:
        broker=http_adapter(wire,stack)(s.settings);broker.verify()
        if lost:
            with pytest.raises(Exception):intraday.execute(s,broker,record,{'US.AAA':{}},'2026-10-13')
            assert s.meta('paused') and s.orders()[0]['status']=='unknown'
        else:intraday.execute(s,broker,record,{'US.AAA':{}},'2026-10-13')
        wire.advance();wire.advance();reconcile(s,broker)
        assert len(wire.rows)==1 and len(s.orders())==1 and s.orders()[0]['status']=='filled'
        assert s.portfolio()['positions']['US.AAA']['qty']==s.orders()[0]['qty']
        assert sum(c['method']=='POST' and c['route']=='orders' for c in wire.calls)==1


def test_missing_unheld_minute_name_does_not_block_another_signal(tmp_path,monkeypatch):
    s,b,record=minute_fixture(tmp_path,monkeypatch)
    record['evidence']['symbols']=['US.MISSING','US.AAA']
    observed=s.meta('intraday_observation');observed['status']='partial';s.set_meta('intraday_observation',observed)
    intraday.execute(s,b,record,{'US.AAA':{}},'2026-10-13')
    assert len(b.submits)==1 and b.submits[0]['symbol']=='US.AAA' and not s.meta('paused')


def test_cancelled_partial_minute_exit_reserves_only_remaining_shares(tmp_path,monkeypatch):
    s,b,record=minute_fixture(tmp_path,monkeypatch)
    intraday.execute(s,b,record,{'US.AAA':{}},'2026-10-13');buy=s.orders()[-1]
    s.fill(buy['id'],buy['qty'],100.,'filled')
    observed=s.meta('intraday_observation');observed['bars']['US.AAA']=[{'close':90}];s.set_meta('intraday_observation',observed)
    intraday.execute(s,b,record,{'US.AAA':{}},'2026-10-13');sell=s.orders()[-1]
    s.fill(sell['id'],sell['qty']//2,90.,'cancelled')
    intraday.execute(s,b,record,{'US.AAA':{}},'2026-10-13');remaining=s.orders()[-1]
    assert remaining['side']=='SELL' and remaining['id']!=sell['id']
    assert remaining['qty']==sell['qty']-sell['qty']//2
    assert remaining['qty']==s.portfolio()['positions']['US.AAA']['qty']


def test_minute_dataset_hash_is_independent_from_daily_strategy_hash(tmp_path):
    from quant_agent.pipeline import engine_hash
    assert engine_hash({'execution_timeframe':'5m'})==intraday.engine_hash()
    path=tmp_path/'data.json';path.write_text(canonical({'symbols':['US.QQQ']}));key=digest({'symbols':['US.QQQ']})
    assert data.load(path,key)['symbols']==['US.QQQ']
    path.write_text('{}')
    with pytest.raises(ValueError,match='changed'):data.load(path,key)


def test_risk_pause_is_recorded_once_and_preserves_original_stop(tmp_path):
    from quant_agent.config import initialize
    from quant_agent.store import Store
    s=Store(tmp_path,initialize(tmp_path));s.pause('Risk breach 2.1%',category='risk')
    s.pause('Risk breach 2.2%',category='risk')
    assert s.meta('pause_reason')=='Risk breach 2.1%' and s.meta('latest_risk_breach')['reason']=='Risk breach 2.2%'
    with s.read() as db:assert db.execute("SELECT COUNT(*) FROM events WHERE kind='pause'").fetchone()[0]==1


def test_minute_mandate_does_not_keep_opening_old_daily_positions(tmp_path):
    from tests.test_quant_supervisor_mock import rehearsal_store
    from quant_agent.runner import paper_tick
    s=rehearsal_store(tmp_path);s.set_meta('intraday_enabled',True)
    record=s.strategies(['paper'])[0]
    assert paper_tick(s,object(),record,{},'2026-10-13')['status']=='legacy_hold'
    assert s.orders()==[]


def test_rolling_minute_history_reuses_aligned_windows(tmp_path,monkeypatch):
    from quant_agent.config import initialize
    from quant_agent.store import Store
    s=Store(tmp_path,initialize(tmp_path));calls=[]
    def fake(broker,symbol,start,end,now=None):
        calls.append((start,end));rows=[]
        for day in data.us_calendar().sessions_in_range(start,end):
            rows.extend(data.normalize(session_rows(str(day.date())),now='2026-10-17T00:00Z'))
        return rows
    monkeypatch.setattr(data,'fetch',fake)
    data.collect(s,['US.QQQ'],'2026-10-01','2026-10-08',broker=object(),delay=0)
    first=len(calls)
    data.collect(s,['US.QQQ'],'2026-10-02','2026-10-09',broker=object(),delay=0)
    assert len(calls)-first<=1
    before=len(calls)
    data.collect(s,['US.QQQ'],'2026-10-02','2026-10-09',broker=object(),delay=0)
    assert len(calls)==before


def test_minute_observation_consumes_only_new_completed_bars_without_orders(tmp_path,monkeypatch):
    from quant_agent.config import initialize
    from quant_agent.store import Store
    import quant_agent.broker as broker_module
    s=Store(tmp_path,initialize(tmp_path));clock=SimpleNamespace(now=pd.Timestamp('2026-10-16T14:00:20Z'));calls=[]
    class Timestamp(pd.Timestamp):
        @classmethod
        def now(cls,**kwargs):return clock.now
    fake_pd=SimpleNamespace(**{k:getattr(pd,k) for k in dir(pd) if not k.startswith('__')});fake_pd.Timestamp=Timestamp
    monkeypatch.setattr(intraday,'pd',fake_pd)
    monkeypatch.setattr(broker_module,'session_now',lambda:'2026-10-16')
    monkeypatch.setattr(intraday,'select_universe',lambda *_:['US.QQQ'])
    def request(*args):calls.append(args);return {'data':{'kline_list':session_rows()}}
    monkeypatch.setattr(intraday,'FutuSim',lambda _:SimpleNamespace(request=request))
    intraday.observe(s)
    observed=s.meta('intraday_observation');assert len(observed['bars']['US.QQQ'])==6 and len(observed['signals'])==3
    clock.now+=pd.Timedelta(minutes=1);intraday.observe(s);assert len(calls)==1
    clock.now+=pd.Timedelta(minutes=4);intraday.observe(s);assert len(calls)==2
    assert len(s.meta('intraday_observation')['bars']['US.QQQ'])==7 and s.orders()==[]
