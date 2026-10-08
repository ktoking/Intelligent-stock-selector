#!/usr/bin/env python3
"""Same-provider repeatability and 5m/15m aggregation audit, not independent pricing."""
from pathlib import Path
from datetime import datetime,timezone
import hashlib
import json
import pandas as pd
import numpy as np
import yfinance as yf


def aggregate(frame):
    return frame.resample('15min',closed='left',label='left').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna(subset=['open'])


def compare(old,new):
    common=old.index.intersection(new.index)
    a=old.loc[common,['open','high','low','close','volume']];b=new.loc[common,a.columns]
    price=((a.iloc[:,:4]-b.iloc[:,:4]).abs()/a.iloc[:,:4]*10000)
    return {'old_bars':len(old),'new_bars':len(new),'common_bars':len(common),'missing_bars':len(old.index.difference(new.index)),'extra_bars':len(new.index.difference(old.index)), 'price_max_abs_difference_bps':float(price.max().max()) if len(common) else None,'price_bars_over_1bp':int((price.max(axis=1)>1).sum()),'volume_changed_bars':int((a.volume!=b.volume).sum()),'volume_max_abs_difference':float((a.volume-b.volume).abs().max()) if len(common) else None}


def run(output):
    if output.exists():raise FileExistsError(output)
    output.mkdir(parents=True)
    source=Path('data/factor_lab/20260906_minute_pilot_v1/public_5m.pkl')
    old=pd.read_pickle(source);jobs=[(s,'5m') for s in ('SOXL','TQQQ','SMH','QQQ')]+[(s,'15m') for s in ('SOXL','TQQQ')]
    m={'created_at':datetime.now(timezone.utc).isoformat(),'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'jobs':jobs,'start':'2026-07-10','end_exclusive':'2026-09-05','scope':'Yahoo repeat downloads and Yahoo cross-interval comparison; same vendor, not independent market truth','threshold':'Report every difference; flag price differences above1bp. No removal based on profitability.'}
    (output/'manifest.json').write_text(json.dumps(m,indent=2));(output/Path(__file__).name).write_bytes(Path(__file__).read_bytes())
    downloaded={};checks={};errors={}
    for s,interval in jobs:
        key=s+'_'+interval
        try:
            f=yf.download(s,start=m['start'],end=m['end_exclusive'],interval=interval,auto_adjust=False,progress=False,threads=False,timeout=20)
            if f.empty:raise ValueError('empty response')
            if isinstance(f.columns,pd.MultiIndex):f.columns=f.columns.get_level_values(0)
            f.columns=f.columns.str.lower();f.index=f.index.tz_convert('UTC');downloaded[key]=f
            baseline=old[s] if interval=='5m' else aggregate(old[s])
            checks[key]=compare(baseline,f);print(key,checks[key],flush=True)
        except Exception as e:errors[key]=str(e);print(key,type(e).__name__,flush=True)
    pd.to_pickle(downloaded,output/'downloaded.pkl')
    trades=pd.read_csv('data/factor_lab/20260906_yahoo_5m_timing_v1/morning_base_fee7_trades.csv');audit=[]
    for n,t in trades.iterrows():
        f=old[t.symbol];start=pd.Timestamp(t.entry_time);end=pd.Timestamp(t.exit_time)
        ix=f.index.get_loc(start);window=f.loc[start:end];prev=f.close.shift().loc[window.index]
        row={'symbol':t.symbol,'day':t.day,'pnl':float(t.pnl),'bars':len(window),'signal_bar':str(f.index[ix-1]),'entry_volume':float(f.loc[start,'volume']),'exit_volume':float(f.loc[end,'volume']),'max_open_gap_from_previous_close_pct':float(((window.open/prev-1).abs()*100).max()),'max_bar_range_pct':float(((window.high/window.low-1)*100).max()),'zero_volume_bars':int((window.volume<=0).sum()),'entry_qty_pct_of_bar_volume':float(t.qty/f.loc[start,'volume']*100),'exit_qty_pct_of_bar_volume':float(t.qty/f.loc[end,'volume']*100)}
        refreshed=downloaded.get(t.symbol+'_5m')
        if refreshed is not None:row['refresh_comparison']=compare(window,refreshed.loc[start:end])
        window.to_csv(output/f'trade{n}_{t.symbol}_{t.day}_bars.csv');audit.append(row)
    (output/'report.json').write_text(json.dumps({'manifest':m,'download_sha256':hashlib.sha256((output/'downloaded.pkl').read_bytes()).hexdigest(),'checks':checks,'errors':errors,'trades':audit,'promotion_passed':False,'execution_enabled':False},indent=2,allow_nan=False))

if __name__=='__main__':run(Path('data/factor_lab/20260906_yahoo_5m_price_audit_v1'))
