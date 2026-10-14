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
