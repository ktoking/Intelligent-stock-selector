#!/usr/bin/env python3
"""Fixed ATR multiplier; compare inclusion of overnight gaps and daily warmup."""
from pathlib import Path
from datetime import datetime,timezone
import json
import hashlib
import numpy as np
import pandas as pd
from scripts.yahoo_5m_factor_lab import load_data,simulate
from scripts.yahoo_5m_morning_ablation import ablation_features

MODES=('original','exclude_overnight_gap','session_only_14bars')

def stop_features(data,fs,mode):
    if mode not in MODES:raise ValueError(mode)
    out={s:f.copy() for s,f in fs.items()}
    if mode=='original':return out
    for s,f in out.items():
        raw=data[s];day=raw.index.tz_convert('America/New_York').strftime('%Y-%m-%d')
        previous=raw.close.groupby(day).shift()
        tr=pd.concat([raw.high-raw.low,(raw.high-previous).abs(),(raw.low-previous).abs()],axis=1).max(axis=1)
        atr=tr.rolling(14).mean() if mode=='exclude_overnight_gap' else tr.groupby(day).transform(lambda x:x.rolling(14).mean())
        f['stop_fraction']=(1.5*atr/raw.close).clip(.003,.03)
    return out


def run(output):
    if output.exists():raise FileExistsError(output)
    output.mkdir(parents=True);source=Path('data/factor_lab/20260906_minute_pilot_v1/public_5m.pkl')
    prior=json.loads(Path('data/factor_lab/20260906_yahoo_5m_morning_ablation_v1/report.json').read_text());parts=prior['manifest']['parts'];days=sum(parts.values(),[])
    m={'created_at':datetime.now(timezone.utc).isoformat(),'rules':['full','without_efficiency'],'modes':MODES,'fees_bps':[7,15],'capital':10000,'risk':.01,'minimum_fee_usd':2,'slippage_bps':2,'daily_entries':1,'multiplier':1.5,'stop_clip':[.003,.03],'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'parts':parts,'scope':'Only stop distance and risk sizing input changed; entry factors including their original ATR-normalized VWAP rank remain frozen. Session-only requires14 current-session bars and can reject early signals. No tuning or selection; previously observed history.','code':{}}
    for path in (Path(__file__),Path('scripts/yahoo_5m_morning_ablation.py'),Path('scripts/yahoo_5m_factor_lab.py'),Path('scripts/yahoo_5m_regime_lab.py'),Path('scripts/equity_factor_lab.py')):
        b=path.read_bytes();(output/path.name).write_bytes(b);m['code'][path.name]=hashlib.sha256(b).hexdigest()
    (output/'manifest.json').write_text(json.dumps(m,indent=2));data=load_data(source);fs=ablation_features(data);rows=[];signal_stops=[]
    for mode in MODES:
        active=stop_features(data,fs,mode)
        for rule in m['rules']:
            for s,f in active.items():
                dates=f.index.tz_convert('America/New_York').strftime('%Y-%m-%d')
                for t in f.index[f[rule]&dates.isin(days)]:signal_stops.append({'mode':mode,'rule':rule,'symbol':s,'signal_bar':str(t),'stop_fraction':None if pd.isna(f.loc[t,'stop_fraction']) else float(f.loc[t,'stop_fraction'])})
            for fee in m['fees_bps']:
                r=simulate(data,active,{'family':rule,'threshold':0,'risk':.01},days,initial_equity=10000,cost_bps=fee,minimum_fee_usd=2,slippage_bps=2,max_daily_entries=1,reward_multiple=None)
                if mode=='original':
                    old=next(x['metrics'] for x in prior['rows'] if x['case']==rule and x['fee']==fee)
                    for key in old:np.testing.assert_allclose(old[key],r['metrics'][key],rtol=0,atol=1e-9)
                np.testing.assert_allclose(sum(t['pnl'] for t in r['trades']),r['daily'][-1]['equity']-10000,rtol=0,atol=1e-7)
                for kind in ('daily','trades'):pd.DataFrame(r[kind]).to_csv(output/f'{rule}_{mode}_fee{fee}_{kind}.csv',index=False)
                rows.append({'rule':rule,'mode':mode,'fee':fee,'metrics':r['metrics']});print(rule,mode,fee,round(r['metrics']['return_pct'],3),r['metrics']['trades'],flush=True)
    pd.DataFrame(signal_stops).to_csv(output/'signal_stops.csv',index=False)
    (output/'report.json').write_text(json.dumps({'manifest':m,'rows':rows,'original_parity':True,'promotion_passed':False,'execution_enabled':False},indent=2))

if __name__=='__main__':run(Path('data/factor_lab/20260906_yahoo_5m_atr_audit_v1'))
