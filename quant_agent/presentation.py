"""Read-only descriptions for the dashboard; these do not change trading rules."""
import math

import exchange_calendars as xc
import pandas as pd

from .exchange_clock import us_calendar


def strategy_labels(records):
    """Readable identities only; frozen strategy names and IDs stay intact."""
    names={'trend_buffer15':'趋势缓冲','momentum_blend20':'多周期动量',
        'residual_momentum20':'残差动量','trend_reduce20':'趋势减仓',
        'residual_close_pressure20':'残差收盘压力','residual_impact_discount':'残差冲击折价',
        'residual_gap_stability':'残差缺口稳定','residual_pressure_discount':'残差压力折价'}
    by_id={r['id']:r for r in records}
    roots=[r for r in records if r['stage'] in ('paper','live_review','retired') and r['evidence'].get('paper_authorization') and
        not r['evidence']['paper_authorization'].get('predecessor')]
    roots.sort(key=lambda r:(r['evidence']['paper_authorization'].get('observed_at',r['created']),r['created'],r['id']))
    slots={r['id']:chr(65+i) if i<26 else str(i+1) for i,r in enumerate(roots)}
    result={}
    for record in records:
        spec,e=record['spec'],record['evidence'];name=spec.get('factor_name',spec['policy']['name'])
        root=record;seen=set()
        while root['evidence'].get('paper_authorization',{}).get('predecessor') in by_id and root['id'] not in seen:
            seen.add(root['id']);root=by_id[root['evidence']['paper_authorization']['predecessor']]
        stamp=pd.Timestamp(record['created']);stamp=stamp.tz_localize('UTC') if stamp.tzinfo is None else stamp
        version=stamp.tz_convert('Asia/Shanghai').strftime('%m/%d')+' · '+record['id'][:4]
        role=('模拟 '+slots[root['id']]+(' · 归档' if record['stage']=='retired' else '') if root['id'] in slots else
            ('观察 ' if record['stage']=='shadow' else '研究 ')+version)
        if e.get('paper_authorization',{}).get('account_champion'):
            role='账户优胜策略'+(' · 归档' if record['stage']=='retired' else '')
        universe=len(e.get('symbols',spec.get('research_symbols',[])))
        vol=spec['policy'].get('annual_vol_target')
        note=('冻结 '+e['seed_asof']+' · ' if e.get('seed_asof') else '')+(str(universe)+' 只标的池' if universe else '研究池待核验')
        if isinstance(vol,(int,float)):note+=' · 年化波动目标 '+format(vol*100,'.0f')+'%'
        result[record['id']]={'display_name':names.get(name,name.replace('_',' '))+' · '+role,
            'identity_label':role,'identity_note':note,'frozen_universe_count':universe}
    return result


def focus_quote_view(store, calendar):
    """Expose fresh stored prices for display, never as execution inputs."""
    if not calendar['session']:
        return {}
    now = pd.Timestamp(calendar['now'])
    candidates = [((store.meta('last_tick') or {}).get('quotes', {}), 'Futunn REST'),
                  (store.meta('push_quotes', {}), 'Futunn WebSocket')]
    result = {}
    for quotes, source in candidates:
        for symbol, q in quotes.items():
            try:
                price = float(q.get('last_price', q.get('price', 0)))
                stamp = float(q['update_time'])
                at = pd.Timestamp(stamp, unit='ms' if stamp > 1e12 else 's', tz='UTC')
                if (not math.isfinite(price) or price <= 0 or q.get('data_date') != calendar['session']
                        or not -10 <= (now-at).total_seconds() <= store.settings.max_quote_age_seconds):
                    continue
                if symbol in result and at <= pd.Timestamp(result[symbol]['updated_at']):
                    continue
                result[symbol] = {'price': price, 'data_date': q['data_date'],
                                  'updated_at': at.isoformat(), 'source': source}
            except (ValueError, TypeError, KeyError, OverflowError):
                continue
    return result


def session_timeline(calendar):
    now = pd.Timestamp(calendar['now'])
    cal = us_calendar()
    day = now.tz_convert('America/New_York').date().isoformat()
    # Keep today's completed session visible until New York midnight. On a
    # weekend/holiday show the next session, not the last completed Friday.
    session = pd.Timestamp(day if cal.is_session(day) else calendar['next_session'])
    opening, closing = cal.session_open(session), cal.session_close(session)
    milestones = [('preflight', '开盘预检', opening-pd.Timedelta(minutes=5)),
                  ('open', '开盘 · 生成计划', opening),
                  ('monitor', '持续监控', opening+pd.Timedelta(minutes=5)),
                  ('close', '收盘封存', closing),
                  ('review', '研究与复盘', closing+pd.Timedelta(minutes=15))]
    return {'session': str(session.date()), 'open': opening.isoformat(), 'close': closing.isoformat(),
            'milestones': [{'key': k, 'label': label, 'at': at.isoformat()} for k, label, at in milestones],
            'timezone': 'Asia/Shanghai', 'note': '节点等距展示；游标按各阶段内的实际时间推进。时间到达不代表任务已完成。'}


FACTOR_NAMES = {'momentum_risk': '63 日动量 / 20 日波动',
                'momentum_blend': '63 / 126 日平均动量 / 20 日波动',
                'residual_momentum': '相对 QQQ 的 63 日残差动量 / 20 日波动',
                'path_quality': '风险调整动量 × 63 日路径效率',
                'near_high': '收盘价 / 252 日滚动最高价'}


def expression_formula(expression):
    """Format stored arithmetic as text only, without evaluating model output."""
    operators = {'add': '+', 'sub': '−', 'mul': '×', 'div': '/'}
    def describe(node, depth=0):
        if depth > 15:
            raise ValueError('Expression too deep')
        if node.get('field'):
            return str(node['field'])
        op = node['op']
        if op in operators:
            return '('+describe(node['a'], depth+1)+' '+operators[op]+' '+describe(node['b'], depth+1)+')'
        x = describe(node['x'], depth+1)
        return op+'('+x+(', '+str(node['window']) if op in ('mean', 'std', 'lag') else '')+')'
    try:
        if 'nodes' in expression:
            trees = []
            for i, node in enumerate(expression['nodes'][:15]):
                tree = dict(node)
                for key in ('a', 'b', 'x'):
                    ref = tree.get(key)
                    if ref is not None:
                        if type(ref) is not int or not 0 <= ref < i:
                            raise ValueError('Invalid reference')
                        tree[key] = trees[ref]
                trees.append(tree)
            root = expression['root']
            if type(root) is not int or not 0 <= root < len(trees):
                raise ValueError('Invalid root')
            return describe(trees[root])
        return describe(expression)
    except (KeyError, TypeError, ValueError):
        return '表达式说明不可用；请查看保存的因子代码'


def strategy_logic(spec):
    defaults = {'factor': 'momentum_risk', 'trend': 'slow', 'schedule': 'twice', 'positions': 5,
                'weight_cap': .3, 'annual_vol_target': .2, 'rank_buffer': 10, 'regime': 'gate',
                'trail': .15, 'turnover_band': .025, 'cooldown': 2}
    p = defaults | spec['policy']
    expression = spec.get('factor_expression')
    weights = spec.get('factor_weights')
    factor = (expression_formula(expression) if expression else
              ' + '.join(f"{w['weight']:.0%} × 普通股截面排名({FACTOR_NAMES.get(w['factor'], w['factor'])})" for w in weights)
              if weights else FACTOR_NAMES.get(p['factor'], p['factor']))
    factor_label = '自定义因子' if expression else '因子组合' if weights else FACTOR_NAMES.get(p['factor'], p['factor'])
    trend = '收盘价 > EMA20 > EMA60' if p['trend'] == 'fast' else '收盘价 > EMA50 > EMA150'
    if p['trend'] == 'breakout':
        trend += '，且近 10 日出现 55 日高点突破'
    cadence = {'daily': '每个交易日', 'weekly': '每周首个交易日',
               'twice': '每周首个交易日与周四（遇休市顺延）'}[p['schedule']]
    regime = ('QQQ 高于 EMA100 允许新买入；低于或等于时停止加仓，已有持仓仍按排名与退出规则处理。'
              if p['regime'] == 'gate' else
              'QQQ 高于 EMA100 正常分配；低于或等于 EMA100 但高于 EMA200 时目标总仓上限 50%；其余目标为 0。')
    return {'summary': f"{factor_label}降序选股；{trend}；{cadence}调仓，目标 {p['positions']} 股。",
            'steps': [
                {'label': '入选', 'text': f'普通股、20 日平均成交额 ≥ 500 万美元、63 日动量 > 0、20 日波动有效且 > 0；{trend}。'},
                {'label': '排序', 'text': factor+'；分数越高越优先，排除无效分数。'},
                {'label': '调仓', 'text': f"{cadence}调仓；首次计划也会评估。已有持仓仍在前 {p['rank_buffer']} 名则优先保留，再补前 {p['positions']} 名，最多 {p['positions']} 股。"},
                {'label': '仓位', 'text': f"按 20 日波动倒数分配，以近 60 日协方差控制年化波动目标 {p['annual_vol_target']:.0%}、单股上限 {p['weight_cap']:.0%}；偏离目标小于 {p['turnover_band']:.1%} 可跳过调仓，退出 / 新建仓 / 超上限除外。"},
                {'label': '市场', 'text': regime},
                {'label': '退出', 'text': f"前一日收盘跌破 {'EMA20' if p['trend']=='fast' else 'EMA50'}，或较跟踪高点回撤超过 {p['trail']:.0%}，发出退出计划；冷却 {p['cooldown']} 个交易日。"},
                {'label': '时点', 'text': '使用已完成日线，开盘后的前 5 分钟评估计划；卖出资金 T+1，买入只用已有现金。行情异动先入研究池。'}]}
