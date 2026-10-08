#!/usr/bin/env python3
"""Fixed range-bounce timing/strength ablation; all results remain exploratory."""
import hashlib
import json
from datetime import datetime,timezone
from pathlib import Path
import numpy as np
import pandas as pd
from scripts.yahoo_5m_factor_lab import load_data,simulate
from scripts.yahoo_5m_regime_lab import regime_features

WINDOWS={'all':('09:50','14:55'),'morning':('09:50','11:25'),'midday':('11:30','12:55'),'afternoon':('13:00','14:55')}

def timing_features(data):
    fs=regime_features(data)
    for s,f in fs.items():
        slot=f.index.tz_convert('America/New_York').strftime('%H:%M')
        for name,(start,end) in WINDOWS.items():
            for strength in ('base','below_vwap','deep_vwap'):
                extra=pd.Series(True,index=f.index) if strength=='base' else f.distance_atr<(-1 if strength=='deep_vwap' else 0)
                f[f'{name}_{strength}']=f.range_bounce_volume&(slot>=start)&(slot<=end)&extra
    return fs

def bootstrap(returns):
    # Circular five-session blocks, preserving local dependence. Exploratory,
    # unadjusted for the many strategies already examined on this history.
    x=np.asarray(returns);rng=np.random.default_rng(20260906)
    starts=rng.integers(0,len(x),size=(5000,int(np.ceil(len(x)/5))))
    ix=((starts[:,:,None]+np.arange(5))%len(x)).reshape(5000,-1)[:,:len(x)]
    net=(np.prod(1+x[ix],axis=1)-1)*100
    return {'return_pct_95_interval':np.quantile(net,[.025,.975]).tolist(),'positive_fraction':float((net>0).mean()),'method':'5000 circular 5-session block resamples; no multiple-testing correction; not a forecast'}

def run(output):
    if output.exists():raise FileExistsError(output)
    output.mkdir(parents=True)
    source=Path('data/factor_lab/20260906_minute_pilot_v1/public_5m.pkl')
    parts=json.loads(Path('data/factor_lab/20260906_yahoo_5m_multifactor_v1/manifest.json').read_text())['partitions']
    cfgs=[{'name':f'{w}_{s}','family':f'{w}_{s}','threshold':0,'risk':.01} for w in WINDOWS for s in ('base','below_vwap','deep_vwap')]
    m={'created_at':datetime.now(timezone.utc).isoformat(),'configs':cfgs,'windows':WINDOWS,'partitions':parts,'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'initial_equity':10000,'max_daily_entries':1,'fees_bps':[7,15],'minimum_fee_usd':2,'slippage_bps':2,'reward_multiple':None,'selection':'None. Report every configuration, including negative and zero-trade outcomes.','history':'Repeatedly observed short pilot; no new temporal holdout.','code':{}}
    for path in (Path(__file__),Path('scripts/yahoo_5m_regime_lab.py'),Path('scripts/yahoo_5m_factor_lab.py'),Path('scripts/equity_factor_lab.py')):
        payload=path.read_bytes();(output/path.name).write_bytes(payload);m['code'][path.name]=hashlib.sha256(payload).hexdigest()
    (output/'manifest.json').write_text(json.dumps(m,indent=2))
    data=load_data(source);fs=timing_features(data);rows=[]
    for cfg in cfgs:
        for fee in m['fees_bps']:
            res=simulate(data,fs,cfg,sum(parts.values(),[]),initial_equity=10000,cost_bps=fee,minimum_fee_usd=2,slippage_bps=2,max_daily_entries=1,reward_multiple=None)
            row={'config':cfg,'fee':fee,'metrics':res['metrics'],'parts':{},'bootstrap':bootstrap([x['return'] for x in res['daily']])}
            for part,days in parts.items():
                daily=[x for x in res['daily'] if x['date'] in days]
                row['parts'][part]=float((np.prod([1+x['return'] for x in daily])-1)*100)
            np.testing.assert_allclose(sum(t['pnl'] for t in res['trades']),res['daily'][-1]['equity']-10000,atol=1e-7,rtol=0)
            for kind in ('daily','trades'):pd.DataFrame(res[kind]).to_csv(output/f'{cfg["name"]}_fee{fee}_{kind}.csv',index=False)
            rows.append(row);print(cfg['name'],fee,round(res['metrics']['return_pct'],3),flush=True)
    report={'manifest':m,'results':rows,'promotion_passed':False,'execution_enabled':False}
    (output/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False))

if __name__=='__main__':run(Path('data/factor_lab/20260906_yahoo_5m_timing_v1'))
