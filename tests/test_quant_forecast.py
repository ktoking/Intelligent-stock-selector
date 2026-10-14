"""Forecast evidence dates, benchmark consistency and isolation from real records."""
from copy import deepcopy
import json

import exchange_calendars as xc
import numpy as np
import pytest

from quant_agent.config import initialize
from quant_agent.forecast import forecast_data, future_sessions, project
from quant_agent.schedule import calendar_state
from quant_agent.store import Store


def chart():
    days = [str(d.date()) for d in xc.get_calendar('XNYS').sessions_in_range('2026-07-01','2026-10-09')]
    x = .0005+.012*np.sin(np.arange(len(days))*1.3)
    y = .6*x+.005*np.cos(np.arange(len(days))*.9)
    return {'data_hash':'frozen','source':'Futunn frozen daily research',
            'curve':[{'date':d,'equity':float(v)} for d,v in zip(days,10000*np.exp(np.cumsum(y)))],
            'benchmark_curve':[{'date':d,'equity':float(v)} for d,v in zip(days,10000*np.exp(np.cumsum(x)))]}


def test_forecast_has_seven_exchange_sessions_anchored_to_the_actual_last_nav():
    c=chart();original=deepcopy(c);f=project(c,'2026-10-09','2026-10-10')
    assert f['available'] and f['kind']=='statistical_forecast'
    assert f['future_dates']==['2026-10-12','2026-10-13','2026-10-14','2026-10-15','2026-10-16','2026-10-19','2026-10-20']
    for key in ('strategy_curve','benchmark_curve'):
        rows=f[key]
        assert len(rows)==8 and rows[0]['return_pct']==0
        assert rows[0]['equity']==c['curve' if key=='strategy_curve' else 'benchmark_curve'][-1]['equity']
        assert all(r['low_equity']<=r['equity']<=r['high_equity'] and r['low_equity']>0 for r in rows)
    assert c==original and project(c,'2026-10-09','2026-10-10')==f
    assert '未校准' in f['interval']['label'] and '未来样本验证' in f['limitation']


def test_same_qqq_input_produces_same_benchmark_forecast_across_different_strategies():
    a=chart();b=deepcopy(a)
    for p in b['curve']:p['equity']=10000**2/p['equity']
    first=project(a,'2026-10-09','2026-10-10');second=project(b,'2026-10-09','2026-10-10')
    assert first['benchmark_curve']==second['benchmark_curve']
    assert first['strategy_curve']!=second['strategy_curve']
    # Rows after the forecast origin cannot change its training or outcome.
    b=deepcopy(a)
    for key in ('curve','benchmark_curve'):b[key].append({'date':'2026-10-12','equity':1e9})
    assert project(b,'2026-10-09','2026-10-10')==first


def test_cash_only_strategy_cannot_gain_predicted_market_profit():
    c=chart()
    for p in c['curve']:p['equity']=10000
    f=project(c,'2026-10-09','2026-10-10')
    assert all(r['equity']==10000 and r['low_return_pct']==r['high_return_pct']==0 for r in f['strategy_curve'])


def test_stale_short_invalid_and_missing_session_evidence_do_not_get_a_forecast():
    c=chart();c['curve'].pop()
    assert not project(c,'2026-10-09','2026-10-10')['available']
    c=chart();c['curve']=c['curve'][-10:]
    assert not project(c,'2026-10-09','2026-10-10')['available']
    c=chart();c['curve'].pop(-4)
    assert not project(c,'2026-10-09','2026-10-10')['available']
    c=chart();c['curve'][-1]['equity']=float('nan')
    assert not project(c,'2026-10-09','2026-10-10')['available']
    assert future_sessions('2026-11-25')[:2]==['2026-11-27','2026-11-30']


def test_forecast_request_does_not_edit_frozen_charts_shadow_or_orders(tmp_path):
    s=Store(tmp_path,initialize(tmp_path));key=s.propose({'policy':{'name':'test'}})
    path=tmp_path/'artifacts'/('chart-'+key+'.json');path.parent.mkdir();path.write_text(json.dumps(chart()))
    raw=path.read_bytes();calendar=calendar_state('2026-10-10T12:00Z')
    f=forecast_data(s,key,calendar);assert f['available']
    f['strategy_curve'][-1]['equity']=0
    assert forecast_data(s,key,calendar)['strategy_curve'][-1]['equity']>0
    assert path.read_bytes()==raw and not s.orders()
    with s.transaction() as db:assert db.execute('SELECT COUNT(*) FROM shadow').fetchone()[0]==0
