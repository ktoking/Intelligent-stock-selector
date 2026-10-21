"""Versioned 5m research, ongoing observations, and broker-paper routing.

This module never clears a risk pause or reuses a daily backtest as minute proof.
"""
from dataclasses import asdict
from contextlib import nullcontext
from datetime import date,timedelta
from hashlib import sha256
import json
import math
from pathlib import Path

import pandas as pd

from . import intraday_data as bars_api,intraday_engine as engine
from .broker import FutuSim,executable_quote,BrokerError,QuoteUnavailable
from .config import digest,canonical
from .exchange_clock import us_calendar,latest_completed_session
from .files import atomic_write
from .store import utcnow


def is_minute(record):return record['spec'].get('execution_timeframe')=='5m'


def engine_hash():
    factor=Path(__file__).with_name('minute_factors.py')
    return sha256(Path(engine.__file__).read_bytes()+Path(__file__).read_bytes()+Path(bars_api.__file__).read_bytes()+(factor.read_bytes() if factor.exists() else b'')).hexdigest()


def verify(record):
    spec=record['spec'];e=record['evidence']
    if spec['engine_sha256']!=engine_hash():raise BrokerError('Frozen five-minute engine changed')
    value=bars_api.load(e['minute_data_path'],spec['research_data_sha256'])
    if value['symbols']!=e['symbols']:raise BrokerError('Frozen minute universe changed')
    if spec.get('minute_factor'):
        from .minute_factors import verify_source
        verify_source(spec['minute_factor'])
    return value


def select_universe(store,limit=8):
    # Each research version freezes this selection. New movers enter a later
    # version; current index membership never masquerades as historical PIT.
    held=list(store.portfolio()['positions']);hits=store.meta('universe_scan',{}).get('hits',[])
    liquid=[h['symbol'] for h in hits if float(h.get('price') or 0)>=5]
    return list(dict.fromkeys(['US.QQQ','US.SPY','US.NVDA']+held+liquid))[:limit]


def research(store,symbols=None,days=120,broker=None,locked=False):
    from .checkpoints import run_step,workflow_lock
    from .pipeline import artifact
    asof=latest_completed_session();symbols=symbols or select_universe(store)
    start=str(date.fromisoformat(asof)-timedelta(days=days))
    version=digest(['5m-research-v2',symbols,start,asof,engine_hash()])
    with (nullcontext() if locked else workflow_lock(store)):
        path,key,data=run_step(store,'minute_download',asof,version,
            lambda:list(bars_api.collect(store,symbols,start,asof,broker)))
        sessions=sorted(set.intersection(*[{r['session'] for r in rows} for rows in data['bars'].values()]))
        if len(sessions)<20:raise ValueError('Minute research has fewer than 20 complete common sessions')
        a,b=int(len(sessions)*.5),int(len(sessions)*.75)
        windows={'train':sessions[:a],'validation':sessions[a:b],'retrospective_test':sessions[b:]}
        def evaluate():
            trials=[]
            for family in engine.FAMILIES:
                runs={label:engine.replay(data,family,subset,store.settings.fee_reserve_bps) for label,subset in windows.items()}
                stress={label:engine.replay(data,family,windows[label],50)['metrics'] for label in ('validation','retrospective_test')}
                delayed={label:engine.replay(data,family,windows[label],store.settings.fee_reserve_bps,1)['metrics'] for label in ('validation','retrospective_test')}
                stats={k:r['metrics'] for k,r in runs.items()}
                qqq=[r for r in data['bars']['US.QQQ'] if r['session'] in windows['retrospective_test']]
                base=(qqq[-1]['close']/qqq[0]['open']*(1-store.settings.fee_reserve_bps/10000)**2-1)*100
                gates={'test_sample':len(windows['retrospective_test'])>=20,'test_activity':stats['retrospective_test']['orders']>=10,
                    'drawdown':all(v['max_drawdown_pct']<=store.settings.max_drawdown_pct for v in stats.values()),
                    'validation_and_test_profit':all(stats[k]['return_pct']>0 for k in ('validation','retrospective_test')),
                    'high_cost_profit':all(v['return_pct']>0 for v in stress.values()),
                    'delayed_profit':all(v['return_pct']>0 for v in delayed.values()),'qqq_excess':stats['retrospective_test']['return_pct']>base}
                trials.append({'family':family,'timeframe':'5m','stats':stats,'stress':stress,'delayed':delayed,
                    'benchmark':{'return_pct':base,'source':'Futunn unadjusted QQQ minute bars; buy and hold same test dates'},
                    'gates':gates,'eligible':all(gates.values()),'independent_ledger':{k:r['independent_ledger'] for k,r in runs.items()},
                    'failed_gates':[k for k,v in gates.items() if not v],'test_run':runs['retrospective_test']})
            return {'created_at':utcnow(),'asof':asof,'timeframe':'5m','source':bars_api.SOURCE,'symbols':symbols,
                'data_path':path,'data_sha256':key,'engine_sha256':engine_hash(),'coverage':data['coverage'],
                'common_sessions':len(sessions),'windows':{k:[v[0],v[-1]] for k,v in windows.items()},
                'cost_bps':store.settings.fee_reserve_bps,'stress_bps':50,'signal_lag_bars':1,'trials':trials,
                'selection_bias':data['universe_bias'],'status':'validated' if any(t['eligible'] for t in trials) else 'no_qualified_candidate',
                'rules':'仅完整5分钟K；15分钟确认；次根开盘回放；最多2只、各20%；卖出资金T+1；每日每标的一次开仓；收盘前退出。'}
        report=run_step(store,'minute_backtest',asof,digest([version,key]),evaluate)
        report_path=artifact(store,'minute-research-'+digest(report)[:20],report)
        store.set_meta('intraday_research',{**report,'report_path':report_path})
        store.event('minute_research_completed',{'asof':asof,'common_sessions':len(sessions),'qualified':sum(t['eligible'] for t in report['trials']),'report':report_path})
        qualified=sorted([t for t in report['trials'] if t['eligible']],key=lambda t:-t['stats']['retrospective_test']['return_pct'])
        if store.settings.continuous_paper and store.meta('intraday_enabled',False):select_paper_candidate(store,report)
        elif qualified and store.meta('intraday_enabled',False):consider_promotion(store,report,qualified[0])
        elif not qualified and store.meta('intraday_enabled',False):
            from .champion import pending,queue_rotation
            if store.strategies(['paper']) and not pending(store):
                queue_rotation(store,{'champion':None,'data_sha256':report['data_sha256']},report_path)
        store.set_meta('task:minute_research',{'status':'completed','asof':asof,'finished_at':utcnow(),
            'data_dir':store.meta('current_data_dir',store.settings.data_dir),'reason':str(len(qualified))+' 个候选通过分钟历史门槛'})
        store.set_meta('task:minute_promotion',{'status':'completed','asof':asof,'finished_at':utcnow(),
            'data_dir':store.meta('current_data_dir',store.settings.data_dir),
            'reason':'无合格分钟候选；按既有交接流程退出旧策略并持现金，风险暂停期间不执行' if not qualified else (store.meta('intraday_comparison') or {}).get('reason','候选等待启用后比较')})
        return report


def consider_promotion(store,report,trial):
    """Minute-vs-daily compares identical dates and cash cost; existing rotation owns funds."""
    from research.daily_factor_lab.core import Policy
    from .pipeline import artifact
    experiment=not trial['eligible']
    spec={'schema_version':1,'instrument':'stock','execution_timeframe':'5m','origin':'codex_minute_factor' if trial.get('factor') else 'fixed_minute_catalog',
        'policy':asdict(Policy(trial['family']+'_5m')),'minute_family':trial['family'],
        'minute_cost_bps':report['cost_bps'],
        'hypothesis':report['rules'],'research_symbols':report['symbols'],
        'engine_sha256':report['engine_sha256'],'research_data_sha256':report['data_sha256']}
    if trial.get('factor'):spec.update(minute_factor=trial['factor'],factor_name=trial['factor']['name'],hypothesis=trial['factor']['hypothesis'])
    key=store.propose(spec);record=next(r for r in store.strategies() if r['id']==key)
    evidence={**{k:v for k,v in trial.items() if k!='test_run'},'passed':not experiment,'symbols':report['symbols'],
        'seed_asof':report['asof'],'seed_hash':report['data_sha256'],'minute_data_path':report['data_path'],
        'windows':report['windows'],'protocol':report['report_path'] if report.get('report_path') else store.meta('intraday_research')['report_path'],
        'engine_sha256':spec['engine_sha256'],'shadow_started_at':utcnow()}
    if experiment:evidence.update(paper_experiment=True,experiment_authorization='2026-10-20',validation_status='failed_profitability_gates; simulated experiment only')
    for before,after in [('proposed','backtested'),('backtested','validated'),('validated','shadow')]:
        if record['stage']==before:
            store.transition(key,before,after,evidence);record=next(r for r in store.strategies() if r['id']==key)
    from .activity import ledger_activity
    run=trial['test_run'];curve=[{**p,'cash':p['equity']} for p in run['curve']]
    orders=[{'date':t['session'],'symbol':t['symbol'],'side':t['side'],'qty':t['qty'],'raw_price':t['price'],
        'cost':t['fee'],'reason':'已完成5分钟信号；下一根开盘假设成交'} for t in run['trades']]
    activity=ledger_activity(curve,orders)
    artifact(store,'activity-'+key,{**activity,'strategy':key,'data_hash':report['data_sha256'],'kind':'retrospective_test',
        'source':bars_api.SOURCE,'curve':curve,'cost_bps':report['cost_bps'],'timeframe':'5m','limitation':'历史分钟假设成交，非券商模拟成交。'})
    artifact(store,'chart-'+key,{'strategy':key,'data_hash':report['data_sha256'],'kind':'retrospective_test','curve':run['minute_curve'],
        'benchmark_curve':[],'window':report['windows']['retrospective_test'],'cost_bps':report['cost_bps'],'source':bars_api.SOURCE,'timeframe':'5m'})
    if record['stage']!='shadow':return
    incumbent=next((r for r in store.strategies(['paper']) if r['id']==store.meta('account_champion')),None)
    comparison={'candidate':key,'eligible':False,'reason':'等待同区间账户策略比较'}
    if incumbent and not store.settings.continuous_paper:
        if is_minute(incumbent):
            data=bars_api.load(report['data_path'],report['data_sha256'])
            fn=None
            if incumbent['spec'].get('minute_factor'):
                from .minute_factors import signal_function
                fn=signal_function(data,incumbent['spec']['minute_factor'])
            stats={k:engine.replay(data,incumbent['spec']['minute_family'],
                sorted({r['session'] for r in data['bars']['US.QQQ'] if w[0]<=r['session']<=w[1]}),report['cost_bps'],signal_fn=fn)['metrics'] for k,w in report['windows'].items()}
        else:
            from .panel_cache import load_panel
            from .dataset import frozen_view
            from .evolution import compile_strategy
            from .engine import simulate
            panel=load_panel(store.meta('current_data_dir',store.settings.data_dir));panel,policy=compile_strategy(frozen_view(panel,incumbent),incumbent['spec'])
            stats={k:simulate(panel,policy,*w,report['cost_bps'])['metrics'] for k,w in report['windows'].items()}
        lead=trial['stats']['retrospective_test']['return_pct']-stats['retrospective_test']['return_pct']
        from .promotion import score
        stable=all(trial['stats'][k]['max_drawdown_pct']<=stats[k]['max_drawdown_pct'] for k in ('validation','retrospective_test')) and score(trial['stats'])>=score(stats)
        comparison.update(eligible=lead>=.5 and stable,improvement_pct=lead,incumbent_stats=stats,reason='同区间分钟策略收益领先且回撤不增加' if lead>=.5 and stable else '同区间收益或稳健性未优于在任者')
    else:comparison.update(eligible=True,reason='用户授权连续模拟实验；按同版本验证区间择优，盈利检验结果保留' if store.settings.continuous_paper else '分钟策略通过独立验证，账户无在任优胜策略')
    store.set_meta('intraday_comparison',comparison)
    from .champion import queue_rotation,pending
    active=pending(store)
    if comparison['eligible'] and active and not active['order_ids'] and not store.orders(active=True) and active['target']!=key:
        from .champion import _save
        _save(store,active,'superseded','尚无委托的交接由最新分钟实验候选替代')
    if comparison['eligible'] and not pending(store):
        material=artifact(store,'minute-promotion-'+digest([key,comparison])[:20],comparison)
        queue_rotation(store,{'champion':key,'data_sha256':report['data_sha256']},material)


def select_paper_candidate(store,report):
    valid=[t for t in report['trials'] if (t['eligible'] or store.settings.continuous_paper
        and t['gates'].get('test_sample') and t['gates'].get('test_activity')
        and all(t['independent_ledger'].values()))]
    if not valid:return
    chosen=max(valid,key=lambda t:(t['eligible'],t['stats']['validation']['return_pct']))
    current=next((r for r in store.strategies(['paper']) if r['id']==store.meta('account_champion')),None)
    if current and is_minute(current) and current['spec']['research_data_sha256']==report['data_sha256'] and current['spec']['minute_family']==chosen['family']:
        return
    consider_promotion(store,report,chosen)


def observe(store):
    """Network worker only; no broker order endpoint is reachable here."""
    from .broker import session_now
    session=session_now()
    if not session:return
    now=pd.Timestamp.now(tz='UTC');opening=us_calendar().session_open(session)
    boundary=opening+pd.Timedelta(seconds=int((now-opening).total_seconds()//300)*300)
    if boundary<=opening:return
    last=store.meta('intraday_observation',{})
    if last.get('boundary')==boundary.isoformat() and last.get('status')=='ok':return
    broker=FutuSim(store.settings);report=store.meta('intraday_research') or {}
    records=[r for r in store.strategies(['paper','shadow']) if is_minute(r)]
    symbols=list(dict.fromkeys((report.get('symbols') or select_universe(store))+[s for r in records for s in r['evidence']['symbols']]))
    bars={};errors=[]
    for symbol in symbols:
        try:
            rows=bars_api.fetch(broker,symbol,session,session,now)
            expected=[int(t.timestamp()*1000) for t in pd.date_range(opening+pd.Timedelta(minutes=5),boundary,freq='5min')]
            if [r['end_ms'] for r in rows]!=expected:raise QuoteUnavailable('Missing closed minute candles: '+symbol)
            bars[symbol]=rows
        except Exception as exc:errors.append({'symbol':symbol,'error_type':type(exc).__name__})
    if not bars:raise QuoteUnavailable('No complete same-session five-minute candles available')
    factor_signals=[]
    for record in records:
        if record['spec'].get('minute_factor') and 'US.QQQ' in bars:
            try:
                from .minute_factors import signal_function
                fn=signal_function({'bars':bars},record['spec']['minute_factor'])
                factor_signals.extend({'strategy':record['id'],'symbol':s,'family':record['spec']['minute_family'],
                    'signal':fn(s,rows,False),'bar_close':rows[-1]['end_ms']} for s,rows in bars.items())
            except ValueError as exc:errors.append({'strategy':record['id'],'error_type':type(exc).__name__})
    value={'observed_at':utcnow(),'session':session,'boundary':boundary.isoformat(),'source':bars_api.SOURCE,
        'status':'partial' if errors else 'ok','bars':bars,'errors':errors,'signals':[
            {'symbol':symbol,'family':family,'signal':engine.decision(rows,family),'bar_close':rows[-1]['end_ms']}
            for symbol,rows in bars.items() for family in engine.FAMILIES]+factor_signals}
    # Failed symbols retry next minute; completed same-boundary observations
    # are reused. They never create retrospective paper fills.
    atomic_write(store.home/'intraday-observed'/session/(str(int(boundary.timestamp()))+'.json'),canonical(value))
    store.set_meta('intraday_observation',value)
    store.event('minute_observed',{'session':session,'bar_close':boundary.isoformat(),'symbols':len(bars),'errors':len(errors)})


def execute(store,broker,record,quotes,session):
    """Every closed bar may generate an intent; still uses existing cash/ack fences."""
    if not session:return {'status':'closed'}
    verify(record)
    if record['spec']['minute_cost_bps']!=store.settings.fee_reserve_bps:raise BrokerError('Frozen minute execution cost changed')
    from .champion import pending
    if store.meta('paused'):return {'status':'paused','reason':store.meta('pause_reason')}
    if pending(store) or store.meta('account_champion')!=record['id']:return {'status':'rotating'}
    observation=store.meta('intraday_observation') or {};now=pd.Timestamp.now(tz='UTC')
    if observation.get('session')!=session or observation.get('status') not in ('ok','partial'):return {'status':'blocked','reason':'等待完整分钟K线'}
    boundary=pd.Timestamp(observation['boundary'])
    if not 0<=(now-boundary).total_seconds()<=120:return {'status':'blocked','reason':'分钟信号过期，等待下一根已完成K线'}
    working=[o for o in store.orders(active=True) if o['bucket']=='stock']
    if working:
        for row in working:
            if row['strategy']!=record['id'] or row['status']!='reserved':continue
            if row.get('order_type',1)!=3:executable_quote(quotes.get(row['symbol'],{}),store.settings)
            if store.claim(row['id']):
                try:store.submitted(row['id'],broker.submit(row))
                except Exception as exc:store.submission_failed(row['id'],exc);raise
        return {'status':'working'}
    book=store.strategy_portfolio(record['id']);closing=us_calendar().session_close(session)
    factor_signal=None
    if record['spec'].get('minute_factor'):
        from .minute_factors import signal_function
        factor_signal=signal_function({'bars':observation['bars']},record['spec']['minute_factor'])
    orders=[];missing=[]
    held_missing=any(not observation['bars'].get(symbol) for symbol in book['positions'])
    for symbol in sorted(record['evidence']['symbols'],key=lambda s:(s not in book['positions'],s)):
        rows=observation['bars'].get(symbol);held=book['positions'].get(symbol)
        if not rows:missing.append(symbol);continue
        signal=factor_signal(symbol,rows,bool(held)) if factor_signal else engine.decision(rows,record['spec']['minute_family'],bool(held));last=rows[-1]['close']
        if held and (signal=='SELL' or now>=closing-pd.Timedelta(minutes=10) or last<=held['basis']/held['qty']*.985 or last>=held['basis']/held['qty']*1.03):
            side='SELL';qty=held['qty']
        elif not held and signal=='BUY' and now<closing-pd.Timedelta(minutes=30):
            attempted=any(o['strategy']==record['id'] and o['session']==session and o['symbol']==symbol and o['side']=='BUY'
                and not (o['status']=='rejected' and o['filled']==0) for o in store.orders())
            if attempted or held_missing or len(book['positions'])+sum(o['side']=='BUY' for o in orders)>=2:continue
            side='BUY';qty=None
        else:continue
        from .simulation_policy import stock_price
        price,kind=stock_price(store,symbol,quotes.get(symbol,{}),side,last,quote_fn=executable_quote)
        if qty is None:
            from .capital import stock_cap
            nav=book['nav'];available=min(book['cash'],stock_cap(store)-sum(p['qty']*p['mark'] for p in book['positions'].values()))
            qty=int(min(nav*.2,available)/(price*(1+store.settings.fee_reserve_bps/10000)))
        if qty<=0:continue
        attempts=sum(o['strategy']==record['id'] and o['symbol']==symbol and o['side']==side
            and (store.meta('minute_order:'+o['id']) or {}).get('signal_close')==observation['boundary'] for o in store.orders())
        if attempts>=3:continue
        key=digest(['minute-intent-v2',record['id'],session,observation['boundary'],symbol,side,attempts])[:32]
        if store.reserve(key,record['id'],'stock',symbol,side,qty,price,session,order_type=kind):
            store.set_meta('minute_order:'+key,{'signal_close':observation['boundary'],'family':record['spec']['minute_family']})
        row=next(o for o in store.orders() if o['id']==key)
        if row['status']=='reserved' and store.claim(key):
            try:store.submitted(key,broker.submit(row))
            except Exception as exc:store.submission_failed(key,exc);raise
        orders.append(row)
        # Reconcile before another name: broker fills and reservations always
        # precede allocation of the remaining shared cash.
        break
    status={'status':'partial' if missing else 'checked','timeframe':'5m','signal_close':observation['boundary'],
        'orders':len(orders),'missing_symbols':missing,'observed_at':utcnow()}
    store.set_meta('intraday_execution',status);return status


def view(store):
    research=store.meta('intraday_research') or {};observed=store.meta('intraday_observation') or {}
    return {'enabled':store.meta('intraday_enabled',False),'timeframe':'5m','check_seconds':60,
        'research':{k:v for k,v in research.items() if k not in ('trials',)},
        'trials':[{k:v for k,v in t.items() if k!='test_run'} for t in research.get('trials',[])],
        'observation':{k:v for k,v in observed.items() if k!='bars'},'execution':store.meta('intraday_execution'),
        'comparison':store.meta('intraday_comparison'),'download':store.meta('intraday_download'),
        'factor_research':store.meta('minute_factor_latest'),'factor_registry':store.meta('minute_factor_registry',[]),
        'continuous_paper':store.settings.continuous_paper,'stock_market_fallback':store.settings.stock_market_fallback,
        'risk_policy':'亏损阈值仅提示；账户、现金和未知委托仍须核对' if store.settings.continuous_paper else '亏损阈值暂停',
        'execution_blocked':bool(store.meta('paused')),'pause_reason':store.meta('pause_reason')}
