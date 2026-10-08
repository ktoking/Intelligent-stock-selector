#!/usr/bin/env python3
"""One-component-at-a-time entry ablation, with fixed exits and costs."""
from pathlib import Path
from datetime import datetime,timezone
import hashlib
import json
import numpy as np
import pandas as pd
from scripts.yahoo_5m_factor_lab import load_data,simulate
from scripts.yahoo_5m_regime_lab import regime_features

COMPONENTS=('volume','efficiency','range','turning','reference')
CASES=('full',)+tuple('without_'+x for x in COMPONENTS)+('neutral_rank',)

def ablation_features(data):
    result=regime_features(data)
    for s,reference in [('SOXL','SMH'),('TQQQ','QQQ')]:
        raw=data[s];f=result[s];local=raw.index.tz_convert('America/New_York');day=local.strftime('%Y-%m-%d');slot=local.strftime('%H:%M')
        low=raw.low.groupby(day).transform(lambda x:x.shift().rolling(12,min_periods=6).min())
        ref=data[reference].close
        masks=pd.DataFrame({'volume':f.rvol>=1.2,'efficiency':f.efficiency<.35,'range':(raw.low<=low)&(raw.close>low),'turning':(raw.close>raw.close.shift())&(raw.close>raw.open),'reference':ref/ref.groupby(day).transform('first')-1>-.015},index=f.index)
        morning=(slot>='09:50')&(slot<='11:25')
        for case in CASES:
            keep=[x for x in COMPONENTS if case!='without_'+x]
            f[case]=masks[keep].all(axis=1)&morning
        for c in COMPONENTS:f['component_'+c]=masks[c]
        assert (f.full==(f.range_bounce_volume&morning)).all()
    return result

def run(output):
    if output.exists():raise FileExistsError(output)
    output.mkdir(parents=True)
    source=Path('data/factor_lab/20260906_minute_pilot_v1/public_5m.pkl');prior=Path('data/factor_lab/20260906_yahoo_5m_timing_v1')
    parts=json.loads((prior/'manifest.json').read_text())['partitions'];days=sum(parts.values(),[])
    m={'created_at':datetime.now(timezone.utc).isoformat(),'cases':CASES,'parts':parts,'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'capital':10000,'fees_bps':[7,15],'minimum_fee_usd':2,'slippage_bps':2,'risk':.01,'daily_entries':1,'target':None,'scope':'One entry condition removed per case; ranking fixed except explicit neutral_rank. No tuning or winner selection. Repeatedly observed history, not new holdout.','code':{}}
    for path in (Path(__file__),Path('scripts/yahoo_5m_factor_lab.py'),Path('scripts/yahoo_5m_regime_lab.py'),Path('scripts/equity_factor_lab.py')):
        b=path.read_bytes();(output/path.name).write_bytes(b);m['code'][path.name]=hashlib.sha256(b).hexdigest()
    (output/'manifest.json').write_text(json.dumps(m,indent=2));data=load_data(source);fs=ablation_features(data);rows=[]
    for case in CASES:
        active={s:f.copy() for s,f in fs.items()}
        if case=='neutral_rank':
            for f in active.values():f['score']=0.
        for fee in m['fees_bps']:
            r=simulate(data,active,{'family':case,'threshold':0,'risk':.01},days,initial_equity=10000,cost_bps=fee,minimum_fee_usd=2,slippage_bps=2,max_daily_entries=1,reward_multiple=None)
            signals={s:int(f.loc[f.index.tz_convert('America/New_York').strftime('%Y-%m-%d').isin(days),case].sum()) for s,f in fs.items()}
            row={'case':case,'fee':fee,'metrics':r['metrics'],'signals':signals,'parts':{}}
            for part,ds in parts.items():row['parts'][part]=float((np.prod([1+d['return'] for d in r['daily'] if d['date'] in ds])-1)*100)
            np.testing.assert_allclose(sum(t['pnl'] for t in r['trades']),r['daily'][-1]['equity']-10000,rtol=0,atol=1e-7)
            for kind in ('daily','trades'):pd.DataFrame(r[kind]).to_csv(output/f'{case}_fee{fee}_{kind}.csv',index=False)
            rows.append(row);print(case,fee,round(r['metrics']['return_pct'],3),r['metrics']['trades'],signals,flush=True)
    old=json.loads((prior/'report.json').read_text())
    for fee in m['fees_bps']:
        a=next(r for r in rows if r['case']=='full' and r['fee']==fee)['metrics'];b=next(r for r in old['results'] if r['config']['name']=='morning_base' and r['fee']==fee)['metrics']
        for k in a:np.testing.assert_allclose(a[k],b[k],rtol=0,atol=1e-9)
    (output/'report.json').write_text(json.dumps({'manifest':m,'rows':rows,'full_baseline_parity':True,'promotion_passed':False,'execution_enabled':False},indent=2))

if __name__=='__main__':run(Path('data/factor_lab/20260906_yahoo_5m_morning_ablation_v1'))
