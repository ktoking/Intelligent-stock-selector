#!/usr/bin/env python3
"""Fixed two-rule collision sensitivity and profit concentration diagnostics."""
import json
import hashlib
from datetime import datetime,timezone
from pathlib import Path
import numpy as np
import pandas as pd
from scripts.yahoo_5m_factor_lab import load_data,simulate
from scripts.yahoo_5m_morning_ablation import ablation_features

POLICIES=('original','prefer_SOXL','prefer_TQQQ','skip_collision')

def apply_policy(fs,rule,policy):
    if policy not in POLICIES:raise ValueError(policy)
    out={s:f.copy() for s,f in fs.items()}
    both=out['SOXL'][rule]&out['TQQQ'][rule]
    for s,f in out.items():
        if policy.startswith('prefer_'):f['score']=float(s==policy[7:])
        if policy=='skip_collision':f[rule]=f[rule]&~both
    return out


def concentration(daily):
    x=np.array([d['return'] for d in daily]);indices=np.argsort(x)[::-1]
    return {str(n):float((np.prod(1+np.delete(x,indices[:n]))-1)*100) for n in (1,2,3)}


def run(output):
    if output.exists():raise FileExistsError(output)
    output.mkdir(parents=True)
    source=Path('data/factor_lab/20260906_minute_pilot_v1/public_5m.pkl');prior=Path('data/factor_lab/20260906_yahoo_5m_morning_ablation_v1/report.json')
    old=json.loads(prior.read_text());parts=old['manifest']['parts'];days=sum(parts.values(),[])
    m={'created_at':datetime.now(timezone.utc).isoformat(),'rules':['full','without_efficiency'],'policies':POLICIES,'fees_bps':[7,15],'initial_equity':10000,'minimum_fee_usd':2,'slippage_bps':2,'risk':.01,'daily_entries':1,'target':None,'parts':parts,'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'interpretation':'No selection, all predeclared cases. Same observed history. Removing best days is descriptive, not a rerun or deployable policy. Skip collision skips that signal time, allows later independent signal.','code':{}}
    for path in (Path(__file__),Path('scripts/yahoo_5m_morning_ablation.py'),Path('scripts/yahoo_5m_factor_lab.py'),Path('scripts/yahoo_5m_regime_lab.py'),Path('scripts/equity_factor_lab.py')):
        b=path.read_bytes();(output/path.name).write_bytes(b);m['code'][path.name]=hashlib.sha256(b).hexdigest()
    (output/'manifest.json').write_text(json.dumps(m,indent=2));data=load_data(source);fs=ablation_features(data);rows=[];collisions=[]
    for rule in m['rules']:
        mask=fs['SOXL'][rule]&fs['TQQQ'][rule]
        for ix in fs['SOXL'].index[mask]:
            if str(ix.tz_convert('America/New_York').date()) not in days:continue
            collisions.append({'rule':rule,'signal_bar':str(ix),'SOXL_score':float(fs['SOXL'].loc[ix,'score']),'TQQQ_score':float(fs['TQQQ'].loc[ix,'score'])})
        for policy in POLICIES:
            active=apply_policy(fs,rule,policy)
            for fee in m['fees_bps']:
                res=simulate(data,active,{'family':rule,'threshold':0,'risk':.01},days,initial_equity=10000,cost_bps=fee,slippage_bps=2,minimum_fee_usd=2,max_daily_entries=1,reward_multiple=None)
                if policy=='original':
                    expected=next(x['metrics'] for x in old['rows'] if x['case']==rule and x['fee']==fee)
                    for k in expected:np.testing.assert_allclose(res['metrics'][k],expected[k],atol=1e-9,rtol=0)
                row={'rule':rule,'policy':policy,'fee':fee,'metrics':res['metrics'],'zero_best_days':concentration(res['daily'])}
                np.testing.assert_allclose(sum(t['pnl'] for t in res['trades']),res['daily'][-1]['equity']-10000,atol=1e-7,rtol=0)
                for kind in ('daily','trades'):pd.DataFrame(res[kind]).to_csv(output/f'{rule}_{policy}_fee{fee}_{kind}.csv',index=False)
                rows.append(row);print(rule,policy,fee,round(res['metrics']['return_pct'],3),flush=True)
    (output/'report.json').write_text(json.dumps({'manifest':m,'rows':rows,'collision_signals':collisions,'baseline_parity':True,'promotion_passed':False,'execution_enabled':False},indent=2))

if __name__=='__main__':run(Path('data/factor_lab/20260906_yahoo_5m_collision_v1'))
