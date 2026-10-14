"""Archive before observations; score frozen rules without trading effects."""
from copy import deepcopy
from dataclasses import asdict
import json

import exchange_calendars as xc
import numpy as np
import pandas as pd
import pytest

from quant_agent import engine
from quant_agent.config import initialize, canonical
from quant_agent.forecast_tracking import update_tracking, compare_prefix
from quant_agent.pipeline import engine_hash
from quant_agent.schedule import calendar_state
from quant_agent.store import Store
from research.daily_factor_lab.core import load_panel, catalog, benchmark
from research.daily_factor_lab.paper import data_prefix_hash


@pytest.fixture
def research(tmp_path):
    data = tmp_path/'data'; data.mkdir()
    names = ['US.AAA', 'US.QQQ']
    (data/'universe.json').write_text(canonical({'symbols': names}))
    (data/'security_types.json').write_text(canonical({'US.AAA': 'STOCK', 'US.QQQ': 'ETF'}))
    days = xc.get_calendar('XNYS').sessions_in_range('2025-01-02', '2026-10-20')
    for j, name in enumerate(names):
        close = 100*np.exp(np.cumsum(.001+.005*np.sin(np.arange(len(days))*(1.3+j*.1))))
        pd.DataFrame({'date': days.strftime('%Y%m%d'), 'open': close*.999, 'close': close,
                      'high': close*1.01, 'low': close*.99, 'volume': 1e6, 'turnover': 1e8}).to_pickle(data/(name[3:]+'.pkl'))
    store = Store(tmp_path/'home', initialize(tmp_path/'home', data_dir=str(data)))
    panel = load_panel(data, asof='2026-10-09')
    policy = catalog()['trend_buffer15']
    spec = {'policy': asdict(policy), 'engine_sha256': engine_hash(), 'research_data_sha256': panel.metadata['input_sha256']}
    key = store.propose(spec)
    evidence = {'symbols': names, 'seed_asof': '2026-10-09', 'seed_hash': data_prefix_hash(panel, '2026-10-09')}
    store.transition(key, 'proposed', 'backtested', evidence)
    chart = {'strategy': key, 'data_hash': spec['research_data_sha256'], 'window': ['2026-07-01', '2026-10-09'], 'cost_bps': 25,
             'curve': engine.simulate(panel, policy, '2026-07-01', '2026-10-09')['curve'].to_dict('records'),
             'benchmark_curve': benchmark(panel, '2026-07-01', '2026-10-09')['curve'].to_dict('records')}
    path = store.home/'artifacts'/('chart-'+key+'.json'); path.parent.mkdir(); path.write_text(canonical(chart))
    return store, data, path


def test_preopen_archive_is_immutable_and_get_does_not_write(research, monkeypatch):
    from quant_agent.web import public_status
    store, _, chart = research
    calendar = calendar_state('2026-10-12T12:00Z')
    before = chart.read_bytes()
    first = update_tracking(store, calendar)
    path = next((store.home/'artifacts').glob('forecast-record-*.json')); raw = path.read_bytes()
    second = update_tracking(store, calendar_state('2026-10-12T13:00Z'))
    assert first['archived'] == second['archived'] == 1
    assert first['pending'] == 1 and first['stats']['strategy_mae_pp'] is None
    assert path.read_bytes() == raw and chart.read_bytes() == before
    monkeypatch.setattr('quant_agent.web.calendar_state', lambda: calendar)
    with store.transaction() as db:
        counts = tuple(db.execute('SELECT (SELECT COUNT(*) FROM meta),(SELECT COUNT(*) FROM events)').fetchone())
    assert public_status(store)['forecast_tracking']['archived'] == 1
    with store.transaction() as db:
        assert tuple(db.execute('SELECT (SELECT COUNT(*) FROM meta),(SELECT COUNT(*) FROM events)').fetchone()) == counts
        assert db.execute('SELECT COUNT(*) FROM shadow').fetchone()[0] == 0
    assert store.orders() == []


@pytest.mark.parametrize('now', ['2026-10-12T13:30Z', '2026-10-12T19:00Z'])
def test_archive_refuses_first_future_session_already_started(research, now):
    store, _, _ = research
    assert update_tracking(store, calendar_state(now))['archived'] == 0
    assert not list((store.home/'artifacts').glob('forecast-record-*.json'))


def test_due_forecast_replays_original_rules_and_deduplicates_qqq(research):
    store, _, _ = research
    # Same benchmark input, two distinct strategy versions for scoring.
    record = store.strategies()[0]
    second = store.propose({**record['spec'], 'policy': {**record['spec']['policy'], 'name': 'second-control'}})
    store.transition(second, 'proposed', 'backtested', record['evidence'])
    original = json.loads((store.home/'artifacts'/('chart-'+record['id']+'.json')).read_text())
    (store.home/'artifacts'/('chart-'+second+'.json')).write_text(canonical({**original, 'strategy': second}))
    archived = update_tracking(store, calendar_state('2026-10-12T12:00Z'))
    assert archived['archived'] == 2
    # Both original curves must survive an exact replay through the new end date.
    result = update_tracking(store, calendar_state('2026-10-20T20:16Z'))
    assert result['evaluated'] == 2 and result['pending'] == 0
    assert result['stats']['benchmark_samples'] == 1
    assert result['stats']['strategy_mae_pp'] >= 0
    assert result['rows'][0]['kind'] == 'frozen_rule_retrospective_extension'
    assert not store.orders() and store.portfolio()['cash'] == 10000
    paths = list((store.home/'artifacts').glob('forecast-result-*.json')); raw = [p.read_bytes() for p in paths]
    assert update_tracking(store, calendar_state('2026-10-21T12:00Z'))['evaluated'] == 2
    assert [p.read_bytes() for p in paths] == raw


def test_revision_cannot_be_counted_as_a_prediction_result(research):
    store, data, _ = research
    update_tracking(store, calendar_state('2026-10-12T12:00Z'))
    path = data/'AAA.pkl'; frame = pd.read_pickle(path); frame.loc[1, 'volume'] += 1; frame.to_pickle(path)
    result = update_tracking(store, calendar_state('2026-10-20T20:16Z'))
    assert result['evaluated'] == 0 and result['unavailable'] == 1
    assert '行情发生修订' in result['rows'][0]['reason']
    assert result['stats']['benchmark_coverage_pct'] is None
    original = [{'date': '2026-10-09', 'equity': 10000, 'cash': 9000}]
    changed = deepcopy(original); changed[0]['cash'] += 1
    with pytest.raises(ValueError, match='发生修订'):
        compare_prefix(original, changed, '2026-10-09')
