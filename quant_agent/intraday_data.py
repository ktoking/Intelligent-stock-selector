"""Futunn unadjusted five-minute bars, labelled by CLOSE time, never daily proxies."""
from datetime import date,timedelta
import json
import math
from pathlib import Path
import time

import pandas as pd

from .broker import FutuSim,ReadUnavailable
from .config import canonical,digest
from .exchange_clock import us_calendar
from .files import atomic_write

SOURCE='Futunn AppKey REST history-kline; ktype=6; autype=0; regular session; close timestamps'
FIELDS=('open','high','low','close','volume','turnover')


def normalize(rows,now=None):
    now=pd.Timestamp.now(tz='UTC') if now is None else pd.Timestamp(now)
    cal=us_calendar();result={}
    for raw in rows:
        end=pd.Timestamp(int(raw['time_key']),unit='ms',tz='UTC')
        day=end.tz_convert('America/New_York').date().isoformat()
        if not cal.is_session(day):continue
        opening,closing=cal.session_open(day),cal.session_close(day)
        if not opening<end<=closing:continue
        if (end-opening).total_seconds()%300:raise ValueError('Five-minute close timestamp is not on exchange grid')
        if end+pd.Timedelta(seconds=2)>now:continue
        if raw.get('date') and int(raw['date'])!=int(day.replace('-','')):raise ValueError('Minute trading date differs from timestamp')
        row={'end_ms':int(end.timestamp()*1000),'session':day,**{k:float(raw[k]) for k in FIELDS}}
        if (not all(math.isfinite(row[k]) for k in FIELDS) or min(row[k] for k in FIELDS[:4])<=0
            or min(row[k] for k in FIELDS[4:])<0 or row['high']<max(row[k] for k in FIELDS[:4])
            or row['low']>min(row[k] for k in FIELDS[:4])):raise ValueError('Invalid five-minute OHLCV')
        if row['end_ms'] in result and result[row['end_ms']]!=row:raise ValueError('Conflicting duplicate five-minute candle')
        result[row['end_ms']]=row
    return [result[k] for k in sorted(result)]


def fetch(broker,symbol,start,end,now=None):
    # Observed gateway end is exclusive at midnight for minute bars. Request
    # the following date, then enforce trading dates locally. Never infer empty
    # same-day response means that minute permissions are unavailable.
    last=date.fromisoformat(end)+timedelta(days=1)
    data=broker.request('GET','/api/v1.0/quote/'+symbol+'/history-kline',
        {'start':start,'end':str(last),'ktype':6,'autype':0,'num':370,'extended_time':0})['data']
    raw=data['kline_list']
    if len(raw)>=370 or data.get('next_time'):raise ValueError('Minute window may be truncated; split into smaller date windows')
    return [r for r in normalize(raw,now) if start<=r['session']<=end]


def complete_days(rows):
    cal=us_calendar();days={}
    for r in rows:days.setdefault(r['session'],[]).append(r)
    return {day:bars for day,bars in days.items() if [r['end_ms'] for r in bars]==
        [int(t.timestamp()*1000) for t in pd.date_range(cal.session_open(day)+pd.Timedelta(minutes=5),cal.session_close(day),freq='5min')]}


def collect(store,symbols,start,end,broker=None,delay=2.):
    broker=broker or FutuSim(store.settings);root=store.home/'intraday-data';root.mkdir(exist_ok=True,mode=0o700)
    saved=store.meta('intraday_download') or {}
    key=saved.get('data_sha256');previous=root/(str(key)+'.json')
    if key and previous.is_file():
        value=load(previous,key)
        if value['symbols']==list(symbols) and value['start']==start and value['end']==end and value['source']==SOURCE:
            return str(previous),key,value
    chunks=root/'chunks';chunks.mkdir(exist_ok=True,mode=0o700);data={};coverage={}
    for symbol in symbols:
        requested=date.fromisoformat(start)
        cursor=requested-timedelta(days=(requested-date(1970,1,1)).days%4)
        finish=date.fromisoformat(end);rows=[]
        while cursor<=finish:
            last=min(finish,cursor+timedelta(days=3));key=digest([SOURCE,symbol,str(cursor),str(last)])
            path=chunks/(key+'.json')
            if path.exists():batch=json.loads(path.read_text())['bars']
            else:
                for attempt in range(3):
                    try:batch=fetch(broker,symbol,str(cursor),str(last));break
                    except ReadUnavailable as exc:
                        if exc.ret_code!=-11 or attempt==2:raise
                        time.sleep(max(16,exc.retry_after or 0))
                atomic_write(path,canonical({'source':SOURCE,'symbol':symbol,'start':str(cursor),'end':str(last),'bars':batch}))
                time.sleep(delay)
            rows.extend(batch);cursor=last+timedelta(days=1)
            store.set_meta('intraday_download',{'status':'running','symbol':symbol,'through':str(last),'end':end,'cached_windows':True})
        # Re-normalize cached records too; overlapping windows must agree.
        unique={}
        for r in rows:
            if r['end_ms'] in unique and unique[r['end_ms']]!=r:raise ValueError('Minute cache overlap changed')
            unique[r['end_ms']]=r
        clean=[unique[k] for k in sorted(unique) if start<=unique[k]['session']<=end];days=complete_days(clean)
        data[symbol]=[r for bars in days.values() for r in bars]
        coverage[symbol]={'raw_bars':len(clean),'complete_sessions':len(days),'incomplete_sessions':sorted({r['session'] for r in clean}-set(days))}
    value={'source':SOURCE,'timeframe':'5m','timestamp':'bar_close','adjustment':'none','start':start,'end':end,
           'symbols':list(symbols),'coverage':coverage,'bars':data,'universe_bias':'Current observed symbols; not historical point-in-time index membership'}
    key=digest(value);path=root/(key+'.json')
    if not path.exists():atomic_write(path,canonical(value))
    store.set_meta('intraday_download',{'status':'completed','end':end,'coverage':coverage,'data_sha256':key})
    return str(path),key,value


def load(path,expected=None):
    value=json.loads(Path(path).read_text())
    if expected and digest(value)!=expected:raise ValueError('Frozen minute dataset changed')
    return value
