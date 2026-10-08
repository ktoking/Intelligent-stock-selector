#!/usr/bin/env python3
"""Predeclared directional signals using observed bullish and inverse ETF prices."""
import json
import hashlib
from pathlib import Path
import numpy as np
import pandas as pd
from scripts.yahoo_5m_factor_lab import features,load_data,simulate

PAIRS={'SOXL':('SMH',1),'SOXS':('SMH',-1),'TQQQ':('QQQ',1),'SQQQ':('QQQ',-1)}

def directional_features(data, pairs=PAIRS):
    out=features(data, pairs=[(s,r) for s,(r,d) in pairs.items()])
    for s,(r,direction) in pairs.items():
        f=data[r];local=f.index.tz_convert('America/New_York')
        day=local.strftime('%Y-%m-%d');slot=local.strftime('%H:%M')
        vwap=(((f.high+f.low+f.close)/3*f.volume).groupby(day).cumsum()/f.volume.groupby(day).cumsum().replace(0,np.nan))
        # Cumulative first-three-bar range, frozen after 09:40; never broadcast future values.
        opening_high=f.high.where(slot<='09:40').groupby(day).cummax().groupby(day).ffill()
        opening_low=f.low.where(slot<='09:40').groupby(day).cummin().groupby(day).ffill()
        normal=f.volume.groupby(slot).transform(lambda x:x.shift().rolling(20,min_periods=3).median())
        rvol=f.volume/normal.replace(0,np.nan)
        grouped=f.close.resample('15min',closed='left',label='right').agg(['last','count'])
        closes=grouped.loc[grouped['count']==3,'last']
        difference=closes.ewm(span=9,adjust=False).mean()-closes.ewm(span=21,adjust=False).mean()
        trend=difference.reindex(f.index+pd.Timedelta(minutes=5),method='ffill');trend.index=f.index
        distance=(f.close-vwap)*direction
        previous=distance.groupby(day).shift()
        opening=(f.close>opening_high) if direction==1 else (f.close<opening_low)
        agree=(direction*trend>0)&(distance>0)
        out[s]['score']=rvol.fillna(0)
        for threshold in (1.,1.5):
            gate=agree&(rvol>=threshold)&(slot>='09:45')
            out[s][f'opening_break_{threshold}']=gate&opening
            out[s][f'vwap_cross_{threshold}']=gate&(previous<=0)
    return out

def run(output):
    m=json.loads((output/'manifest.json').read_text())
    parts=json.loads(Path('data/factor_lab/20260906_yahoo_5m_multifactor_v1/manifest.json').read_text())['partitions']
    m['partitions']=parts
    m['rules']='Reference completed 15m EMA9/21 agrees with direction, directional VWAP distance positive, reference same-clock volume/prior20-session median with min3, opening range first3 bars or intraday VWAP crossing; next bar execution.'
    m['code']={}
    for path in (Path(__file__),Path('scripts/yahoo_5m_factor_lab.py'),Path('scripts/equity_factor_lab.py')):
        payload=path.read_bytes();(output/path.name).write_bytes(payload);m['code'][path.name]=hashlib.sha256(payload).hexdigest()
    (output/'manifest.json').write_text(json.dumps(m,indent=2))
    data=load_data(output/'data.pkl',symbols=list(dict.fromkeys(list(PAIRS)+['SMH','QQQ'])))
    fs=directional_features(data);rows=[];days=sum(parts.values(),[])
    for family in m['families']:
        for rv in m['rvol_thresholds']:
            for cap in m['daily_entries']:
                cfg={'name':f'{family}_{rv}_cap{cap}','family':f'{family}_{rv}','threshold':0,'risk':.01}
                for fee in (7,15):
                    res=simulate(data,fs,cfg,days,initial_equity=10000,cost_bps=fee,minimum_fee_usd=2,slippage_bps=2,max_daily_entries=cap,reward_multiple=None,symbols=list(PAIRS))
                    row={'config':cfg,'fee':fee,'metrics':res['metrics'],'parts':{}}
                    for part,pdays in parts.items():
                        returns=[x['return'] for x in res['daily'] if x['date'] in pdays]
                        row['parts'][part]=float((np.prod(1+np.array(returns))-1)*100)
                    np.testing.assert_allclose(sum(t['pnl'] for t in res['trades']),res['daily'][-1]['equity']-10000,atol=1e-7,rtol=0)
                    for kind in ('daily','trades'):
                        pd.DataFrame(res[kind]).to_csv(output/f'{cfg["name"]}_fee{fee}_{kind}.csv',index=False)
                    rows.append(row);print(cfg['name'],fee,round(row['metrics']['return_pct'],3),flush=True)
    (output/'report.json').write_text(json.dumps({'manifest':m,'results':rows,'promotion_passed':False,'execution_enabled':False},indent=2,allow_nan=False))

if __name__=='__main__':run(Path('data/factor_lab/20260906_yahoo_5m_direction_v1'))
