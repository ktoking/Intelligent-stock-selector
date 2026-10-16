"""Auditable strategy activity; keep historical, shadow and broker evidence apart."""
import json
import math
from pathlib import Path

from .config import canonical


def ledger_activity(curve, orders, kind='retrospective_test'):
    dates = {r['date'] for r in curve}
    positions, trades, daily, by_day = {}, [], [], {}
    for order in orders:
        if order['date'] not in dates:
            raise ValueError('Trade outside equity evidence')
        by_day.setdefault(order['date'], []).append(order)
    cash, prior_nav, realized = 10000., 10000., 0.
    for point in curve:
        day_trades = []
        for order in by_day.get(point['date'], []):
            s, qty = order['symbol'], float(order['qty'])
            price, fee = float(order['raw_price']), float(order['cost'])
            amount = qty*price
            held = positions.setdefault(s, {'qty': 0., 'basis': 0.})
            pnl = 0.
            if order['side'] == 'BUY':
                cash -= amount+fee
                held['qty'] += qty
                held['basis'] += amount+fee
            else:
                if qty > held['qty']+1e-9:
                    raise ValueError('Historical sale exceeds holdings')
                basis = held['basis']*qty/held['qty']
                pnl = amount-fee-basis
                held['basis'] -= basis
                held['qty'] -= qty
                cash += amount-fee
                realized += pnl
            trade = {'date': point['date'], 'symbol': s, 'side': order['side'], 'qty': qty,
                     'price': price, 'amount': amount, 'fee': fee, 'realized_pnl': pnl,
                     'cash_flow': -(amount+fee) if order['side']=='BUY' else amount-fee,
                     'reason': order.get('reason'), 'evidence_kind': kind}
            trades.append(trade); day_trades.append(trade)
        nav = float(point['equity'])
        if not math.isclose(cash, float(point['cash']), abs_tol=1e-6):
            raise ValueError('Trading cash differs from frozen curve')
        held_value = nav-cash
        unrealized = held_value-sum(p['basis'] for p in positions.values())
        if not math.isclose(realized+unrealized, nav-10000, abs_tol=1e-6):
            raise ValueError('Realized/unrealized accounting differs')
        daily.append({'date': point['date'], 'nav': nav, 'cash': cash, 'held_value': held_value,
                      'pnl': nav-prior_nav, 'return_pct': (nav/prior_nav-1)*100,
                      'cumulative_return_pct': (nav/10000-1)*100,
                      'buy_amount': sum(t['amount'] for t in day_trades if t['side']=='BUY'),
                      'sell_amount': sum(t['amount'] for t in day_trades if t['side']=='SELL'),
                      'fees': sum(t['fee'] for t in day_trades), 'order_count': len(day_trades),
                      'realized_pnl': sum(t['realized_pnl'] for t in day_trades),
                      'unrealized_pnl': unrealized, 'holdings': sum(p['qty']>1e-9 for p in positions.values())})
        prior_nav = nav
    return {'trades': trades, 'daily': daily, 'summary': {
        'order_count': len(trades), 'buy_count': sum(t['side']=='BUY' for t in trades),
        'sell_count': sum(t['side']=='SELL' for t in trades),
        'buy_amount': sum(t['amount'] for t in trades if t['side']=='BUY'),
        'sell_amount': sum(t['amount'] for t in trades if t['side']=='SELL'),
        'fees': sum(t['fee'] for t in trades), 'realized_pnl': realized,
        'unrealized_pnl': daily[-1]['unrealized_pnl'] if daily else 0,
        'nav': daily[-1]['nav'] if daily else None}}


def save_activity(store, record, run, data_hash):
    from .pipeline import artifact
    name = 'activity-'+record['id']
    path = store.home/'artifacts'/(name+'.json')
    if path.exists():
        return str(path)
    curve = run['curve'].to_dict('records')
    payload = ledger_activity(curve, run['orders']) | {'strategy': record['id'], 'data_hash': data_hash,
        'kind': 'retrospective_test', 'source': 'Futunn 复权日线；历史假设交易，非券商成交',
        'curve': curve, 'cost_bps': 25, 'limitation': '成交价为历史开盘价，费用为单边25bps模型；股数属于复权口径，不是当时真实委托股数。'}
    return artifact(store, name, payload)


def backfill_activity(store):
    """Rebuild only exact engine/data versions and require frozen metric equality."""
    from research.daily_factor_lab.core import load_panel
    from .pipeline import engine_hash
    from .evolution import compile_strategy
    from .engine import simulate
    candidates = [Path(store.settings.data_dir)] + sorted((store.home/'market-data').glob('*'))
    panels = {}
    for directory in dict.fromkeys(candidates):
        if not directory.is_dir() or not (directory/'universe.json').exists():
            continue
        panel = load_panel(directory)
        panels[panel.metadata['input_sha256']] = panel
    outputs, skipped = [], []
    for r in store.strategies():
        if (store.home/'artifacts'/('activity-'+r['id']+'.json')).exists():
            continue
        expected = r['evidence'].get('stats', {}).get('retrospective_test')
        panel = panels.get(r['spec'].get('research_data_sha256'))
        if not expected:
            continue
        if panel is None or r['spec'].get('engine_sha256') != engine_hash(r['spec']):
            skipped.append({'id': r['id'], 'reason': 'Exact frozen dataset or engine unavailable'}); continue
        protocol = json.loads(Path(r['evidence']['protocol']).read_text())
        start, end = protocol['windows']['retrospective_test']
        compiled, policy = compile_strategy(panel, r['spec'])
        run = simulate(compiled, policy, start, end, 25)
        for metric in ('return_pct','max_drawdown_pct','cost_dollars','orders','trading_days','avg_exposure_pct'):
            if not math.isclose(run['metrics'][metric], expected[metric], rel_tol=1e-10, abs_tol=1e-8):
                raise ValueError('Reconstructed transactions differ from frozen '+metric)
        outputs.append(save_activity(store, r, run, panel.metadata['input_sha256']))
    return {'created': len(outputs), 'skipped': skipped}


def strategy_activity(store, strategy, mode='history'):
    record = next((r for r in store.strategies() if r['id']==strategy), None)
    if record is None:
        return None
    if mode == 'history':
        path = store.home/'artifacts'/('activity-'+strategy+'.json')
        return json.loads(path.read_text()) if path.exists() else {'kind': 'retrospective_test', 'trades': [], 'daily': [],
            'summary': {}, 'unavailable': '尚无可核账交易材料；冻结数据或引擎缺失时不会用新版本替代。'}
    if mode == 'forward':
        with store.transaction() as db:
            sealed = [dict(r) for r in db.execute('SELECT session,state FROM shadow WHERE strategy=? ORDER BY session', (strategy,))]
            events = [dict(r) for r in db.execute("SELECT created,payload FROM events WHERE kind='shadow_observed_plan' AND json_extract(payload,'$.strategy')=? ORDER BY seq", (strategy,))]
            pending = db.execute('SELECT value FROM meta WHERE key=?', ('shadow_pending:'+strategy,)).fetchone()
        plans = {json.loads(e['payload'])['session']: json.loads(e['payload'])['orders'] for e in events}
        curve = [{'date':r['session'], 'equity':json.loads(r['state'])['nav'], 'cash':json.loads(r['state'])['cash']} for r in sealed]
        # Orders must remain present to reconstruct costs/basis. Legacy seals
        # lacking their plan keep their curve but cannot imply full accounting.
        if any(json.loads(r['state']).get('order_count',0) and r['session'] not in plans for r in sealed):
            return {'kind':'forward_shadow','trades':[],'daily':[],'summary':{},'unavailable':'旧观察日缺少计划材料，无法完整核账。'}
        orders = [o for r in sealed for o in plans.get(r['session'], [])]
        return ledger_activity(curve, orders, 'forward_shadow') | {'kind':'forward_shadow', 'curve':curve,
            'source':'未来实际观察报价；Shadow 假设成交，非券商成交',
            'pending_plan':json.loads(pending['value']) if pending else None}
    with store.transaction() as db:
        orders = [{k:v for k,v in dict(r).items() if k!='broker_id'} for r in db.execute("SELECT * FROM orders WHERE strategy=? AND bucket='stock' ORDER BY created", (strategy,))]
        fills = [dict(r) for r in db.execute("SELECT f.*,o.symbol,o.side,o.session,o.multiplier FROM fills f JOIN orders o ON o.id=f.order_id WHERE o.strategy=? AND o.bucket='stock' ORDER BY f.seq", (strategy,))]
        owners = [r[0] for r in db.execute('SELECT DISTINCT strategy FROM orders')]
        closing_nav = [dict(r) for r in db.execute('SELECT * FROM paper_nav p WHERE observed=(SELECT MAX(observed) FROM paper_nav WHERE session=p.session) ORDER BY session')]
    allocated=store.settings.parallel_paper and strategy in store.meta('paper_allocations',{}).get('stock',{})
    own=None
    if allocated:
        own=store.strategy_portfolio(strategy)
        with store.transaction() as db:
            owner_curve=[dict(r) for r in db.execute('SELECT * FROM strategy_paper_nav WHERE strategy=? ORDER BY observed',(strategy,))]
            closing_nav=[dict(r) for r in db.execute('SELECT * FROM strategy_paper_nav p WHERE strategy=? AND observed=(SELECT MAX(observed) FROM strategy_paper_nav WHERE strategy=p.strategy AND session=p.session) ORDER BY session',(strategy,))]
        basis={}
        for f in fills:
            position=basis.setdefault(f['symbol'],{'qty':0,'basis':0.})
            amount=f['qty']*f['price']*f['multiplier']
            if f['side']=='BUY':
                position['qty']+=f['qty'];position['basis']+=amount+f['fee'];f['realized_pnl']=0.
            else:
                if f['qty']>position['qty']:raise ValueError('Owner fill ledger inconsistent')
                removed=position['basis']*f['qty']/position['qty'];position['qty']-=f['qty'];position['basis']-=removed
                f['realized_pnl']=amount-f['fee']-removed
    # The original paper NAV is account-level. Only attribute it to a strategy
    # when no other strategy ever submitted an intent in that ledger.
    attributable = allocated or not owners or owners == [strategy]
    activity = (own | {'curve':owner_curve}) if allocated else store.paper_activity()
    if allocated and record['stage']=='retired':activity={**activity,'nav':own['performance_nav']}
    curve = [{'date':r['observed'], 'session':r['session'], 'equity':r['nav'], 'cash':r['cash']} for r in activity['curve']] if attributable and fills else []
    performance_curve=None
    if allocated and store.settings.capital_mode=='broker_equity':
        from .capital import strategy_performance
        performance_return,performance_curve=strategy_performance(store,strategy,own)
        curve=[{'date':r['observed'],'session':r['session'],'equity':r['performance_equity'],'cash':r['cash']} for r in performance_curve]
        activity={**activity,'nav':own['initial_capital']*(1+performance_return/100)}
    daily = []
    if attributable and fills:
        prior = activity.get('initial_capital',activity.get('budget',10000.))
        for point in closing_nav:
            day = point['session']
            rows = [f for f in fills if f['session']==day]
            neutral=next((r for r in performance_curve or [] if r['observed']==point['observed']),None)
            performance_nav=neutral['performance_equity'] if neutral else point['nav']
            daily.append({'date':day,'nav':point['nav'],'cash':point['cash'],'held_value':point['nav']-point['cash'],
                          'pnl':sum(f['realized_pnl'] for f in rows)+point['unrealized']-(daily[-1].get('unrealized',0) if daily else 0) if neutral else point['nav']-prior,
                          'unrealized':point.get('unrealized',0),'return_pct':(performance_nav/prior-1)*100,'fees':sum(f['fee'] for f in rows),
                          'order_count':sum(o['session']==day for o in orders),
                          'buy_amount':sum(f['qty']*f['price']*f['multiplier'] for f in rows if f['side']=='BUY'),
                          'sell_amount':sum(f['qty']*f['price']*f['multiplier'] for f in rows if f['side']=='SELL')})
            prior = performance_nav
    trades = [{'date':f['observed'],'session':f['session'],'symbol':f['symbol'],'side':f['side'],'qty':f['qty'],'price':f['price'],
               'amount':f['qty']*f['price']*f['multiplier'],'fee':f['fee'],'realized_pnl':f['realized_pnl'],'evidence_kind':'paper'} for f in fills]
    return {'kind':'paper','trades':trades,'daily':daily,'orders':orders,'curve':curve,
            'summary':{'order_count':len(orders),'fill_count':len(fills),'fees':sum(f['fee'] for f in fills),
                       'initial_capital':activity.get('initial_capital',activity.get('budget',10000.)),
                       'buy_amount':sum(t['amount'] for t in trades if t['side']=='BUY'),'sell_amount':sum(t['amount'] for t in trades if t['side']=='SELL'),
                       'realized_pnl':sum(f['realized_pnl'] for f in fills),
                       'unrealized_pnl':activity['unrealized_pnl'] if attributable and (fills or allocated) else None,
                       'nav':activity['nav'] if attributable and (fills or allocated) else None},
            'source':('券商模拟成交；按策略资金分配与成交归属独立核账。' if allocated else '券商模拟成交；')+'时间为程序核对时间，费用为估算',
            'limitation':'账本曾有多个策略，收益不可归属到单个策略。' if not attributable else '盘中当日收益仍为账本估值，尚未封存。'}
