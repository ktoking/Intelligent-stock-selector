"""Read-only holdings views from broker fill ownership and observed REST marks."""
import json
import math

import pandas as pd
from pathlib import Path
from functools import lru_cache

from .store import utcnow


@lru_cache(maxsize=4)
def _historical_panel(directory,manifest):
    from research.daily_factor_lab.core import load_panel
    return load_panel(directory)


def _frozen_panel(store,data_hash):
    candidates=[Path(store.meta('current_data_dir',store.settings.data_dir)),Path(store.settings.data_dir)]
    candidates+=sorted((store.home/'market-data').glob('*'),reverse=True)
    for directory in dict.fromkeys(candidates):
        if not directory.is_dir() or not (directory/'universe.json').exists():continue
        completed=directory/'completed.json'
        if completed.exists() and json.loads(completed.read_text()).get('input_sha256')!=data_hash:continue
        # Unpublished/incomplete attempts are not historical valuation evidence.
        if directory not in candidates[:2] and not completed.exists():continue
        files=sorted(directory.glob('*.pkl'))+[directory/'universe.json',directory/'security_types.json']
        manifest=tuple((p.name,p.stat().st_mtime_ns,p.stat().st_size) for p in files)
        try:panel=_historical_panel(str(directory),manifest)
        except (ValueError,FileNotFoundError):continue
        if panel.metadata['input_sha256']==data_hash:return panel
    return None


def historical_holdings(store,strategy,day=None):
    """Reconstruct hypothetical holdings on a date from the exact frozen cache."""
    record=next((r for r in store.strategies() if r['id']==strategy),None)
    if record is None:return None
    path=store.home/'artifacts'/('activity-'+strategy+'.json')
    if not path.exists():return {'available':False,'reason':'该版本缺少冻结回测交易材料。'}
    activity=json.loads(path.read_text());daily=activity.get('daily',[])
    day=day or (daily[-1]['date'] if daily else None)
    point=next((r for r in daily if r['date']==day),None)
    if point is None:return {'available':False,'reason':'该日期没有冻结回测估值。'}
    panel=_frozen_panel(store,activity.get('data_hash'))
    if panel is None:return {'available':False,'reason':'该版本的原始冻结行情不可用，无法核实逐标的持仓估值。'}
    return replay_historical_holdings(activity,panel,day)


def replay_historical_holdings(activity,panel,day):
    point=next(r for r in activity['daily'] if r['date']==day)
    held={}
    for t in activity['trades']:
        if t['date']>day:continue
        p=held.setdefault(t['symbol'],{'qty':0.,'basis':0.})
        if t['side']=='BUY':
            p['qty']+=t['qty'];p['basis']+=t['amount']+t['fee']
        else:
            if t['qty']>p['qty']+1e-9:raise ValueError('Historical holdings sale exceeds inventory')
            p['basis']-=p['basis']*t['qty']/p['qty'];p['qty']-=t['qty']
    index=panel.dates.get_loc(pd.Timestamp(day));positions=[]
    for symbol,p in held.items():
        if p['qty']<=1e-9:continue
        j=panel.symbols.index(symbol);mark=float(panel.fields['close'][index,j])
        if not math.isfinite(mark) or mark<=0:raise ValueError('Frozen historical mark missing')
        value=p['qty']*mark;pnl=value-p['basis'];basis=p['basis']
        series=[{'time':str(panel.dates[i].date()),'price':float(panel.fields['close'][i,j])} for i in range(max(0,index-59),index+1) if math.isfinite(panel.fields['close'][i,j])]
        positions.append({'symbol':symbol,'qty':p['qty'],'multiplier':1,'avg_cost':basis/p['qty'],'cost_basis':basis,
            'mark':mark,'market_value':value,'weight_pct':value/point['nav']*100,'unrealized_pnl':pnl,
            'unrealized_pct':pnl/basis*100 if basis>0 else None,'quote_time':None,'mark_date':day,'quote_status':'historical','series':series})
    positions.sort(key=lambda p:p['market_value'],reverse=True)
    value=sum(p['market_value'] for p in positions);unrealized=sum(p['unrealized_pnl'] for p in positions)
    if not math.isclose(value,point['held_value'],abs_tol=1e-6) or not math.isclose(unrealized,point['unrealized_pnl'],abs_tol=1e-6):
        raise ValueError('Historical per-symbol holdings differ from frozen daily ledger')
    return {'available':True,'kind':'retrospective_test','asof':day,'budget':10000.,'nav':point['nav'],'cash':point['cash'],
        'positions':positions,'position_count':len(positions),'held_value':value,'exposure_pct':value/point['nav']*100,
        'unrealized_pnl':unrealized,'source':'冻结回测交易与同版本复权日线；假设成交。',
        'cost_note':'历史回测初始本金 $10,000；成本含单边25bps假设费用。虚线为该日平均成本，价格为同版本复权收盘价。'}


def quote_stamp(value):
    try:
        stamp=pd.Timestamp(value,unit='ms',tz='UTC') if isinstance(value,(int,float)) else pd.Timestamp(value)
        if pd.isna(stamp):return None
        if stamp.tzinfo is None:stamp=stamp.tz_localize('America/New_York')
        return stamp.tz_convert('UTC').isoformat()
    except (ValueError,TypeError,OverflowError):return None


def record_marks(db,quotes,observed):
    """Only held assets; identical quote timestamps are not new observations."""
    for row in db.execute('SELECT bucket,symbol FROM positions WHERE qty>0').fetchall():
        q=quotes.get(row['symbol'],{});stamp=quote_stamp(q.get('update_time'))
        price=float(q.get('last_price') or 0)
        if stamp and math.isfinite(price) and price>0:
            db.execute('INSERT OR IGNORE INTO position_marks VALUES (?,?,?,?,?,?)',
                (row['bucket'],row['symbol'],stamp,observed,q.get('data_date'),price))


def _view(store,db,portfolio,bucket,calendar,identity=None,name=None):
    now=pd.Timestamp(calendar['now']);nav=portfolio.get('nav')
    if nav is None:
        nav=portfolio['cash']+sum(p['qty']*p['mark']*p['multiplier'] for p in portfolio['positions'].values())
    row=db.execute("SELECT value FROM meta WHERE key='last_tick'").fetchone();tick=json.loads(row[0]) if row else {}
    positions=[]
    for symbol,p in portfolio['positions'].items():
        qty,multiplier,mark,basis=p['qty'],p['multiplier'],p['mark'],p['basis']
        value=qty*multiplier*mark;cost=basis/(qty*multiplier);pnl=value-basis
        series=[dict(r) for r in db.execute('SELECT quote_time time,price FROM position_marks WHERE bucket=? AND symbol=? ORDER BY quote_time DESC LIMIT 120',(bucket,symbol))][::-1]
        quote=(tick.get('quotes') or {}).get(symbol,{})
        stamp=quote_stamp(quote.get('update_time')) if math.isclose(float(quote.get('price') or 0),mark,rel_tol=1e-9) else None
        if not stamp and series and math.isclose(series[-1]['price'],mark,rel_tol=1e-9):stamp=series[-1]['time']
        age=(now-pd.Timestamp(stamp)).total_seconds() if stamp else None
        status='unverified' if stamp is None else 'fresh' if calendar.get('session') and 0<=age<=store.settings.max_quote_age_seconds else 'stale' if calendar.get('session') else 'closed'
        positions.append({'symbol':symbol,'qty':qty,'multiplier':multiplier,'avg_cost':cost,'cost_basis':basis,
            'mark':mark,'market_value':value,'weight_pct':value/nav*100 if nav>0 else None,
            'unrealized_pnl':pnl,'unrealized_pct':pnl/basis*100 if basis>0 else None,
            'quote_time':stamp,'quote_status':status,'series':series,
            'price_basis':'券商模拟成交及REST估值；成本含估算费用，报价为每股/每单位价格。'})
    positions.sort(key=lambda p:p['market_value'],reverse=True)
    held=sum(p['market_value'] for p in positions)
    return {'id':identity,'name':name,'bucket':bucket,'budget':portfolio.get('budget'),
        'nav':nav,'cash':portfolio['cash'],'held_value':held,'exposure_pct':held/nav*100 if nav>0 else None,
        'unrealized_pnl':sum(p['unrealized_pnl'] for p in positions),'positions':positions,
        'position_count':len(positions),'observed_at':tick.get('time'),
        'source':'券商模拟成交增量按归属核账；估值来自程序实际观察的REST报价。',
        'cost_note':'平均成本包含估算买入费用；仓位=持仓市值/该组合净值。虚线为平均成本，曲线仅使用实际记录的报价。'}


def holdings_summary(store,calendar):
    with store.transaction() as db:
        row=db.execute("SELECT value FROM meta WHERE key='paper_allocations'").fetchone();alloc=json.loads(row[0]) if row else {}
        groups=[]
        records=[dict(r) for r in db.execute('SELECT id,spec,stage FROM strategies ORDER BY created,id')]
        owners=[r[0] for r in db.execute("SELECT DISTINCT strategy FROM orders WHERE bucket='stock'")]
        for r in records:
            if r['id'] in alloc.get('stock',{}):
                portfolio=store.strategy_portfolio(r['id'],db=db)
            elif not store.settings.parallel_paper and owners==[r['id']]:
                portfolio=store.portfolio('stock',db);portfolio['budget']=store.settings.stock_budget
            else:continue
            spec=json.loads(r['spec']);name=spec.get('factor_name',spec['policy']['name'])
            groups.append(_view(store,db,portfolio,'stock',calendar,r['id'],name)|{'stage':r['stage']})
        row=db.execute("SELECT value FROM meta WHERE key='option_paper_strategy'").fetchone();agent=json.loads(row[0]) if row else {}
        for identity,cap in alloc.get('option',{}).items():
            if cap<=0:continue
            portfolio=store.strategy_portfolio(identity,'option',db)
            groups.append(_view(store,db,portfolio,'option',calendar,identity,agent.get('spec',{}).get('name','期权模拟策略'))|{'stage':'paper'})
        books={}
        for bucket,budget in [('stock',store.settings.stock_budget),('option',store.settings.option_budget)]:
            portfolio=store.attributed_portfolio(bucket,db);portfolio['budget']=budget
            books[bucket]=_view(store,db,portfolio,bucket,calendar,name='股票模拟组合' if bucket=='stock' else '期权模拟组合')
    return {'strategies':groups,**books,'created':utcnow()}
