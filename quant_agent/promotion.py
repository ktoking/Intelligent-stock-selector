"""Version-bound paper promotion; idle slots only, no orders and no new money."""
import json
import math
from pathlib import Path

import pandas as pd

from .config import canonical,digest
from .store import utcnow
from .pipeline import artifact,engine_hash
from .dataset import frozen_view
from .evolution import compile_strategy
from .engine import simulate
from .exchange_clock import us_calendar,latest_completed_session
from .broker import session_now


def score(stats):
    values=[stats[k] for k in ('validation','retrospective_test')]
    return min(x['return_pct'] for x in values)/max(1.,max(x['max_drawdown_pct'] for x in values))


def rule_signature(record):
    spec=record['spec']
    return digest({'policy':{k:v for k,v in spec['policy'].items() if k!='name'},
        'weights':spec.get('factor_weights',[]),'expression':spec.get('factor_expression')})


def plan_promotions(store,panel):
    policy=store.meta('promotion_policy',{})
    if not policy.get('enabled'):return {'enabled':False,'items':[]}
    if policy.get('mode')=='account_champion':
        from .champion import screen
        return screen(store,panel)
    candidates=[r for r in store.strategies(['shadow']) if r['evidence'].get('passed')
        and r['spec'].get('research_data_sha256')==panel.metadata['input_sha256']]
    owners=store.strategies(['paper']);comparisons=[]
    for candidate in candidates:
        evidence=candidate['evidence'];path=Path(evidence['protocol'])
        windows=json.loads(path.read_text())['windows']
        family=[r for r in owners if rule_signature(r)==rule_signature(candidate)]
        # A universe/data refresh of an existing rule competes for its own
        # slot; do not silently turn another slot into a duplicate strategy.
        for owner in family or owners:
            if engine_hash(owner['spec'])!=owner['spec']['engine_sha256']:continue
            if (candidate['spec']['policy']==owner['spec']['policy']
                and candidate['spec'].get('factor_weights',[])==owner['spec'].get('factor_weights',[])
                and candidate['spec'].get('factor_expression')==owner['spec'].get('factor_expression')
                and evidence['symbols']==owner['evidence']['symbols']):continue
            base,p=compile_strategy(frozen_view(panel,owner),owner['spec'])
            stats={label:simulate(base,p,*windows[label],25)['metrics'] for label in ('validation','retrospective_test')}
            improvement=score(evidence['stats'])-score(stats)
            better=improvement>=policy.get('minimum_score_improvement',.1) and evidence['stats']['retrospective_test']['return_pct']>=stats['retrospective_test']['return_pct']
            comparisons.append({'candidate':candidate['id'],'incumbent':owner['id'],'eligible':better,
                'candidate_score':score(evidence['stats']),'incumbent_score':score(stats),'improvement':improvement,
                'baseline_stats':stats,'candidate_stats':{k:evidence['stats'][k] for k in stats},
                'data_sha256':panel.metadata['input_sha256'],'windows':windows,
                'candidate_spec_sha256':digest(candidate['spec']),'incumbent_spec_sha256':digest(owner['spec']),
                'forward_passed':bool(evidence.get('shadow_gates')) and all(evidence['shadow_gates'].values()),
                'forward_wait_waived':True,'reason':'同一行情、同一区间，历史门槛通过且稳健评分提高' if better else '未优于现有策略，保留研究/Shadow证据'})
    eligible=sorted((r for r in comparisons if r['eligible']),key=lambda r:(-r['improvement'],r['candidate'],r['incumbent']))
    # At most one replacement per completed session; all comparisons stay visible.
    queue=[{**r,'status':'ready','created_at':utcnow()} for r in eligible[:1]]
    result={'created_at':utcnow(),'asof':panel.metadata['end'],'data_sha256':panel.metadata['input_sha256'],
        'policy':policy,'comparisons':comparisons,'items':queue,
        'limitation':'历史择优仍有筛选偏差；用户授权跳过20天等待，仅晋升模拟实验，不代表未来验证通过。'}
    name='promotion-screen-'+digest(result)[:20];artifact(store,name,result)
    store.set_meta('promotion_screen',result);store.set_meta('promotion_queue',queue)
    store.event('promotion_screened',{'asof':result['asof'],'candidates':len(candidates),'comparisons':len(comparisons),'queued':len(queue)})
    return result


def replace_idle_slot(store,item,now=None):
    """Atomic cash attribution transfer; physical account/balances untouched."""
    stamp=pd.Timestamp(now or utcnow())
    if session_now(stamp):return '正常交易时段不更换策略'
    policy=store.meta('promotion_policy',{})
    if not policy.get('enabled'):return '自动晋升未启用'
    if not store.settings.parallel_paper:return '需要独立策略预算'
    if store.meta('paused'):return '执行暂停，候选排队'
    records={r['id']:r for r in store.strategies()}
    candidate=records.get(item['candidate']);old=records.get(item['incumbent'])
    if not candidate or not old or candidate['stage']!='shadow' or old['stage']!='paper':return '候选或原策略阶段已改变'
    gates=candidate['evidence'].get('gates',{})
    required={'test_sample','test_activity','drawdown','validation_and_test_profit','high_cost_profit','delayed_profit','qqq_excess'}
    if not candidate['evidence'].get('passed') or set(gates)!=required or not all(gates.values()):return '候选历史验证未通过'
    if len(candidate['evidence'].get('independent_ledger',{}))!=3:return '缺少独立核账证据'
    if not item.get('eligible') or item.get('improvement',-1)<store.meta('promotion_policy')['minimum_score_improvement']:return '筛选评分未达到晋升门槛'
    if digest(candidate['spec'])!=item['candidate_spec_sha256'] or digest(old['spec'])!=item['incumbent_spec_sha256']:return '比较版本已经改变'
    if engine_hash(candidate['spec'])!=candidate['spec']['engine_sha256']:return '候选引擎已改变'
    from research.daily_factor_lab.core import load_panel
    from research.daily_factor_lab.paper import data_prefix_hash
    data_dir=store.meta('current_data_dir',store.settings.data_dir);panel=load_panel(data_dir)
    if panel.metadata['end']!=latest_completed_session(stamp):return '日线尚未覆盖最近完成交易日'
    if panel.metadata['input_sha256']!=item['data_sha256']:return '行情版本已更新，需要重新比较'
    seed=frozen_view(panel,candidate)
    if data_prefix_hash(seed,candidate['evidence']['seed_asof'])!=candidate['evidence']['seed_hash']:return '候选冻结行情校验失败'
    cal=us_calendar();future=cal.sessions_in_range(stamp.date(),stamp.date()+pd.Timedelta(days=10))
    next_open=next(cal.session_open(d) for d in future if cal.session_open(d)>stamp)
    if stamp>=next_open-pd.Timedelta(minutes=5):return '已接近开盘，保留已预检的策略'
    transfer=digest([item['candidate'],item['incumbent'],item['data_sha256']])
    with store.transaction() as db:
        paused=json.loads(db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0])
        if paused:return '执行暂停，候选排队'
        if db.execute("SELECT 1 FROM orders WHERE status IN ('unknown','submitting') LIMIT 1").fetchone():return '存在未核对回执'
        if db.execute("SELECT 1 FROM orders WHERE strategy=? AND status NOT IN ('filled','cancelled','rejected') LIMIT 1",(old['id'],)).fetchone():return '原策略有未完成订单'
        if db.execute("SELECT 1 FROM strategy_funding WHERE id=?",(transfer+'-in',)).fetchone():return '该次交接已经完成'
        day_key='promotion_applied:'+candidate['evidence']['seed_asof']
        if db.execute('SELECT 1 FROM meta WHERE key=?',(day_key,)).fetchone():return '本交易日已完成一次策略晋升'
        current=dict(db.execute('SELECT id,stage FROM strategies WHERE id IN (?,?)',(old['id'],candidate['id'])).fetchall())
        if current.get(old['id'])!='paper' or current.get(candidate['id'])!='shadow':return '并发阶段变化'
        own=store.strategy_portfolio(old['id'],db=db)
        if own['positions']:return '原策略有持仓，保留原策略并等待空仓'
        unsettled_row=db.execute("SELECT value FROM meta WHERE key='unsettled'").fetchone()
        if unsettled_row and any(r['bucket']=='stock' and r['settles']>stamp.isoformat() for r in json.loads(unsettled_row[0])):return '股票卖出资金尚未交收'
        amount=own['cash']
        if not math.isfinite(amount) or amount<=0:return '没有可交接现金'
        alloc=json.loads(db.execute("SELECT value FROM meta WHERE key='paper_allocations'").fetchone()[0])
        if not math.isclose(sum(alloc['stock'].values()),10000,abs_tol=.001):return '股票总预算不一致'
        caps=alloc['stock'];cap=caps[old['id']]
        if cap<=0:return '原策略无有效额度'
        initial_row=db.execute("SELECT value FROM meta WHERE key='strategy_initial_capital'").fetchone()
        initial=json.loads(initial_row[0]) if initial_row else {}
        if not db.execute("SELECT 1 FROM strategy_funding WHERE bucket='stock' LIMIT 1").fetchone():
            for identity,budget in caps.items():
                db.execute('INSERT INTO strategy_funding VALUES (?,?,?,?,?,?)',('seed-'+identity,identity,'stock',budget,stamp.isoformat(),'initial'))
                initial.setdefault(identity,budget)
        initial[candidate['id']]=amount
        db.execute('INSERT INTO strategy_funding VALUES (?,?,?,?,?,?)',(transfer+'-out',old['id'],'stock',-amount,stamp.isoformat(),'rotation'))
        db.execute('INSERT INTO strategy_funding VALUES (?,?,?,?,?,?)',(transfer+'-in',candidate['id'],'stock',amount,stamp.isoformat(),'rotation'))
        caps[old['id']]=0;caps[candidate['id']]=cap
        old_e={**old['evidence'],'retirement':{'successor':candidate['id'],'at':stamp.isoformat(),'returned_cash':amount,'reason':'用户授权：空仓策略历史择优替换'}}
        new_e={**candidate['evidence'],'paper_authorization':{'source':policy['authorization'],
            'observed_at':stamp.isoformat(),'stock_allocation':cap,'starting_cash':amount,'predecessor':old['id'],
            'forward_wait_waived':True,'forward_validation_passed':item.get('forward_passed',False)}}
        db.execute("UPDATE strategies SET stage='retired',evidence=? WHERE id=?",(canonical(old_e),old['id']))
        db.execute("UPDATE strategies SET stage='paper',evidence=? WHERE id=?",(canonical(new_e),candidate['id']))
        store.set_meta('paper_allocations',alloc,db);store.set_meta('strategy_initial_capital',initial,db)
        store.set_meta(day_key,candidate['id'],db)
        store.set_meta('execution_cache:'+candidate['id'],str(data_dir),db)
        store.set_meta('preflight_session',None,db)
        store.set_meta('execution_readiness',{'ready':False,'observed_at':utcnow(),'data_asof':panel.metadata['end'],
            'blockers':['新策略已交接，等待实际账户与冻结版本重新核对'],'checks':{},'order_endpoint_tested':False,'live_enabled':False},db)
        store.event('paper_promoted',{'candidate':candidate['id'],'predecessor':old['id'],'starting_cash':amount,
            'allocation':cap,'score_improvement':item['improvement'],'stock_budget':10000,'option_budget':1000,
            'forward_validated':item.get('forward_passed',False)},db)
    return None


def apply_promotions(store):
    queue=store.meta('promotion_queue',[]);changed=[]
    for item in queue:
        if item['status']!='ready':continue
        reason=replace_idle_slot(store,item)
        item.update(status='waiting' if reason else 'promoted',reason=reason,attempted_at=utcnow())
        if not reason:changed.append(item['candidate'])
    store.set_meta('promotion_queue',queue)
    return changed
