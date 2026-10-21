"""Account authority shared by daily and minute research; no broker calls here.

Original scientific gates remain mandatory. Common-window replay is a new
retrospective comparison, not additional out-of-sample or forward validation.
"""
import json
import math
from hashlib import sha256
from pathlib import Path

import pandas as pd

from .config import digest
from .exchange_clock import latest_completed_session,us_calendar
from .store import utcnow

MODE='strict_common_v1'


def version():return sha256(Path(__file__).read_bytes()).hexdigest()


def strict(store):return store.meta('promotion_policy',{}).get('selection_mode')==MODE


def qualified(record):
    from .champion import REQUIRED
    from .pipeline import engine_hash
    e=record['evidence'];g=e.get('gates',{});a=e.get('independent_ledger',{})
    return bool(not e.get('paper_experiment') and e.get('passed') and set(g)==REQUIRED and all(g.values())
        and len(a)==3 and all(a.values()) and record['spec']['engine_sha256']==engine_hash(record['spec']))


def lineage(record):
    s=record['spec']
    return digest({'timeframe':s.get('execution_timeframe','1d'),'policy':{k:v for k,v in s['policy'].items() if k!='name'},
        'weights':s.get('factor_weights'),'expression':s.get('factor_expression'),
        'minute_family':s.get('minute_family'),'minute_factor':{k:s.get('minute_factor',{}).get(k) for k in ('expression','base_family')},
        'symbols':record['evidence'].get('symbols'),'engine':s['engine_sha256']})


def activate(store,days=3):
    if type(days) is not int or not 1<=days<=30:raise ValueError('Invalid confirmation sessions')
    policy={**store.meta('promotion_policy',{}),'enabled':True,'mode':'account_champion',
        'selection_mode':MODE,'confirmation_sessions':days,'minimum_return_improvement_pct':0,
        'authorization':'2026-10-21用户授权：日K/分钟K统一成本与区间，合格策略收益择优；挑战者连续领先完整交易日后切换；亏损实验仅Shadow；仅模拟账户。'}
    from .champion import pending,_save
    r=pending(store)
    if r and not r.get('selection_proof'):
        # Keep the immutable decision, all submitted order identities and fills.
        store.set_meta('superseded_rotation:'+r['id'],r)
    store.set_meta('promotion_policy',policy)
    if r and not r.get('selection_proof') and not any(o['bucket']=='stock' for o in store.orders(active=True)):
        _save(store,r,'superseded','用户改为严格统一择优：取消亏损实验接任；已执行退出及T+1交收保留')
    store.event('strict_account_policy_activated',{'confirmation_sessions':days,'live_enabled':False})
    return policy


def authorized_screen(store,result):
    saved=store.meta('promotion_screen',{})
    if (result.get('selection_mode')!=MODE or saved.get('selector_sha256')!=version() or not result.get('selection_proof')
            or saved.get('selection_proof')!=result['selection_proof']
            or saved.get('champion')!=result.get('champion')
            or saved.get('data_sha256')!=result.get('data_sha256') or not saved.get('admission_ready')):return False
    if saved.get('asof')!=latest_completed_session():return False
    from .panel_cache import load_panel
    try:
        if load_panel(store.meta('current_data_dir',store.settings.data_dir)).metadata['input_sha256']!=saved.get('daily_data_sha256'):return False
    except (OSError,ValueError):return False
    record=next((r for r in store.strategies() if r['id']==result.get('champion')),None)
    return result.get('champion') is None or bool(record and qualified(record)
        and any(a['strategy']==record['id'] and a['eligible'] and a['spec_sha256']==digest(record['spec']) for a in saved['ranking']))


def rotation_authorized(store,r):
    result=store.meta('promotion_screen',{})
    return (r.get('selection_proof')==result.get('selection_proof') and
        r['target']==(result.get('champion') or '__cash__') and authorized_screen(store,result))


def buy_authorized(store,ident):
    # Completed authority remains valid while a qualified incumbent is retained
    # during the challenger's confirmation period. Every new close needs a screen.
    result=store.meta('promotion_screen',{})
    return bool(result.get('champion')==ident and authorized_screen(store,result))


def confirmation(store,records,ranking,asof):
    """Only distinct, consecutive, completed exchange sessions advance a lead."""
    if asof!=latest_completed_session():raise ValueError('Comparison is not for latest completed session')
    eligible=[r for r in ranking if r['eligible']]
    current=store.meta('account_champion');inc=next((r for r in eligible if r['strategy']==current),None)
    best=eligible[0] if eligible else None
    if best and inc and math.isclose(best['return_pct'],inc['return_pct'],abs_tol=1e-9):best=inc
    required=store.meta('promotion_policy')['confirmation_sessions'];old=store.meta('champion_confirmation',{})
    state={'asof':asof,'required':required,'sessions':0,'leader':best['strategy'] if best else None,
        'incumbent':current,'status':'no_qualified_candidate'}
    chosen=best
    if best:
        key=lineage(records[best['strategy']]);state['lineage']=key
        # Same rule with refreshed data is not a new challenger. Existing
        # holdings/rules stay frozen; don't rotate to a duplicate daily version.
        if inc and lineage(records[current])==key:
            chosen=inc;state['status']='incumbent_retained'
        elif inc and best['strategy']!=current:
            previous=old.get('asof');consecutive=bool(previous and str(us_calendar().next_session(previous).date())==asof)
            same=(old.get('lineage')==key and old.get('incumbent')==current)
            count=(old.get('sessions',0) if same and previous==asof else
                old.get('sessions',0)+1 if same and consecutive else 1)
            # Re-running a revised screen on one date cannot earn another day.
            state.update(sessions=count,status='confirmed' if count>=required else 'confirming')
            if count<required:chosen=inc
        else:state.update(status='initial_admission',sessions=0)
    store.set_meta('champion_confirmation',state)
    return chosen,state


def _daily(store,panel,record):
    from research.daily_factor_lab.paper import data_prefix_hash
    from .dataset import frozen_view
    from .evolution import compile_strategy
    view=frozen_view(panel,record);e=record['evidence']
    # Old price revisions cannot become approved through a ranking screen.
    if data_prefix_hash(view,e['seed_asof'])!=e['seed_hash']:
        raise ValueError('冻结日线前缀与当前行情不一致，需数据审查或新研究版本')
    return compile_strategy(view,record['spec'])


def minute_view(store,record,asof):
    from .intraday import verify
    from .intraday_data import load
    frozen=verify(record)
    if frozen['end']==asof:return frozen
    report=store.meta('intraday_research',{})
    if report.get('asof')!=asof:raise ValueError('最新分钟数据尚未完成')
    current=load(report['data_path'],report['data_sha256']);bars={}
    for symbol,rows in frozen['bars'].items():
        if symbol not in current['bars']:raise ValueError('最新分钟数据缺少冻结成员')
        # A moving download window may drop older rows; compare every overlap
        # and retain the frozen prefix. No rule, universe or history rewrite.
        old={r['end_ms']:r for r in rows};new={r['end_ms']:r for r in current['bars'][symbol]}
        overlap=set(old)&set(new)
        if not overlap or any(old[t]!=new[t] for t in overlap):raise ValueError('分钟冻结历史修订，需重新研究')
        if max(r['session'] for r in new.values())!=asof:raise ValueError('分钟成员缺少最新完成日')
        bars[symbol]=rows+[new[t] for t in sorted(new) if t>max(old)]
    return {**frozen,'end':asof,'bars':bars}


def minute_basis(panel,data,windows):
    """Conservative verified same-price gate; never guess adjustment factors."""
    import numpy as np
    first,last=windows['train'][0],windows['retrospective_test'][1]
    for symbol,rows in data['bars'].items():
        if symbol not in panel.symbols:raise ValueError('分钟与日线缺少共同价格参照')
        j=panel.symbols.index(symbol);days={}
        for r in rows:
            if first<=r['session']<=last:days.setdefault(r['session'],[]).append(r)
        for day,bars in days.items():
            i=panel.dates.get_indexer([pd.Timestamp(day)])[0]
            if i<0:raise ValueError('分钟日期缺少日线参照')
            values=[bars[0]['open'],max(r['high'] for r in bars),min(r['low'] for r in bars),bars[-1]['close']]
            reference=[panel.fields[k][i,j] for k in ('open','high','low','close')]
            if not np.allclose(values,reference,rtol=1e-4,atol=1e-5):
                raise ValueError('分钟原始价与日线复权价未通过同价核验，保持Shadow')


def screen(store,panel):
    from .champion import CASH,pending,queue_rotation,_save
    from .pipeline import artifact
    from .engine import simulate
    from .promotion import score
    from scripts.verify_daily_factor_run import audit_ledger
    # Materialize ALL originally qualified minute trials, rather than the
    # legacy validation-return winner only. queue_rotation fences their
    # legacy admission attempt until this shared screen has finished.
    from .intraday import consider_promotion,engine_hash as minute_hash
    report=store.meta('intraday_research') or {}
    if report.get('engine_sha256')==minute_hash() and report.get('asof')==panel.metadata['end']:
        for trial in report.get('trials',[]):
            if trial.get('eligible') and all(trial.get('independent_ledger',{}).values()):
                consider_promotion(store,report,trial)
    all_records={r['id']:r for r in store.strategies(['paper','shadow'])}
    asof=panel.metadata['end']
    if asof!=latest_completed_session():raise ValueError('Unified selection needs latest completed daily data')
    cost=store.settings.fee_reserve_bps;prepared={};excluded=[]
    for ident,r in all_records.items():
        if not qualified(r):
            excluded.append({'strategy':ident,'timeframe':r['spec'].get('execution_timeframe','1d'),
                'reason':'原始历史门槛未通过，实验只保留研究/Shadow'});continue
        try:
            if r['spec'].get('execution_timeframe')=='5m':
                from .intraday import verify
                data=minute_view(store,r,asof)
                prepared[ident]=('5m',data)
            else:prepared[ident]=('1d',_daily(store,panel,r))
        except (ValueError,OSError,KeyError,AssertionError) as exc:
            excluded.append({'strategy':ident,'timeframe':r['spec'].get('execution_timeframe','1d'),'reason':str(exc)[:220]})
    # A minute candidate with insufficient common history or incompatible
    # prices stays Shadow without changing the daily contenders' protocol.
    minute_ids=[k for k,v in prepared.items() if v[0]=='5m']
    common=set(str(d.date()) for d in panel.dates[220:])
    for ident in minute_ids:
        value=prepared[ident][1]
        days=set.intersection(*[{r['session'] for r in rows} for rows in value['bars'].values()])&common
        try:
            if len(days)<85 or max(days)!=asof:raise ValueError('分钟共同回放不足85个完整交易日，保持Shadow')
            ordered=sorted(days)[-85:]
            probe={'train':[ordered[0],ordered[12]],'retrospective_test':[ordered[33],ordered[-1]]}
            minute_basis(panel,value,probe)
        except (ValueError,KeyError) as exc:
            excluded.append({'strategy':ident,'timeframe':'5m','reason':str(exc)[:220]});prepared.pop(ident)
    minute=[v[1] for v in prepared.values() if v[0]=='5m']
    if minute:
        days=common.copy()
        for data in minute:
            for rows in data['bars'].values():days&={r['session'] for r in rows}
        days=sorted(days)
        if len(days)<85:
            for ident in list(prepared):
                if prepared[ident][0]=='5m':
                    excluded.append({'strategy':ident,'timeframe':'5m','reason':'候选间共同完整日期不足85日，保持Shadow'})
                    prepared.pop(ident)
            minute=[]
        else:
            days=days[-85:]
            windows={'train':[days[0],days[12]],'validation':[days[13],days[32]],'retrospective_test':[days[33],days[-1]]}
    if not minute:
        daily=next((all_records[k] for k,v in prepared.items() if v[0]=='1d'),None)
        if daily:windows=json.loads(Path(daily['evidence']['protocol']).read_text())['windows']
        else:windows={}
    ranking=[]
    # Both benchmark legs use the common daily open/close and entry cost.
    # Native replays mark final holdings; benchmark is marked, not liquidated.
    if windows:
        testdays=[str(d.date()) for d in panel.dates if windows['retrospective_test'][0]<=str(d.date())<=windows['retrospective_test'][1]]
        j=panel.symbols.index('US.QQQ');indices=panel.dates.get_indexer(pd.to_datetime(testdays))
        base=float((panel.fields['close'][indices[-1],j]/panel.fields['open'][indices[0],j]/(1+cost/10000)-1)*100)
    for ident,(timeframe,value) in prepared.items():
        r=all_records[ident]
        try:
            if timeframe=='1d':
                compiled,policy=value
                runs={k:simulate(compiled,policy,*w,cost) for k,w in windows.items()}
                stress={k:simulate(compiled,policy,*windows[k],50)['metrics'] for k in ('validation','retrospective_test')}
                delayed={k:simulate(compiled,policy,*windows[k],cost,signal_lag=2)['metrics'] for k in ('validation','retrospective_test')}
                audits={k:audit_ledger(compiled,v['curve'],v['orders'],cost) for k,v in runs.items()}
            else:
                from .intraday_engine import replay
                minute_basis(panel,value,windows)
                fn=None
                if r['spec'].get('minute_factor'):
                    from .minute_factors import signal_function
                    fn=signal_function(value,r['spec']['minute_factor'])
                subsets={k:[str(d.date()) for d in panel.dates if w[0]<=str(d.date())<=w[1]] for k,w in windows.items()}
                runs={k:replay(value,r['spec']['minute_family'],days,cost,signal_fn=fn) for k,days in subsets.items()}
                stress={k:replay(value,r['spec']['minute_family'],subsets[k],50,signal_fn=fn)['metrics'] for k in ('validation','retrospective_test')}
                delayed={k:replay(value,r['spec']['minute_family'],subsets[k],cost,1,signal_fn=fn)['metrics'] for k in ('validation','retrospective_test')}
                audits={k:v['independent_ledger'] for k,v in runs.items()}
            stats={k:v['metrics'] for k,v in runs.items()};test=stats['retrospective_test']
            gates={'test_sample':test['trading_days']>=52,'test_activity':test['orders']>=10,
                'drawdown':all(v['max_drawdown_pct']<=store.settings.max_drawdown_pct for v in stats.values()),
                'validation_and_test_profit':all(stats[k]['return_pct']>0 for k in ('validation','retrospective_test')),
                'high_cost_profit':all(v['return_pct']>0 for v in stress.values()),
                'delayed_profit':all(v['return_pct']>0 for v in delayed.values()),'qqq_excess':test['return_pct']>base,
                'independent_ledger':all(audits.values())}
            ranking.append({'strategy':ident,'timeframe':timeframe,'stage':r['stage'],'spec_sha256':digest(r['spec']),
                'stats':stats,'stress':stress,'delayed':delayed,'gates':gates,'independent_ledger':audits,
                'eligible':all(gates.values()),'return_pct':test['return_pct'],'score':score(stats)})
        except (ValueError,OSError,KeyError,AssertionError) as exc:
            excluded.append({'strategy':ident,'timeframe':timeframe,'reason':str(exc)[:220]})
    ranking.sort(key=lambda r:(not r['eligible'],-r['return_pct'],r['strategy']))
    chosen,confirm=confirmation(store,all_records,ranking,asof)
    current=store.meta('account_champion');owners=[r['id'] for r in all_records.values() if r['stage']=='paper']
    # Incomplete data is not evidence that an incumbent lost: don't initiate
    # liquidation merely because its replay could not be computed.
    incomplete=any(a['strategy']==current and '原始历史门槛' not in a['reason'] for a in excluded)
    ready=bool(chosen) or not incomplete and bool(owners)
    result={'selection_mode':MODE,'selector_sha256':version(),'created_at':utcnow(),'asof':asof,'cost_bps':cost,'stress_bps':50,
        'data_sha256':digest([panel.metadata['input_sha256'],[(k,digest(v[1]) if v[0]=='5m' else all_records[k]['spec']['research_data_sha256']) for k,v in prepared.items()]]),
        'daily_data_sha256':panel.metadata['input_sha256'],'windows':windows,'ranking':ranking,'excluded':excluded,
        'champion':chosen['strategy'] if chosen else None,'confirmation':confirm,'policy':store.meta('promotion_policy'),
        'admission_ready':ready,'items':[],'comparisons':[],
        'limitation':'统一日期与单边成本、各自冻结标的和规则的历史再比较；分钟原价/日线复权需同价核验。不是新增样本外验证或未来收益保证。'}
    if ready and (owners!=[result['champion']] or result['champion']!=current):
        result['items']=[{'candidate':result['champion'] or CASH,'incumbent':current,'eligible':True,'status':'ready',
            'candidate_return_pct':chosen['return_pct'] if chosen else None,
            'candidate_score':chosen['score'] if chosen else 0.,'incumbent_score':0.,'improvement':0.,
            'candidate_stats':chosen['stats'] if chosen else {},
            'reason':'合格候选按共同区间收益择优；'+confirm['status'],'created_at':utcnow()}]
    result['comparisons']=[{'candidate':r['strategy'],'incumbent':current,'eligible':r['eligible'],
        'candidate_score':r['score'],'incumbent_score':0.,'improvement':r['score'],'candidate_return_pct':r['return_pct'],
        'candidate_stats':r['stats'],'reason':'统一区间收益排名' if r['eligible'] else '未通过共同区间门槛'} for r in ranking]
    result['selection_proof']=digest(result)
    path=artifact(store,'strict-champion-screen-'+result['selection_proof'][:20],result)
    store.set_meta('promotion_screen',result);store.set_meta('promotion_queue',result['items'])
    store.set_meta('intraday_comparison',{'mode':MODE,'candidate':result['champion'],'eligible':bool(chosen),
        'reason':'日K/分钟K统一账户比较；亏损实验无接任权限','confirmation':confirm})
    store.event('promotion_screened',{'mode':MODE,'asof':asof,'champion':result['champion'],
        'candidates':len(ranking),'confirmation':confirm,'artifact':path})
    active=pending(store)
    if active:
        if active['target']==(result['champion'] or CASH) and ready:
            active['selection_proof']=result['selection_proof'];active['data_sha256']=result['data_sha256']
            active['screen_artifact']=path;store.set_meta('champion_rotation',active)
        elif not any(o['bucket']=='stock' for o in store.orders(active=True)):
            store.set_meta('superseded_rotation:'+active['id'],active)
            _save(store,active,'superseded','统一比较结果改变；保留成交和资金交收，不向旧候选交接',selection_proof=active.get('selection_proof'));active=None
    if result['items'] and not active:queue_rotation(store,result,path)
    return result
