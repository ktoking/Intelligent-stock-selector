"""Dashboard date and valuation boundaries, including incomplete fills."""
import pytest

from quant_agent.config import initialize
from quant_agent.dashboard import dashboard_summary, age_seconds
from quant_agent.schedule import calendar_state
from quant_agent.store import Store
from tests.test_quant_agent import paper


def summary(store, now='2026-10-12T12:00Z', tasks=()):
    calendar = calendar_state(now)
    store.set_meta('health', {'time': calendar['now'], 'status': 'ok'})
    return dashboard_summary(store, calendar, store.snapshot(), [], store.paper_activity(), tasks)


def test_rest_day_cache_and_initial_cash_are_explicit(tmp_path):
    store = Store(tmp_path, initialize(tmp_path))
    store.set_meta('universe_scan', {'created_at': '2026-10-10T02:00Z', 'hits': [{'data_date': '2026-10-09'}]})
    store.set_meta('daily_brief', {'asof': '2026-10-09', 'generated_at': '2026-10-10T12:00Z'})
    row = summary(store)
    assert row['today_pnl'] == row['today_orders'] == row['today_fills'] == 0
    assert '初始资金' in row['valuation_note']
    freshness = {r['id']: r for r in row['freshness']}
    assert freshness['market']['label'] == '休市缓存' and freshness['market']['tone'] == 'good'
    assert freshness['review']['label'] == '最新收盘复盘'
    assert freshness['news']['label'] == '开盘后更新'
    assert row['attention'] == []
    assert age_seconds('NaT', '2026-10-12T12:00Z') is None


def test_open_session_stale_quote_and_failed_task_need_attention(tmp_path):
    store = Store(tmp_path, initialize(tmp_path))
    store.set_meta('universe_scan', {'created_at': '2026-10-12T12:00Z', 'hits': [{'data_date': '2026-10-09'}]})
    task = {'key': 'review', 'name': '每日复盘', 'state': {'status': 'failed', 'asof': '2026-10-09'}}
    row = summary(store, '2026-10-12T13:31Z', [task])
    assert {r['id'] for r in row['attention']} == {'market', 'task:review'}
    assert next(r for r in row['freshness'] if r['id'] == 'market')['tone'] == 'stale'


def test_today_profit_requires_new_valuation_after_latest_fill(tmp_path, monkeypatch):
    store = Store(tmp_path, initialize(tmp_path))
    key = paper(store)
    monkeypatch.setattr('quant_agent.store.utcnow', lambda: '2026-10-12T13:31:10Z')
    store.reserve('one', key, 'stock', 'US.TEST', 'BUY', 1, 100, '2026-10-12')
    store.claim('one'); store.submitted('one', 'test-only'); store.fill('one', 1, 100, 'filled')
    with store.transaction() as db:
        db.execute('INSERT INTO paper_nav VALUES (?,?,?,?,?,?)', ('2026-10-09T20:01Z', '2026-10-09', 10100, 0, 0, 0))
        db.execute('INSERT INTO paper_nav VALUES (?,?,?,?,?,?)', ('2026-10-12T13:31:00Z', '2026-10-12', 10200, 0, 0, 0))
    row = summary(store, '2026-10-12T13:31:30Z')
    assert row['today_pnl'] is None and row['today_fills'] == 1
    with store.transaction() as db:
        db.execute('UPDATE paper_nav SET observed=? WHERE session=?', ('2026-10-12T13:31:20Z', '2026-10-12'))
    row = summary(store, '2026-10-12T13:31:30Z')
    assert row['today_pnl'] == 100 and row['today_return_pct'] == pytest.approx(100/10100*100)
    assert summary(store, '2026-10-12T13:35Z')['today_pnl'] is None


def test_fill_reconciliation_uses_current_day_even_for_older_order(tmp_path, monkeypatch):
    store = Store(tmp_path, initialize(tmp_path)); key = paper(store)
    monkeypatch.setattr('quant_agent.store.utcnow', lambda: '2026-10-13T00:01Z')
    store.reserve('one', key, 'stock', 'US.TEST', 'BUY', 1, 100, '2026-10-09')
    store.claim('one'); store.submitted('one', 'test-only'); store.fill('one', 1, 100, 'filled')
    row = summary(store, '2026-10-13T00:02Z')
    assert row['session'] == '2026-10-12' and row['today_orders'] == 0 and row['today_fills'] == 1
