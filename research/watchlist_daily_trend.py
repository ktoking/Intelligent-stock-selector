"""Futunn-only daily trend research, read-only data and next-open simulation."""
from __future__ import annotations
import json, re, time, random
from pathlib import Path
from datetime import datetime, timezone
import pandas as pd
import numpy as np
from research.tqqq_intraday import signed_get

OUT=Path('outputs/watchlist_daily_trend_20260923')
WINDOWS=(('2024-09-01','2025-11-30'),('2025-12-01','2026-09-23'))

def download():
    OUT.mkdir(parents=True,exist_ok=True)
    symbols=json.loads(Path('outputs/watchlist_breakout_20260923/universe.json').read_text())['symbols']
    symbols=sorted(set(symbols)|{'US.QQQ','US.SPY','US.TQQQ'})
    (OUT/'universe.json').write_text(json.dumps({'symbols':symbols,'source':'Futunn current US watchlist; survivorship biased','windows':WINDOWS,'autype':1,'fetched_at_utc':datetime.now(timezone.utc).isoformat()},indent=2))
    records=[]
    for n,s in enumerate(symbols,1):
        file=OUT/(s[3:].replace('/','_')+'.pkl')
        if file.exists():records.append((s,'cached',len(pd.read_pickle(file))));continue
        rows=[];error=''
        for start,end in WINDOWS:
            for attempt in range(8):
                try:
                    result=signed_get('/api/v1.0/quote/'+s+'/history-kline',dict(start=start,end=end,ktype=2,num=370,autype=1))
                    batch=result['data']['kline_list']
                    if len(batch)>=370:raise RuntimeError('daily page may be truncated')
                    rows.extend(batch);error='';break
                except Exception as exc:
                    error=str(exc)[:180]
                    if attempt==7:break
                    m=re.search(r'retry after (\d+)s',error)
                    time.sleep((int(m.group(1))+1 if m else min(2**attempt*2,20))+random.random())
            if error:break
            time.sleep(.6)
        if rows and not error:
            f=pd.DataFrame(rows).drop_duplicates('date').sort_values('date')
            f.to_pickle(file);records.append((s,'ok',len(f)))
        else:records.append((s,'error:'+error,len(rows)))
        if n%20==0:
            pd.DataFrame(records,columns=['symbol','status','rows']).to_csv(OUT/'coverage_progress.csv',index=False)
            print('daily',n,'/',len(symbols),'last',records[-1],flush=True)
    pd.DataFrame(records,columns=['symbol','status','rows']).to_csv(OUT/'coverage.csv',index=False)
    print('daily complete',len(records),flush=True)

def extend_earlier():
    """Append a previously uninspected 2023-09..2024-08 diagnostic period."""
    symbols=json.loads((OUT/'universe.json').read_text())['symbols']
    progress=[]
    for n,s in enumerate(symbols,1):
        file=OUT/(s[3:].replace('/','_')+'.pkl')
        marker=OUT/(s[3:].replace('/','_')+'.early_done')
        if marker.exists():progress.append((s,'cached'));continue
        if not file.exists():progress.append((s,'original_missing'));continue
        error=''
        for attempt in range(8):
            try:
                r=signed_get('/api/v1.0/quote/'+s+'/history-kline',dict(start='2023-09-01',end='2024-09-01',ktype=2,num=370,autype=1))
                batch=r['data']['kline_list']
                if len(batch)>=370:raise RuntimeError('early daily page may be truncated')
                f=pd.concat([pd.read_pickle(file),pd.DataFrame(batch)]).drop_duplicates('date').sort_values('date')
                f.to_pickle(file);marker.write_text(str(len(batch)))
                progress.append((s,'ok:'+str(len(batch))));error='';break
            except Exception as exc:
                error=str(exc)[:160]
                if attempt==7:break
                m=re.search(r'retry after (\d+)s',error)
                time.sleep((int(m.group(1))+1 if m else min(2**attempt*2,20))+random.random())
        if error:progress.append((s,'error:'+error))
        if n%20==0:
            pd.DataFrame(progress,columns=['symbol','status']).to_csv(OUT/'earlier_progress.csv',index=False)
            print('earlier',n,'/',len(symbols),'last',progress[-1],flush=True)
        time.sleep(.6)
    pd.DataFrame(progress,columns=['symbol','status']).to_csv(OUT/'earlier_coverage.csv',index=False)
    print('earlier complete',len(progress),flush=True)

def load(start=None):
    symbols=json.loads((OUT/'universe.json').read_text())['symbols']
    data={};rows=[]
    for s in symbols:
        file=OUT/(s[3:].replace('/','_')+'.pkl')
        if not file.exists():rows.append((s,'missing',0));continue
        f=pd.read_pickle(file)
        if f.empty:rows.append((s,'empty',0));continue
        f.index=pd.to_datetime(f.date.astype(str),format='%Y%m%d')
        f=f[['open','high','low','close','volume']].astype(float)
        if start is not None:
            f=f.loc[pd.Timestamp(start):]
            if f.empty:rows.append((s,'empty_after_start',0));continue
        ok=(f[['open','high','low','close']]>0).all().all() and (f.volume>=0).all() and (f.high>=f[['open','low','close']].max(axis=1)).all() and (f.low<=f[['open','high','close']].min(axis=1)).all()
        if not ok or f.index.has_duplicates:rows.append((s,'invalid',len(f)));continue
        data[s]=f;rows.append((s,'ok',len(f)))
    if 'US.QQQ' in data:
        calendar=data['US.QQQ'].index
        for s,f in list(data.items()):
            expected=calendar[(calendar>=f.index.min())&(calendar<=f.index.max())]
            if not f.index.equals(expected) or f.index.max()<calendar.max():
                del data[s]
                rows.append((s,'excluded_incomplete_listing_span',len(f)))
    pd.DataFrame(rows,columns=['symbol','audit','rows']).to_csv(OUT/'audit.csv',index=False)
    return data

def features(data):
    out={}
    for s,f in data.items():
        c=f.close
        prior55=f.high.shift().rolling(55,min_periods=55).max()
        event=c>prior55
        out[s]=pd.DataFrame({'close':c,'ema50':c.ewm(span=50,adjust=False,min_periods=50).mean(),
            'ema100':c.ewm(span=100,adjust=False,min_periods=100).mean(),
            'ema150':c.ewm(span=150,adjust=False,min_periods=150).mean(),
            'mom63':c/c.shift(63)-1,
            'vol20':c.pct_change().rolling(20,min_periods=20).std(),
            'liquid20':(c*f.volume).rolling(20,min_periods=20).mean(),
            'recent_breakout':event.rolling(10,min_periods=10).max().fillna(0).astype(bool),
        },index=f.index)
    return out

def rank_on(feats, qqq, day, family, stock_types=None):
    market=qqq.loc[day] if day in qqq.index else None
    if market is None or pd.isna(market.ema100) or market['close']<=market.ema100:return []
    ranking=[]
    for s,feat in feats.items():
        if s in ('US.QQQ','US.SPY','US.TQQQ'):continue
        if stock_types is not None and stock_types.get(s)!='STOCK':continue
        if day not in feat.index:continue
        r=feat.loc[day]
        if pd.isna(r.ema100) or pd.isna(r.mom63) or pd.isna(r.vol20) or r.vol20<=0 or r.liquid20<5_000_000 or r['close']<5:continue
        if family=='momentum63':eligible=r['close']>r.ema100 and r.mom63>0;score=r.mom63
        elif family=='breakout55':eligible=r['close']>r.ema100 and r.recent_breakout and r.mom63>0;score=r.mom63
        elif family=='dual_ema':eligible=r['close']>r.ema50>r.ema150 and r.mom63>0;score=r.mom63/r.vol20
        else:raise ValueError(family)
        if eligible:ranking.append((float(score),s))
    ranking.sort(key=lambda x:(-x[0],x[1]))
    return [s for _,s in ranking[:5]]

def simulate(data,feats,days,family,bps=10,stock_types=None,vol_target=None):
    qqq=feats['US.QQQ'];dates=list(qqq.index)
    cash=10000.;held={};curve=[];deals=[];orders=[]
    first,last=days[0],days[-1]
    for d in dates:
        if d<first or d>last:continue
        previous=dates[dates.index(d)-1]
        # Every decision uses only previous completed daily data. Trades fill
        # at today's adjusted open with explicit per-side friction.
        exiting=[]
        for s,pos in held.items():
            feat=feats[s]
            if previous not in feat.index or d not in data[s].index:
                raise ValueError(f'held symbol missing adjacent session: {s} {d}')
            p=feat.loc[previous]
            if p['close']<p.ema50 or p['close']<pos['peak']*.85:exiting.append(s)
        weekly=d.weekday()==0
        targets=rank_on(feats,qqq,previous,family,stock_types) if weekly else []
        if weekly:exiting.extend(s for s in held if s not in targets)
        for s in dict.fromkeys(exiting):
            pos=held.pop(s);price=float(data[s].loc[d,'open'])*(1-bps/10000)
            cash+=pos['qty']*price
            deals.append(dict(symbol=s,entry_date=str(pos['entry_day'].date()),exit_date=str(d.date()),
                qty=pos['qty'],entry=pos['entry'],exit=price,pnl=pos['qty']*(price-pos['entry'])))
            orders.append(dict(date=str(d.date()),symbol=s,side='SELL',qty=pos['qty'],price=price))
        if weekly:
            equity_open=cash+sum(p['qty']*float(data[s].loc[d,'open']) for s,p in held.items())
            for s in targets:
                if s in held or d not in data[s].index:continue
                price=float(data[s].loc[d,'open'])*(1+bps/10000)
                target_weight=.2
                if vol_target is not None:
                    target_weight=min(target_weight,vol_target/float(feats[s].loc[previous,'vol20']))
                qty=int(min(cash,equity_open*target_weight)/price)
                if qty<1:continue
                cash-=qty*price
                held[s]=dict(qty=qty,entry=price,entry_day=d,peak=float(feats[s].loc[previous,'close']))
                orders.append(dict(date=str(d.date()),symbol=s,side='BUY',qty=qty,price=price))
        equity=cash
        for s,pos in held.items():
            price=float(data[s].loc[d,'close']);equity+=pos['qty']*price
            pos['peak']=max(pos['peak'],price)
        curve.append((d,equity,cash,len(held)))
    # End-of-window MTM; do not invent a terminal fill or omit open positions.
    frame=pd.DataFrame(curve,columns=['date','equity','cash','holdings'])
    a=np.r_[10000.,frame.equity.to_numpy()]
    stats=dict(return_pct=(a[-1]/a[0]-1)*100,max_drawdown_pct=float((1-a/np.maximum.accumulate(a)).max()*100),
        completed_trades=len(deals),orders=len(orders),holding_count_end=len(held),
        win_rate=sum(x['pnl']>0 for x in deals)/len(deals) if deals else None,
        trading_days=len(frame))
    return stats,frame,deals,orders

def buy_hold(data,symbol,days,bps=10):
    f=data[symbol];start,end=days[0],days[-1]
    qty=int(10000/(f.loc[start,'open']*(1+bps/10000)))
    cash=10000-qty*f.loc[start,'open']*(1+bps/10000)
    a=np.r_[10000.,cash+qty*f.loc[start:end,'close'].to_numpy()]
    return dict(return_pct=(a[-1]/a[0]-1)*100,max_drawdown_pct=float((1-a/np.maximum.accumulate(a)).max()*100),shares=qty)

def research():
    # Keep the initial pre-final experiment reproducible when additional older
    # history is appended later as a separate robustness diagnostic.
    data=load('2024-09-01');feats=features(data)
    dates=list(data['US.QQQ'].index)
    partitions={'train':[d for d in dates if pd.Timestamp('2025-04-01')<=d<=pd.Timestamp('2025-12-31')],
        'validation':[d for d in dates if pd.Timestamp('2026-01-01')<=d<=pd.Timestamp('2026-04-30')],
        'final':[d for d in dates if pd.Timestamp('2026-05-01')<=d<=pd.Timestamp('2026-09-22')]}
    families=('momentum63','breakout55','dual_ema')
    board={}
    for family in families:
        board[family]={}
        for label in ('train','validation'):
            stats,_,_,_=simulate(data,feats,partitions[label],family)
            board[family][label]=stats
    # Final window selected only from train+validation, with a minimum of
    # four completed trades in each window and positive net return in both.
    qualified=[s for s in families if all(board[s][p]['return_pct']>0 and board[s][p]['completed_trades']>=4 for p in ('train','validation'))]
    selected=max(qualified,key=lambda s:min(board[s][p]['return_pct'] for p in ('train','validation'))) if qualified else None
    (OUT/'selection_before_final.json').write_text(json.dumps({'board':board,'qualified':qualified,'selected':selected,'partitions':{k:[str(v[0].date()),str(v[-1].date())] for k,v in partitions.items()}},indent=2))
    result={'source':'Futunn REST split-adjusted daily K; dividends excluded','universe_count':len(data),'board':board,'qualified':qualified,'selected':selected,'windows':{},'benchmark':{}}
    for label,ds in partitions.items():
        result['benchmark'][label]={s:buy_hold(data,s,ds) for s in ('US.QQQ','US.SPY','US.TQQQ')}
    if selected:
        for label,ds in partitions.items():
            result['windows'][label]={}
            for cost in (10,25):
                stats,curve,deals,orders=simulate(data,feats,ds,selected,cost)
                result['windows'][label][str(cost)]=stats
                curve.to_csv(OUT/f'{selected}_{label}_{cost}bps_curve.csv',index=False)
                pd.DataFrame(deals).to_csv(OUT/f'{selected}_{label}_{cost}bps_trades.csv',index=False)
                pd.DataFrame(orders).to_csv(OUT/f'{selected}_{label}_{cost}bps_orders.csv',index=False)
    (OUT/'report.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2),flush=True)

def earlier_diagnostic():
    """Retro test of a rule designed after viewing 2026; not a true holdout."""
    coverage=pd.read_csv(OUT/'earlier_coverage.csv')
    if len(coverage)<len(json.loads((OUT/'universe.json').read_text())['symbols']):
        raise RuntimeError('Earlier data download is incomplete')
    data=load();feats=features(data)
    stock_types=json.loads((OUT/'security_types.json').read_text())
    days=[d for d in data['US.QQQ'].index if pd.Timestamp('2024-05-01')<=d<=pd.Timestamp('2025-03-31')]
    if not days:raise RuntimeError('No earlier testing dates')
    result={'status':'retrospective diagnostic, not an independent holdout: 2026 outcome was viewed before the stock-only volatility sizing rule was devised',
            'dates':[str(days[0].date()),str(days[-1].date())],
            'universe_count':len(data),'ordinary_stock_count':sum(stock_types.get(s)=='STOCK' for s in data),
            'source':'Futunn REST adjusted daily K, dividends excluded',
            'benchmarks':{s:buy_hold(data,s,days) for s in ('US.QQQ','US.SPY','US.TQQQ')},'variants':{}}
    for family in ('dual_ema','breakout55'):
        result['variants'][family]={}
        for bps in (10,25):
            stats,curve,deals,orders=simulate(data,feats,days,family,bps,stock_types,.01)
            result['variants'][family][str(bps)]=stats
            curve.to_csv(OUT/f'earlier_{family}_{bps}bps_curve.csv',index=False)
            pd.DataFrame(deals).to_csv(OUT/f'earlier_{family}_{bps}bps_trades.csv',index=False)
            pd.DataFrame(orders).to_csv(OUT/f'earlier_{family}_{bps}bps_orders.csv',index=False)
    (OUT/'earlier_diagnostic.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2),flush=True)

if __name__=='__main__':
    import sys
    extend_earlier() if '--extend-earlier' in sys.argv else (download() if '--download' in sys.argv else (earlier_diagnostic() if '--earlier-diagnostic' in sys.argv else research()))
