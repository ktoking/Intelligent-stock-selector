"""Local forecast archives and later checks, isolated from execution and Shadow.

Only predictions made before the first future session opens are archived.
Strategy outcomes are retrospective extensions of frozen rules, not fills.
"""
from contextlib import contextmanager
import fcntl
import json
import math

import exchange_calendars as xc
import pandas as pd

from .exchange_clock import us_calendar

from research.daily_factor_lab.core import benchmark, load_panel
from research.daily_factor_lab.paper import data_prefix_hash
from . import engine
from .config import digest
from .dataset import frozen_view
from .evolution import compile_strategy
from .forecast import forecast_data, MODEL
from .pipeline import artifact, engine_hash
from .schedule import calendar_state


@contextmanager
def tracking_lock(store):
    with (store.home/'forecast-tracking.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def compare_prefix(original, extended, origin):
    """Reject revised history before scoring a forecast."""
    prefix = [r for r in extended if r['date'] <= origin]
    if [r['date'] for r in prefix] != [r['date'] for r in original]:
        raise ValueError('历史日期与封存曲线不一致，不能统计兑现。')
    for before, after in zip(original, prefix):
        for key in ('equity', 'cash', 'holdings'):
            if key in before and not math.isclose(before[key], after[key], rel_tol=1e-10, abs_tol=1e-7):
                raise ValueError('历史净值或现金发生修订，不能统计兑现。')


def replay(store, archive):
    """Recompute with the original members, rules, start and costs."""
    record = archive['record']
    if record['spec']['engine_sha256'] != engine_hash(record['spec']):
        raise ValueError('冻结引擎版本已变化，等待原版本复算。')
    if not archive['symbols']:
        raise ValueError('未取得原版本成员名单，无法核验冻结规则。')
    panel = load_panel(store.meta('current_data_dir', store.settings.data_dir), asof=archive['end_date'])
    record = {**record, 'evidence': {**record['evidence'], 'symbols': archive['symbols']}}
    panel = frozen_view(panel, record)
    dates = {str(d.date()) for d in panel.dates}
    if any(d not in dates for d in archive['forecast']['future_dates']):
        raise ValueError('未来已完成日线尚不完整，等待数据更新。')
    if archive.get('prefix_hash') and data_prefix_hash(panel, archive['origin_date']) != archive['prefix_hash']:
        raise ValueError('历史行情发生修订，不能统计兑现。')
    compiled, policy = compile_strategy(panel, record['spec'])
    start = archive['chart']['window'][0]
    bps = archive['chart']['cost_bps']
    run = engine.simulate(compiled, policy, start, archive['end_date'], bps)
    base = benchmark(panel, start, archive['end_date'], bps)
    curves = {'curve': run['curve'].to_dict('records'), 'benchmark_curve': base['curve'].to_dict('records')}
    for key in curves:
        compare_prefix(archive['chart'][key], curves[key], archive['origin_date'])
        if curves[key][-1]['date'] != archive['end_date']:
            raise ValueError('终点日线尚不可用。')
    return curves


def score(archive, curves, observed_at):
    result = {'archive': archive['id'], 'observed_at': observed_at,
              'kind': 'frozen_rule_retrospective_extension', 'source': 'Futunn 本地已完成日线',
              'limitation': '冻结规则的事后复算；不代表 Shadow 观察或券商模拟成交。'}
    for name, key in (('strategy', 'curve'), ('benchmark', 'benchmark_curve')):
        predicted = archive['forecast'][name+'_curve'][-1]
        actual = (curves[key][-1]['equity']/archive['chart'][key][-1]['equity']-1)*100
        result[name+'_return_pct'] = actual
        result[name+'_error_pp'] = actual-predicted['return_pct']
        result[name+'_covered'] = predicted['low_return_pct'] <= actual <= predicted['high_return_pct']
    return result


def original_inputs(store, data_hash, origin, cache):
    """Find the exact archived dataset; never infer missing members."""
    candidates = [store.meta('current_data_dir', store.settings.data_dir), store.settings.data_dir]
    candidates += [str(p.parent) for p in (store.home/'market-data').glob('*/universe.json')]
    for directory in dict.fromkeys(candidates):
        key = (directory, origin)
        if key not in cache:
            try:
                cache[key] = load_panel(directory, asof=origin)
            except (FileNotFoundError, ValueError):
                cache[key] = None
        panel = cache[key]
        if panel is not None and panel.metadata['input_sha256'] == data_hash:
            return {'symbols': panel.symbols, 'prefix_hash': data_prefix_hash(panel, origin)}
    return {'symbols': [], 'prefix_hash': None}


def update_tracking(store, calendar=None):
    """Run explicitly or from a background job; GET endpoints never write."""
    calendar = calendar or calendar_state()
    with tracking_lock(store):
        return _update_tracking(store, calendar)


def _update_tracking(store, calendar):
    now = pd.Timestamp(calendar['now'])
    cal = us_calendar()
    input_cache = {}
    for record in store.strategies():
        ident = digest({'strategy': record['id'], 'origin': calendar['completed_session'], 'model': MODEL})
        path = store.home/'artifacts'/('forecast-record-'+ident+'.json')
        if path.exists():
            continue
        forecast = forecast_data(store, record['id'], calendar)
        if not forecast or not forecast.get('available') or now >= cal.session_open(forecast['future_dates'][0]):
            continue
        chart = json.loads((store.home/'artifacts'/('chart-'+record['id']+'.json')).read_text())
        if record['spec'].get('engine_sha256') != engine_hash(record['spec']):
            continue
        symbols = record['evidence'].get('symbols', [])
        prefix = record['evidence'].get('seed_hash') if record['evidence'].get('seed_asof') == forecast['origin_date'] else None
        if not symbols:
            inputs = original_inputs(store, chart['data_hash'], forecast['origin_date'], input_cache)
            symbols, prefix = inputs['symbols'], inputs['prefix_hash']
        archive = {'id': ident, 'strategy': record['id'], 'name': record['spec'].get('factor_name', record['spec']['policy']['name']),
                   'created_at': now.isoformat(), 'origin_date': forecast['origin_date'], 'end_date': forecast['future_dates'][-1],
                   'forecast': forecast, 'forecast_sha256': digest(forecast), 'chart': chart, 'chart_sha256': digest(chart),
                   'record': record, 'symbols': symbols, 'prefix_hash': prefix}
        artifact(store, 'forecast-record-'+ident, archive)
        store.event('forecast_archived', {'strategy': record['id'], 'origin': forecast['origin_date'], 'end': archive['end_date']})
    rows = []
    for path in sorted((store.home/'artifacts').glob('forecast-record-*.json')):
        archive = json.loads(path.read_text())
        if not archive['symbols']:
            input_path = store.home/'artifacts'/('forecast-input-'+archive['id']+'.json')
            if not input_path.exists() and now < cal.session_open(archive['forecast']['future_dates'][0]):
                inputs = original_inputs(store, archive['chart']['data_hash'], archive['origin_date'], input_cache)
                if inputs['symbols']:
                    artifact(store, 'forecast-input-'+archive['id'], {**inputs, 'created_at': now.isoformat(),
                             'data_hash': archive['chart']['data_hash'], 'archive_sha256': digest(archive)})
            if input_path.exists():
                inputs = json.loads(input_path.read_text())
                if (inputs['archive_sha256'] == digest(archive) and inputs['data_hash'] == archive['chart']['data_hash']
                        and pd.Timestamp(inputs['created_at']) < cal.session_open(archive['forecast']['future_dates'][0])):
                    archive = {**archive, 'symbols': inputs['symbols'], 'prefix_hash': inputs['prefix_hash']}
        row = {key: archive[key] for key in ('strategy', 'name', 'origin_date', 'end_date', 'created_at')}
        result_path = store.home/'artifacts'/('forecast-result-'+archive['id']+'.json')
        if result_path.exists():
            row.update(json.loads(result_path.read_text()), state='evaluated')
        elif not archive['symbols']:
            row.update(state='unavailable', reason='未在首个未来交易日前取得原版本成员名单，不能核验冻结规则。')
        elif archive['end_date'] > calendar['completed_session']:
            row.update(state='pending', reason='首个未来交易日前封存；等待 '+archive['end_date']+' 收盘后的完整日线。')
        else:
            try:
                if digest(archive['forecast']) != archive['forecast_sha256'] or digest(archive['chart']) != archive['chart_sha256']:
                    raise ValueError('封存材料校验不一致。')
                result = score(archive, replay(store, archive), now.isoformat())
                artifact(store, 'forecast-result-'+archive['id'], result)
                row.update(result, state='evaluated')
                store.event('forecast_evaluated', {'strategy': archive['strategy'], 'origin': archive['origin_date']})
            except (ValueError, FileNotFoundError, KeyError, IndexError) as exc:
                row.update(state='unavailable', reason=str(exc) if isinstance(exc, ValueError) else '复算材料尚不完整，等待数据更新。')
        row['benchmark_key'] = digest({'model': archive['forecast']['model'], 'origin': archive['origin_date'],
                                       'curve': archive['forecast']['benchmark_curve']})
        rows.append(row)
    checked = [r for r in rows if r['state'] == 'evaluated']
    qqq = {r['benchmark_key']: r for r in checked}
    summary = {'archived': len(rows), 'evaluated': len(checked), 'pending': sum(r['state'] == 'pending' for r in rows),
               'unavailable': sum(r['state'] == 'unavailable' for r in rows), 'updated_at': now.isoformat(),
               'stats': {'strategy_mae_pp': sum(abs(r['strategy_error_pp']) for r in checked)/len(checked) if checked else None,
                         'benchmark_samples': len(qqq),
                         'benchmark_coverage_pct': sum(r['benchmark_covered'] for r in qqq.values())/len(qqq)*100 if qqq else None},
               'rows': sorted(rows, key=lambda r: (r['origin_date'], r['created_at']), reverse=True)}
    store.set_meta('forecast_tracking', summary)
    return summary
