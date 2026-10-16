"""Presentation boundaries: calendar times, actual rules and observed evidence."""
import json

import pytest

from quant_agent.config import initialize
from quant_agent.presentation import session_timeline, strategy_logic
from quant_agent.schedule import calendar_state
from quant_agent.store import Store
from quant_agent.web import public_status


@pytest.mark.parametrize('now,session,opening,closing', [
    ('2026-10-10T11:00Z', '2026-10-12', '2026-10-12T13:30:00+00:00', '2026-10-12T20:00:00+00:00'),
    ('2026-10-12T13:31Z', '2026-10-12', '2026-10-12T13:30:00+00:00', '2026-10-12T20:00:00+00:00'),
    ('2026-10-12T21:00Z', '2026-10-12', '2026-10-12T13:30:00+00:00', '2026-10-12T20:00:00+00:00'),
    ('2026-11-07T10:00Z', '2026-11-09', '2026-11-09T14:30:00+00:00', '2026-11-09T21:00:00+00:00'),
    ('2026-11-27T17:59Z', '2026-11-27', '2026-11-27T14:30:00+00:00', '2026-11-27T18:00:00+00:00'),
    ('2026-11-26T17:00Z', '2026-11-27', '2026-11-27T14:30:00+00:00', '2026-11-27T18:00:00+00:00'),
])
def test_timeline_uses_displayed_session_not_calendar_next_open(now, session, opening, closing):
    t = session_timeline(calendar_state(now))
    assert (t['session'], t['open'], t['close']) == (session, opening, closing)
    assert [n['key'] for n in t['milestones']] == ['preflight', 'open', 'monitor', 'close', 'review']
    if session == '2026-11-27':
        assert t['milestones'][-1]['at'] == '2026-11-27T18:15:00+00:00'


def test_logic_explains_actual_combination_and_risk_template():
    spec = {'policy': {'name': 'custom', 'factor': 'momentum_risk', 'regime': 'gate'},
            'factor_weights': [{'factor': 'near_high', 'weight': .6}, {'factor': 'path_quality', 'weight': .4}]}
    steps = {s['label']: s['text'] for s in strategy_logic(spec)['steps']}
    assert '60%' in steps['排序'] and '252 日' in steps['排序']
    assert '40%' in steps['排序'] and '路径效率' in steps['排序']
    assert '停止加仓' in steps['市场'] and '清仓' not in steps['市场']
    assert '63 日动量 > 0' in steps['入选']
    assert 'T+1' in steps['时点']
    spec['factor_expression'] = {'op': 'lag', 'window': 1, 'x': {'field': 'residual_momentum'}}
    assert 'lag(residual_momentum, 1)' in strategy_logic(spec)['steps'][1]['text']
    assert '60%' not in strategy_logic(spec)['steps'][1]['text']


def test_status_forward_return_is_observed_and_events_have_identity(tmp_path):
    store = Store(tmp_path, initialize(tmp_path, quote_push_enabled=False))
    key = store.propose({'policy': {'name': 'example'}, 'research_data_sha256': 'hash'})
    assert public_status(store)['strategies'][0]['forward_return_pct'] is None
    with store.transaction() as db:
        db.execute('INSERT INTO shadow VALUES (?,?,?,?,?)',
                   (key, '2026-10-12', '2026-10-12T20:01Z', json.dumps({'nav': 10020}), 'hash'))
        db.execute('INSERT INTO shadow VALUES (?,?,?,?,?)',
                   (key, '2026-10-13', '2026-10-13T20:01Z', json.dumps({'nav': 10050}), 'hash'))
    status = public_status(store)
    assert status['strategies'][0]['forward_return_pct'] == pytest.approx(.5)
    assert status['events'][0]['seq'] > 0
    assert len(store.orders()) == 0


def test_full_pool_anomaly_event_is_deduplicated_per_session_direction(tmp_path, monkeypatch):
    from quant_agent import universe
    store = Store(tmp_path, initialize(tmp_path, quote_push_enabled=False))
    store.set_meta('index_universes', {'observed_at': '2026-10-12T13:30Z', 'indexes': {
        'nasdaq100': {'members': ['US.AAA']}, 'sp500': {'members': ['US.AAA']}}})
    store.set_meta('watchlist_symbols', ['US.AAA'])
    monkeypatch.setattr(universe, 'load_panel', lambda _: None)
    monkeypatch.setattr(universe.time, 'sleep', lambda _: None)
    class Broker:
        price = 104
        def snapshots(self, _):
            return [{'code': 'US.AAA', 'last_price': self.price, 'prev_close_price': 100,
                     'turnover': 40e6, 'data_date': '2026-10-12'}]
    broker = Broker()
    monkeypatch.setattr(universe, 'FutuSim', lambda _: broker)
    for price in (104, 105, 96, 95):
        broker.price = price
        universe.scan(store, refresh_members=False)
    with store.transaction() as db:
        events = list(db.execute("SELECT payload FROM events WHERE kind='price_anomaly'"))
    assert [json.loads(r['payload'])['change_pct'] for r in events] == pytest.approx([4, -4])
    assert len(store.orders()) == 0


def test_quote_display_rejects_old_wrong_day_and_untimestamped_push(tmp_path):
    import pandas as pd
    from quant_agent.presentation import focus_quote_view
    store = Store(tmp_path, initialize(tmp_path, quote_push_enabled=False))
    calendar = calendar_state('2026-10-12T13:31Z')
    stamp = pd.Timestamp(calendar['now']).timestamp()*1000
    store.set_meta('last_tick', {'quotes': {
        'US.AAA': {'price': 100, 'update_time': stamp-20000, 'data_date': '2026-10-12'},
        'US.STALE': {'price': 100, 'update_time': stamp-100000, 'data_date': '2026-10-12'}}})
    store.set_meta('push_quotes', {
        'US.AAA': {'last_price': 101, 'update_time': stamp-10000, 'data_date': '2026-10-12'},
        'US.OLD': {'last_price': 101, 'update_time': stamp-10000, 'data_date': '2026-10-09'},
        'US.UNKNOWN': {'last_price': 102, 'received_at': calendar['now'], 'data_date': '2026-10-12'}})
    result = focus_quote_view(store, calendar)
    assert list(result) == ['US.AAA']
    assert result['US.AAA']['price'] == 101 and result['US.AAA']['source'] == 'Futunn WebSocket'
    assert focus_quote_view(store, calendar_state('2026-10-10T11:00Z')) == {}
    # A more recent REST snapshot must outrank a delayed WebSocket message.
    store.set_meta('last_tick', {'quotes': {'US.AAA': {'price': 103, 'update_time': stamp-5000, 'data_date': '2026-10-12'}}})
    assert focus_quote_view(store, calendar)['US.AAA']['price'] == 103


def test_readiness_display_invalidates_old_preflight_immediately_on_pause(tmp_path,monkeypatch):
    from quant_agent import readiness
    s=Store(tmp_path,initialize(tmp_path,quote_push_enabled=False))
    s.set_meta('execution_readiness',{'ready':True,'observed_at':'2026-10-14T13:25Z'})
    calls=[]
    def current(store):
        calls.append(1)
        return {'ready':not store.meta('paused'),'observed_at':'current','blockers':['paused'] if store.meta('paused') else []}
    monkeypatch.setattr(readiness,'execution_readiness',current)
    assert public_status(s)['execution_readiness']['ready']
    assert public_status(s)['execution_readiness']['ready'] and len(calls)==1
    s.pause('history changed')
    view=public_status(s)['execution_readiness']
    assert not view['ready'] and len(calls)==2
    assert view['preflight_observed_at']=='2026-10-14T13:25Z'
    assert s.meta('execution_readiness')['ready']  # Original preflight evidence retained.


def test_strategy_labels_distinguish_slots_and_versions_and_follow_slot_successor(tmp_path):
    from copy import deepcopy
    from tests.test_quant_supervisor_mock import rehearsal_store
    from quant_agent.presentation import strategy_labels
    s=rehearsal_store(tmp_path);records=s.strategies(['paper'])
    for r in records:r['spec']['policy']['name']='residual_momentum20'
    original=deepcopy(records);labels=strategy_labels(records)
    assert len({v['display_name'] for v in labels.values()})==3
    assert {v['identity_label'] for v in labels.values()}=={'模拟 A','模拟 B','模拟 C'}
    assert strategy_labels(list(reversed(records)))==labels
    unused=deepcopy(records[0]);unused['id']='unused-old-authorization';unused['stage']='rejected';unused['created']='2025-01-01T00:00Z'
    assert {k:v for k,v in strategy_labels([unused]+records).items() if k!=unused['id']}==labels
    shadow=deepcopy(records[0]);shadow['id']='new-shadow-identifier';shadow['stage']='shadow';shadow['evidence'].pop('paper_authorization')
    shadow['created']='2026-10-15T02:00:00Z'
    assert '观察 10/15' in strategy_labels(records+[shadow])[shadow['id']]['display_name']
    records[0]['stage']='retired';successor=deepcopy(records[0]);successor['id']='replacement-strategy';successor['stage']='paper'
    successor['evidence']['paper_authorization']['predecessor']=records[0]['id']
    after=strategy_labels(records+[successor])
    assert after[successor['id']]['identity_label']==labels[records[0]['id']]['identity_label']
    assert '归档' in after[records[0]['id']]['display_name']
    assert original[1:]==records[1:]
