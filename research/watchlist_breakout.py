"""Read-only Futunn watchlist minute downloader and breakout study."""
from __future__ import annotations
import json, time, random, re
from pathlib import Path
from datetime import datetime, timezone
import pandas as pd
import numpy as np
from research.tqqq_intraday import signed_get

OUT=Path('outputs/watchlist_breakout_20260923')
WINDOWS=(('2026-08-24','2026-09-07'),('2026-09-07','2026-09-23'))

def collect():
    OUT.mkdir(parents=True,exist_ok=True)
    response=signed_get('/api/v1.0/quote/user-security',{'group_name':'US'})
    items=response['data']['security_list']
    symbols=sorted({v['code'] for v in items if v['stock_type'] in ('STOCK','ETF') and v['code'].startswith('US.')})
    manifest={'fetched_at_utc':datetime.now(timezone.utc).isoformat(),'group':'US','group_total':len(items),'eligible':len(symbols),'symbols':symbols,'source':'Futunn REST user-security group US; current constituents, survivorship biased','windows':WINDOWS}
    (OUT/'universe.json').write_text(json.dumps(manifest,indent=2))
    records=[]
    for n,s in enumerate(symbols,1):
        path=OUT/(s[3:].replace('/','_')+'.pkl')
        if path.exists():
            records.append((s,'cached',len(pd.read_pickle(path))))
            continue
        rows=[];error=''
        for start,end in WINDOWS:
            for attempt in range(8):
                try:
                    response=signed_get('/api/v1.0/quote/'+s+'/history-kline',dict(start=start,end=end,ktype=6,num=370,autype=0))
                    batch=response['data']['kline_list']
                    if len(batch)>=1000:raise RuntimeError('truncated 1000-bar batch')
                    rows.extend(batch);break
                except Exception as exc:
                    error=str(exc)[:180]
                    if attempt==7:break
                    retry=re.search(r'retry after (\d+)s',error)
                    time.sleep((int(retry.group(1))+1 if retry else min(2**attempt*2,20))+random.random())
            if error and attempt==7:break
            time.sleep(.8)
        if rows and not (error and attempt==7):
            frame=pd.DataFrame(rows).drop_duplicates('time_key').sort_values('time_key')
            frame.to_pickle(path)
            records.append((s,'ok',len(frame)))
        else:records.append((s,'error:'+error,len(rows)))
        if n%10==0:
            print('downloaded',n,'/',len(symbols),'last',records[-1],flush=True)
            pd.DataFrame(records,columns=['symbol','status','rows']).to_csv(OUT/'coverage_progress.csv',index=False)
    pd.DataFrame(records,columns=['symbol','status','rows']).to_csv(OUT/'coverage.csv',index=False)
    print('complete',len(records),flush=True)

def audit():
    import exchange_calendars as xc
    cal=xc.get_calendar('XNYS')
    sessions=cal.sessions_in_range('2026-08-24','2026-09-22')
    expected=pd.DatetimeIndex([t for d in sessions for t in pd.date_range(cal.session_open(d),cal.session_close(d),freq='5min',inclusive='left')])
    valid={};coverage=[]
    universe=json.loads((OUT/'universe.json').read_text())['symbols']
    for s in universe:
        path=OUT/(s[3:].replace('/','_')+'.pkl')
        if not path.exists():coverage.append((s,'download_error',0,0));continue
        f=pd.read_pickle(path)
        if f.empty:coverage.append((s,'empty',0,0));continue
        f.index=pd.DatetimeIndex(pd.to_datetime(f.time_key,unit='ms',utc=True).dt.tz_convert('America/New_York'))-pd.Timedelta(minutes=5)
        missing=len(expected.difference(f.index.tz_convert('UTC')))
        extra=len(f.index.tz_convert('UTC').difference(expected))
        jump=(f.close/f.close.shift()).sub(1).abs().max()
        clean=missing==0 and extra==0 and not f.index.has_duplicates and jump<.4 and (f[['open','high','low','close']]>0).all().all() and (f.volume>=0).all()
        coverage.append((s,'complete' if clean else ('large_jump' if jump>=.4 else 'incomplete'),missing,extra))
        if clean:valid[s]=f
    pd.DataFrame(coverage,columns=['symbol','audit_status','missing_bars','extra_bars']).to_csv(OUT/'audit.csv',index=False)
    return valid

def events(data, family):
    all_events=[]
    for s,f in data.items():
        day=f.index.strftime('%Y-%m-%d')
        slot=f.index.strftime('%H:%M')
        prior_volume=f.volume.groupby(day).transform('sum').groupby(day).first().shift(1)
        prior_dollar=(f.close*f.volume).groupby(day).transform('sum').groupby(day).first().shift(1)
        # Daily eligibility is derived only from the preceding session.
        daily_dollar=(f.close*f.volume).groupby(day).sum()
        liquid=pd.Series(day,index=f.index).map(daily_dollar.shift(1)).fillna(0)>1_000_000
        first30=pd.Series(f.high.where(slot<'10:00'),index=f.index).groupby(day).transform('max')
        close=f.close
        ema20=close.ewm(span=20,adjust=False).mean()
        vwap=((f.high+f.low+close)/3*f.volume).groupby(day).cumsum()/f.volume.groupby(day).cumsum()
        prev_high=f.high.shift().rolling(12).max()
        prior_day_close=close.groupby(day).transform('first').groupby(day).first().shift(1)
        gap=pd.Series(day,index=f.index).map(close.groupby(day).last().shift(1))
        # Relative volume uses only bars earlier today and previous daily volume;
        # historical intraday denominator comes from previous trading sessions.
        typical_vol=f.volume.groupby(slot).transform(lambda x:x.shift(1).rolling(5,min_periods=3).median())
        rvol=f.volume/typical_vol.replace(0,np.nan)
        base=(slot>='10:00')&(slot<='14:30')&liquid&(f.open>=5)&(close>ema20)&(rvol>=1.2)
        if family=='opening30':trigger=close>first30
        elif family=='rolling12':trigger=close>prev_high
        elif family=='opening30_vwap':trigger=(close>first30)&(close>vwap)
        else:raise ValueError(family)
        signal=base&trigger
        for i in np.flatnonzero(signal):
            if i+1>=len(f) or day[i+1]!=day[i]:continue
            all_events.append((day[i],f.index[i+1],s,float(rvol.iloc[i]),float(f.open.iloc[i+1]),i+1))
    return all_events

def simulate(data, evs, days, friction_bps=10):
    cash=10000.;curve=[cash];deals=[];daily=[]
    dayset=set(days);candidates={}
    for day,t,s,rvol,entry,i in evs:
        if day in dayset:candidates.setdefault(day,[]).append((t,-rvol,s,entry,i))
    for day in days:
        begin=cash;position=None
        for t,neg_rvol,s,entry,i in sorted(candidates.get(day,[])):
            if position is not None:break
            f=data[s];quantity=int(cash*.8/(entry*(1+friction_bps/10000)))
            if quantity<1:continue
            buy=entry*(1+friction_bps/10000);cash-=quantity*buy
            peak=buy;exitprice=None;exit_time=None;reason='session_end'
            session=f.loc[day]
            local_i=i-np.flatnonzero(f.index.strftime('%Y-%m-%d')==day)[0]
            for j in range(local_i,len(session)):
                row=session.iloc[j];clock=session.index[j].strftime('%H:%M')
                if clock>='15:50':exitprice=row.open;exit_time=session.index[j];break
                peak=max(peak,row.close)
                if row.close<=buy*.98 or row.close<=peak*.975:
                    if j+1<len(session):
                        exitprice=session.open.iloc[j+1];exit_time=session.index[j+1];reason='close_confirmed_stop'
                    break
            if exitprice is None:raise ValueError('no exit on complete session')
            sell=exitprice*(1-friction_bps/10000);cash+=quantity*sell
            deals.append(dict(day=day,symbol=s,entry_time=str(t),exit_time=str(exit_time),qty=quantity,entry=buy,exit=sell,pnl=quantity*(sell-buy),reason=reason,rvol=-neg_rvol))
            position=True
        curve.append(cash)
        daily.append(dict(day=day,equity=cash,return_pct=(cash/begin-1)*100))
    curve=np.array(curve)
    return dict(return_pct=(cash/10000-1)*100,max_drawdown_pct=float((1-curve/np.maximum.accumulate(curve)).max()*100),trades=len(deals),win_rate=sum(x['pnl']>0 for x in deals)/len(deals) if deals else None),deals,daily

def research():
    data=audit()
    days=sorted(set(next(iter(data.values())).index.strftime('%Y-%m-%d')))
    train,holdout=days[3:-6],days[-6:]
    families=('opening30','rolling12','opening30_vwap')
    board={};event_cache={}
    for family in families:
        ev=events(data,family);event_cache[family]=ev
        board[family]=simulate(data,ev,train)[0]
    selected=max(families,key=lambda f:board[f]['return_pct'])
    (OUT/'selection_before_holdout.json').write_text(json.dumps(dict(train=train,holdout=holdout,board=board,selected=selected,eligible_symbols=len(data)),indent=2))
    results={}
    for label,ds in [('train',train),('holdout',holdout),('all_after_warmup',days[3:])]:
        for bps in (10,20):
            summary,deals,daily=simulate(data,event_cache[selected],ds,bps)
            results[f'{label}_{bps}bps']=summary
            pd.DataFrame(deals).to_csv(OUT/f'{label}_{bps}bps_trades.csv',index=False)
            pd.DataFrame(daily).to_csv(OUT/f'{label}_{bps}bps_daily.csv',index=False)
    report=dict(source='Futunn REST user-security and unadjusted history-kline 5m',universe_count=len(json.loads((OUT/'universe.json').read_text())['symbols']),eligible_count=len(data),train=train,holdout=holdout,board=board,selected=selected,results=results,assumptions='10000 USD; 80% allocation; one trade per day; signal on completed 5m bar and next open fill; 10/20bps each side; flat by 15:50; current watchlist survivor bias')
    (OUT/'report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))

if __name__=='__main__':
    import sys
    research() if '--research' in sys.argv else collect()
