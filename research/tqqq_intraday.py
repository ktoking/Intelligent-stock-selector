"""Read-only Futunn data audit and causal intraday research. No order API."""
import os, json, time, base64, secrets, urllib.request, urllib.parse
from pathlib import Path
from datetime import date, timedelta
from cryptography.hazmat.primitives import serialization
import pandas as pd
import numpy as np

OUT = Path('outputs/tqqq_intraday_20260923')

def signed_get(path, params):
    key = serialization.load_pem_private_key(Path(os.environ['FUTUNN_PRIVATE_KEY_FILE']).read_bytes(), password=None)
    query = urllib.parse.urlencode(params)
    ts = str(int(time.time()*1000))
    sig = base64.b64encode(key.sign('\n'.join([ts, 'GET', path, query, '']).encode())).decode()
    req = urllib.request.Request('https://webapi.futunn.com'+path+'?'+query, headers={'X-Api-Key':os.environ['FUTUNN_APP_KEY'], 'X-Timestamp':ts, 'X-Nonce':secrets.token_urlsafe(24), 'Authorization':sig})
    with urllib.request.urlopen(req, timeout=25) as r:
        result=json.load(r)
    if result.get('ret_code') != 0:
        raise RuntimeError(str(result))
    return result

def get(symbol, start, end, ktype=6):
    return signed_get('/api/v1.0/quote/'+symbol+'/history-kline',dict(start=start,end=end,ktype=ktype,num=370,autype=0))

def download():
    OUT.mkdir(parents=True,exist_ok=True)
    for symbol in ('TQQQ','QQQ'):
        rows=[]
        for offset in range(0,32,4):
            start=date(2026,8,22)+timedelta(days=offset)
            end=min(start+timedelta(days=4),date(2026,9,23))
            result=get('US.'+symbol,str(start),str(end))
            (OUT/f'{symbol}_{start}.json').write_text(json.dumps(result))
            batch=result['data']['kline_list'];rows.extend(batch)
            print(symbol,start,end,len(batch),flush=True)
            time.sleep(1.1)
        df=pd.DataFrame(rows).drop_duplicates('time_key').sort_values('time_key')
        df['timestamp']=pd.to_datetime(df.time_key,unit='ms',utc=True).dt.tz_convert('America/New_York')
        df.to_csv(OUT/f'{symbol}.csv',index=False)
        print(df.groupby('date').size().to_dict(),flush=True)

def load():
    data={}
    for s in ('TQQQ','QQQ'):
        f=pd.read_csv(OUT/f'{s}.csv')
        # REST bars observed at 09:35..16:00: end-labelled. Normalize to opens.
        f.index=pd.to_datetime(f.time_key,unit='ms',utc=True).dt.tz_convert('America/New_York')-pd.Timedelta(minutes=5)
        data[s]=f
    return data

def signals(data):
    f=data['TQQQ']; q=data['QQQ']; day=f.index.date
    e9=f.close.ewm(span=9,adjust=False).mean()
    e20=f.close.ewm(span=20,adjust=False).mean()
    e60=f.close.ewm(span=60,adjust=False).mean()
    q20=q.close.ewm(span=20,adjust=False).mean()
    q60=q.close.ewm(span=60,adjust=False).mean()
    trend=(f.close>e20)&(e20>e60)&(q.close>q20)&(q20>q60)
    vwap=((f.high+f.low+f.close)/3*f.volume).groupby(day).cumsum()/f.volume.groupby(day).cumsum()
    return {'breakout':trend&(f.close>f.high.shift().rolling(12).max()),
            'pullback':trend&(f.low<=e9)&(f.close>e9)&(f.close>f.close.shift()),
            'vwap_reclaim':trend&(f.close>vwap)&(f.close.shift()<=vwap.shift())},e20

def simulate(data, signal, e20, days, bps=10):
    f=data['TQQQ']; cash=10000.; curve=[cash]; trades=[]; daily=[]
    for day in days:
        ix=np.flatnonzero(f.index.strftime('%Y-%m-%d')==day)
        qty=0; entry=0; peak=0; pending=False; count=0; begin=cash
        for n,i in enumerate(ix):
            row=f.iloc[i]; clock=f.index[i].strftime('%H:%M')
            # At bar open act only on prior completed bar. Hard/trailing exits
            # are close-confirmed, NOT assumed continuously resting stops.
            if qty and (pending or clock>='15:50'):
                price=row.open*(1-bps/10000);cash+=qty*price
                trades.append(dict(day=day,entry_time=entry_time,exit_time=str(f.index[i]),qty=qty,entry=entry,exit=price,pnl=qty*(price-entry)))
                qty=0;pending=False
            elif not qty and n>0 and '10:00'<=clock<='14:30' and count<3 and cash>begin*.98 and signal.iloc[i-1]:
                price=row.open*(1+bps/10000)
                qty=int(cash*.80/price);cash-=qty*price
                entry=price;peak=f.close.iloc[i-1];entry_time=str(f.index[i]);count+=1
            if qty:
                peak=max(peak,row.close)
                pending=row.close<e20.iloc[i] or row.close<=entry*.985 or row.close<=peak*.98
            curve.append(cash+qty*row.close)
        assert qty==0,'Missing scheduled close liquidation'
        daily.append(dict(day=day,equity=cash,return_pct=(cash/begin-1)*100))
    a=np.array(curve)
    return dict(return_pct=(cash/10000-1)*100,max_drawdown_pct=float((1-a/np.maximum.accumulate(a)).max()*100),trades=len(trades),win_rate=sum(t['pnl']>0 for t in trades)/len(trades) if trades else None),trades,daily

def research():
    data=load(); f=data['TQQQ']
    import exchange_calendars as xc
    cal=xc.get_calendar('XNYS')
    sessions=cal.sessions_in_range('2026-08-24','2026-09-22')
    expected=pd.DatetimeIndex([t for d in sessions for t in pd.date_range(cal.session_open(d),cal.session_close(d),freq='5min',inclusive='left')])
    audit={}
    for s,df in data.items():
        actual=df.index.tz_convert('UTC')
        audit[s]=dict(rows=len(df),missing=len(expected.difference(actual)),extra=len(actual.difference(expected)),duplicates=int(actual.duplicated().sum()))
        assert list(actual)==list(expected),audit
        assert (df.high>=df[['open','close','low']].max(axis=1)).all()
        assert (df.low<=df[['open','close','high']].min(axis=1)).all()
    days=sorted(set(f.index.strftime('%Y-%m-%d')))
    # Freeze three families, first two sessions warmup, last six sessions holdout.
    train,test=days[2:-6],days[-6:]
    sig,e20=signals(data)
    board={name:simulate(data,s,e20,train)[0] for name,s in sig.items()}
    selected=max(board,key=lambda n:board[n]['return_pct'])
    (OUT/'selection.json').write_text(json.dumps(dict(train=train,test=test,board=board,selected=selected),indent=2))
    results={}
    for label,ds in [('train',train),('holdout',test),('all_after_warmup',days[2:])]:
        for cost in (10,20):
            stats,trades,daily=simulate(data,sig[selected],e20,ds,cost)
            results[f'{label}_{cost}bps']=stats
            pd.DataFrame(trades).to_csv(OUT/f'{label}_{cost}bps_trades.csv',index=False)
            pd.DataFrame(daily).to_csv(OUT/f'{label}_{cost}bps_daily.csv',index=False)
    report=dict(source='Futunn REST history-kline; unadjusted RTH 5m',audit=audit,train=train,holdout=test,board=board,selected=selected,results=results,assumptions='10000 USD; 80% cash allocation; integer shares; 10/20bps total friction EACH SIDE; next bar open execution; close-confirmed exits; no overnight; max 3 entries/day; immediate proceeds reuse',status='exploratory one-month sample, not client validated')
    (OUT/'report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))

if __name__=='__main__':
    import sys
    research() if '--research' in sys.argv else download()
