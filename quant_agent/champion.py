"""Validated research competition and a persistent broker-paper rotation workflow."""
import json
import math
from pathlib import Path

import pandas as pd

from research.daily_factor_lab.core import benchmark
from scripts.verify_daily_factor_run import audit_ledger
from .broker import executable_quote, session_now, QuoteUnavailable
from .capital import TREASURY
from .config import canonical,digest
from .dataset import frozen_view
from .engine import simulate
from .evolution import compile_strategy
from .pipeline import artifact,engine_hash
from .promotion import score
from .store import utcnow

REQUIRED={'test_sample','test_activity','drawdown','validation_and_test_profit','high_cost_profit','delayed_profit','qqq_excess'}
TERMINAL={'completed','superseded'}
CASH='__cash__'


def enabled(store):
    return store.meta('promotion_policy',{}).get('mode')=='account_champion'


def pending(store):
    rotation=store.meta('champion_rotation')
    return rotation if rotation and rotation['status'] not in TERMINAL else None


def valid_record(record):
    e=record['evidence']
    if e.get('paper_experiment'):
        return (record['spec'].get('execution_timeframe')=='5m' and e.get('experiment_authorization')=='2026-10-20'
            and len(e.get('independent_ledger',{}))==3 and all(e['independent_ledger'].values())
            and e.get('gates',{}).get('test_sample') and e.get('gates',{}).get('test_activity')
            and engine_hash(record['spec'])==record['spec']['engine_sha256'])
    return (e.get('passed') and set(e.get('gates',{}))==REQUIRED and all(e['gates'].values())
        and len(e.get('independent_ledger',{}))==3 and engine_hash(record['spec'])==record['spec']['engine_sha256'])


def screen(store,panel):
    """Replay every contender on identical dates/prices; never compare live P&L to a backtest."""
    from .account_selection import strict,screen as unified_screen
    if strict(store):return unified_screen(store,panel)
    policy=store.meta('promotion_policy',{})
    all_records=store.strategies(['paper','shadow'])
    if store.meta('intraday_enabled',False):
        return {'status':'minute_comparison_required','reason':'已切换分钟研究；账户候选由分钟检验与同区间比较提供。'}
    minute_ids={r['id'] for r in all_records if r['spec'].get('execution_timeframe')=='5m'}
    rotation=pending(store)
    if store.meta('account_champion') in minute_ids or rotation and rotation['target'] in minute_ids:
        return {'status':'minute_comparison_required','reason':'分钟策略使用分钟数据及同区间比较；不送入日线回放器。'}
    records=[r for r in all_records if r['id'] not in minute_ids]
    fresh=[r for r in records if r['spec'].get('research_data_sha256')==panel.metadata['input_sha256'] and valid_record(r)]
    if fresh:
        protocol=json.loads(Path(fresh[-1]['evidence']['protocol']).read_text());windows=protocol['windows']
    else:
        dates=panel.dates[220:];a,b=int(len(dates)*.6),int(len(dates)*.8)
        if len(dates)<260:raise ValueError('Insufficient common comparison history')
        windows={k:[str(dates[start].date()),str(dates[end].date())] for k,start,end in
            [('train',0,a-1),('validation',a,b-1),('retrospective_test',b,len(dates)-1)]}
    base=benchmark(panel,*windows['retrospective_test'],25)['metrics']
    ranking=[]
    for r in records:
        if not valid_record(r):continue
        compiled,p=compile_strategy(frozen_view(panel,r),r['spec'])
        runs={k:simulate(compiled,p,*w,25) for k,w in windows.items()}
        stats={k:v['metrics'] for k,v in runs.items()}
        stress={k:simulate(compiled,p,*windows[k],50)['metrics'] for k in ('validation','retrospective_test')}
        delayed={k:simulate(compiled,p,*windows[k],25,signal_lag=2)['metrics'] for k in ('validation','retrospective_test')}
        audits={k:audit_ledger(compiled,run['curve'],run['orders'],25) for k,run in runs.items()}
        test=stats['retrospective_test']
        gates={'test_sample':test['trading_days']>=52,'test_activity':test['orders']>=10,
            'drawdown':all(s['max_drawdown_pct']<=store.settings.max_drawdown_pct for s in stats.values()),
            'validation_and_test_profit':all(stats[k]['return_pct']>0 for k in ('validation','retrospective_test')),
            'high_cost_profit':all(s['return_pct']>0 for s in stress.values()),
            'delayed_profit':all(s['return_pct']>0 for s in delayed.values()),'qqq_excess':test['return_pct']>base['return_pct']}
        ranking.append({'strategy':r['id'],'stage':r['stage'],'spec_sha256':digest(r['spec']),
            'stats':stats,'stress':stress,'delayed':delayed,'independent_ledger':audits,
            'gates':gates,'eligible':all(gates.values()),'return_pct':test['return_pct'],'score':score(stats)})
    ranking.sort(key=lambda r:(not r['eligible'],-r['return_pct'],-r['score'],r['strategy']))
    qualified=[r for r in ranking if r['eligible']]
    current=store.meta('account_champion');chosen=qualified[0] if qualified else None
    incumbent=next((r for r in ranking if r['strategy']==current),None)
    # Hysteresis: a tiny ranking change must pay for switching cost and retain robustness.
    if chosen and incumbent and incumbent['eligible'] and chosen['strategy']!=current:
        if (chosen['return_pct']-incumbent['return_pct']<policy.get('minimum_return_improvement_pct',.5)
                or chosen['score']<incumbent['score']):chosen=incumbent
    result={'created_at':utcnow(),'asof':panel.metadata['end'],'data_sha256':panel.metadata['input_sha256'],
        'windows':windows,'policy':policy,'ranking':ranking,'champion':chosen['strategy'] if chosen else None,
        'comparisons':[],'items':[],
        'limitation':'统一历史区间、成本和风险门槛选优；不是未来收益保证。账户模拟与Shadow收益独立展示。'}
    if chosen:
        owners=[r['id'] for r in records if r['stage']=='paper']
        requires_change=owners!=[chosen['strategy']] or store.meta('paper_allocations',{}).get('stock',{}).get(TREASURY,0)>0
        if requires_change:
            item={'candidate':chosen['strategy'],'incumbent':current or (owners[0] if owners else None),
                'eligible':True,'candidate_score':chosen['score'],'incumbent_score':incumbent['score'] if incumbent else 0,
                'improvement':chosen['score']-(incumbent['score'] if incumbent else 0),'candidate_stats':chosen['stats'],
                'candidate_spec_sha256':chosen['spec_sha256'],'data_sha256':result['data_sha256'],'windows':windows,
                'status':'ready','reason':'统一历史区间收益择优；撤单、退出旧持仓、交收后交接','created_at':utcnow()}
            result['items']=[item];result['comparisons']=[item]
    if not chosen and any(r['stage']=='paper' for r in records):
        result['items']=[{'candidate':CASH,'incumbent':current,'eligible':True,'status':'ready',
            'reason':'没有策略通过统一验证，退出旧策略并持有现金','created_at':utcnow(),
            'candidate_score':0.,'incumbent_score':incumbent['score'] if incumbent else 0.,'improvement':0.}]
    result['comparisons']=[{'candidate':row['strategy'],'incumbent':current or result['champion'],
        'eligible':bool(chosen and row['strategy']==chosen['strategy'] and row['eligible']),
        'candidate_score':row['score'],'incumbent_score':incumbent['score'] if incumbent else (chosen['score'] if chosen else 0.),
        'improvement':row['score']-(incumbent['score'] if incumbent else (chosen['score'] if chosen else 0.)),
        'candidate_stats':row['stats'],'data_sha256':result['data_sha256'],'windows':windows,
        'reason':'本轮账户优胜策略' if chosen and row['strategy']==chosen['strategy'] else
                 '未通过共同区间门槛，不能获得账户权限' if not row['eligible'] else '已比较，收益排名或切换门槛未优于账户优胜策略'} for row in ranking]
    path=artifact(store,'champion-screen-'+digest(result)[:20],result)
    store.set_meta('promotion_screen',result);store.set_meta('promotion_queue',result['items'])
    store.event('promotion_screened',{'mode':'account_champion','asof':result['asof'],'candidates':len(ranking),
        'champion':result['champion'],'artifact':path,'queued':len(result['items'])})
    active=pending(store)
    if active and active['status']=='queued' and not active['order_ids'] and (active['data_sha256']!=result['data_sha256'] or active['target']!=(result['champion'] or CASH)):
        _save(store,active,'superseded','尚未执行的交接由新数据比较替代');active=None
    if result['items'] and not active:queue_rotation(store,result,path)
    return result


def queue_rotation(store,screen_result,artifact_path):
    """Persist the winner; a later screen cannot hot-swap an unfinished rotation."""
    from .account_selection import strict,authorized_screen
    if strict(store) and not authorized_screen(store,screen_result):
        # Legacy minute research may propose experiments, but has no account authority.
        return None
    target=screen_result['champion'] or CASH;records={r['id']:r for r in store.strategies()}
    if target!=CASH and not valid_record(records[target]):raise ValueError('Unvalidated champion')
    if target!=CASH and records[target]['evidence'].get('paper_experiment') and not store.settings.continuous_paper:
        raise ValueError('Experimental paper promotion is not authorized')
    owners=[r['id'] for r in store.strategies(['paper'])]
    rotation={'id':digest([target,owners,screen_result['data_sha256']]),'target':target,
        'outgoing':[i for i in owners if i!=target],'owners':owners,'status':'queued','created_at':utcnow(),
        'target_spec_sha256':digest(records[target]['spec']) if target!=CASH else None,'data_sha256':screen_result['data_sha256'],
        'selection_proof':screen_result.get('selection_proof'),'screen_artifact':artifact_path,'reason':'等待正常开盘后撤单及退出被淘汰策略持仓','order_ids':[]}
    store.set_meta('champion_rotation',rotation);store.event('champion_rotation_queued',rotation)
    return rotation


def _save(store,r,status,reason,selection_proof=None):
    from .account_selection import strict
    if strict(store) and r.get('selection_proof') and status=='superseded' and selection_proof!=r['selection_proof']:
        return False  # Legacy single-timeframe research cannot cancel a unified decision.
    changed=(r['status'],r.get('reason'))!=(status,reason)
    r.update(status=status,reason=reason,updated_at=utcnow());store.set_meta('champion_rotation',r)
    if changed:store.event('champion_rotation_status',{'id':r['id'],'target':r['target'],'status':status,'reason':reason})


def _cancel(store,broker,row):
    if row['status']=='reserved':store.fill(row['id'],0,0,'cancelled');return
    if row['status'] in ('unknown','submitting'):return
    key='rotation_cancel:'+row['id'];last=store.meta(key)
    if last and (pd.Timestamp(utcnow())-pd.Timestamp(last)).total_seconds()<60:return
    store.set_meta(key,utcnow())
    try:broker.cancel(row['broker_id'])
    except Exception as exc:store.event('rotation_cancel_pending',{'order':row['id'],'error_type':type(exc).__name__})
    # A cancellation response does not prove no additional fill. Reconcile next tick.


def rotation_tick(store,broker,quotes,session):
    r=pending(store)
    if not r:return False
    from .account_selection import strict,rotation_authorized
    if strict(store) and not rotation_authorized(store,r):
        working=[o for o in store.orders(active=True) if o['bucket']=='stock']
        if working:
            for order in working:_cancel(store,broker,order)
            _save(store,r,'blocked','旧接任已撤销，等待既有委托撤销及核对完成；不提交新订单')
            return True
        store.set_meta('superseded_rotation:'+r['id'],r)
        _save(store,r,'superseded','严格统一择优已启用：旧接任未通过统一比较，成交及交收记录保留',selection_proof=r.get('selection_proof'))
        return True
    if store.meta('paused'):_save(store,r,'blocked','执行暂停，等待核账');return True
    records={a['id']:a for a in store.strategies()};target=records.get(r['target'])
    if r['target']!=CASH and (not target or not valid_record(target) or digest(target['spec'])!=r['target_spec_sha256']):
        _save(store,r,'blocked','候选冻结版本或验证失效；暂停交接并保留记录');return True
    if not session:
        if r['status']=='settling':
            future=[a['settles'] for a in store.meta('unsettled',[]) if a['bucket']=='stock' and a['settles']>utcnow()]
            deadline=pd.Timestamp(max(future)).tz_convert('Asia/Shanghai').strftime('%Y-%m-%d %H:%M 北京时间') if future else None
            reason=('旧持仓已退出，等待T+1交收至 '+deadline+'；新策略尚未取得买入权限' if future else
                '旧持仓已退出，等待正常交易时段完成交收核对与资金交接')
        else:reason='等待正常交易时段；不使用盘前报价清仓'
        _save(store,r,r['status'],reason);return True
    # Cancel all old normal plans, including the retained winner's stale small-budget plan.
    active=store.orders(active=True)
    if any(o['status'] in ('unknown','submitting') for o in active):
        _save(store,r,'blocked','存在未核对提交结果');return True
    normal=[o for o in active if o['bucket']=='stock' and o['id'] not in r['order_ids']]
    if normal:
        for row in normal:_cancel(store,broker,row)
        _save(store,r,'cancelling','已请求撤旧委托，等待券商最终成交及撤单状态');return True
    held=[(i,s,p) for i in r['outgoing'] for s,p in store.strategy_portfolio(i)['positions'].items()]
    if held:
        _save(store,r,'liquidating','正在退出被淘汰策略持仓；新策略尚无买入权限')
        for owner,symbol,p in held:
            working=[o for o in store.orders(active=True) if o['strategy']==owner and o['symbol']==symbol]
            if working:
                for order in working:
                    if order['status']!='reserved':continue
                    try:
                        if order.get('order_type',1)!=3:executable_quote(quotes.get(symbol,{}),store.settings)
                    except QuoteUnavailable:continue
                    if store.claim(order['id']):
                        try:store.submitted(order['id'],broker.submit(order))
                        except Exception as exc:
                            store.submission_failed(order['id'],exc);raise
                continue
            attempts=sum(o['strategy']==owner and o['symbol']==symbol and o['id'] in r['order_ids'] for o in store.orders())
            retry_key='rotation_retry:'+r['id']+':'+owner+':'+symbol
            retry=store.meta(retry_key,{'floor':0})
            if attempts>=retry['floor']+5:
                if not store.settings.continuous_paper:
                    _save(store,r,'blocked','清仓达到5次尝试，需检查报价和成交；禁止新策略买入');continue
                if not retry.get('after'):
                    retry={**retry,'after':(pd.Timestamp(utcnow())+pd.Timedelta(minutes=5)).isoformat()}
                    store.set_meta(retry_key,retry)
                if pd.Timestamp(utcnow())<pd.Timestamp(retry['after']):
                    _save(store,r,'liquidating',symbol+' 已完成本轮尝试，5分钟后自动重试；先核对既有委托');continue
                store.set_meta(retry_key,{'floor':attempts})
                store.event('rotation_retry_reopened',{'symbol':symbol,'confirmed_attempts':attempts})
            try:
                from .simulation_policy import stock_price
                price,kind=stock_price(store,symbol,quotes.get(symbol,{}),'SELL',p.get('mark'),quote_fn=executable_quote)
            except QuoteUnavailable as exc:
                _save(store,r,'liquidating',symbol+' 清仓等待有效报价：'+str(exc));continue
            key=digest([r['id'],owner,symbol,attempts])[:32]
            # Persist intent membership before reserve/submit so restart can recover it.
            if key not in r['order_ids']:
                r['order_ids'].append(key);store.set_meta('champion_rotation',r)
            store.set_meta('rotation_order:'+key,r['id'])
            store.reserve(key,owner,'stock',symbol,'SELL',p['qty'],price,session,order_type=kind)
            if store.claim(key):
                order=next(o for o in store.orders() if o['id']==key)
                try:store.submitted(key,broker.submit(order))
                except Exception as exc:
                    store.submission_failed(key,exc);raise
        return True
    if any(o['bucket']=='stock' for o in store.orders(active=True)):
        _save(store,r,'liquidating','退出委托等待最终回执');return True
    unsettled=store.meta('unsettled',[])
    if any(a['bucket']=='stock' and a['settles']>utcnow() for a in unsettled):
        _save(store,r,'settling','旧持仓已退出，等待T+1交收后交接；不融资买入');return True
    _complete(store,r,target)
    return False


def _complete(store,r,target):
    from .account_selection import strict,rotation_authorized
    if strict(store) and not rotation_authorized(store,r):raise ValueError('Strict account admission proof invalid')
    destination=target['id'] if target else TREASURY
    stamp=utcnow();alloc=store.meta('paper_allocations',{});initial=store.meta('strategy_initial_capital',{})
    policy=store.meta('promotion_policy');old_queue=store.meta('promotion_queue',[])
    if destination in alloc['stock']:
        initial.setdefault(destination,store.strategy_portfolio(destination)['initial_capital'])
    with store.transaction() as db:
        current=json.loads(db.execute("SELECT value FROM meta WHERE key='champion_rotation'").fetchone()[0])
        if current['id']!=r['id'] or current['status'] in TERMINAL:raise ValueError('Rotation changed')
        if db.execute("SELECT 1 FROM orders WHERE bucket='stock' AND status NOT IN ('filled','cancelled','rejected')").fetchone():raise ValueError('Working stock order')
        target_cash=store.strategy_portfolio(destination,db=db)['cash'] if destination in alloc['stock'] else 0.
        for owner in list(alloc['stock']):
            if owner==destination:continue
            own=store.strategy_portfolio(owner,db=db)
            if own['positions']:raise ValueError('Outgoing owner still holds positions')
            amount=own['cash'];target_cash+=amount
            for suffix,ident,value in [('out',owner,-amount),('in',destination,amount)]:
                db.execute('INSERT INTO strategy_funding VALUES (?,?,?,?,?,?)',
                    (r['id']+'-'+owner+'-'+suffix,ident,'stock',value,stamp,'rotation'))
            initial.setdefault(owner,own['initial_capital']);alloc['stock'][owner]=0
            old=db.execute('SELECT evidence FROM strategies WHERE id=?',(owner,)).fetchone()
            if old:
                evidence=json.loads(old[0]);evidence['retirement']={'successor':destination,'at':stamp,
                    'returned_cash':amount,'reason':'账户优胜策略替换；旧持仓与委托均已清理'}
                db.execute("UPDATE strategies SET stage='retired',evidence=? WHERE id=?",(canonical(evidence),owner))
        if destination not in initial:initial[destination]=target_cash
        alloc['stock'][destination]=store.settings.stock_budget
        evidence={**(target['evidence'] if target else {}),'paper_authorization':{'source':policy['authorization'],
            'observed_at':stamp,'stock_allocation':store.settings.stock_budget,'starting_cash':target_cash,
            'account_champion':True,'forward_wait_waived':True,'forward_validation_passed':False}}
        db.execute("UPDATE strategies SET stage='paper',evidence=? WHERE id=?",(canonical(evidence),destination))
        store.set_meta('paper_allocations',alloc,db);store.set_meta('strategy_initial_capital',initial,db)
        # NAV at a funding timestamp must describe the state AFTER that flow.
        # Otherwise a pre-transfer tick at the same timestamp applies the
        # withdrawal twice and can break retired-strategy return rendering.
        session=pd.Timestamp(stamp).tz_convert('America/New_York').date().isoformat()
        for owner in alloc['stock']:
            own=store.strategy_portfolio(owner,db=db)
            db.execute('INSERT OR REPLACE INTO strategy_paper_nav VALUES (?,?,?,?,?,?,?,?)',
                (owner,stamp,session,own['nav'],own['cash'],own['unrealized_pnl'],own['realized_pnl'],own['budget']))
        store.set_meta('account_champion',destination if target else None,db)
        store.set_meta('paper_rebalance_required:'+destination,True,db)
        store.set_meta('preflight_session',None,db)
        store.set_meta('champion_rotation',{**r,'status':'completed','reason':'撤单、退出、交收和资金交接已完成','completed_at':stamp},db)
        queue=[{**a,'status':'promoted','reason':'账户资金已交接'} for a in old_queue if a['candidate']==destination]
        store.set_meta('promotion_queue',queue,db)
        store.event('paper_promoted',{'candidate':destination,'predecessors':r['outgoing'],'mode':'account_champion',
            'starting_cash':target_cash,'stock_budget':store.settings.stock_budget,'live_enabled':False},db)
