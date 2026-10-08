#!/usr/bin/env python3
"""Exit-only experiment with frozen entry definitions and a shared cash account."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.yahoo_5m_factor_lab import load_data,features,simulate
from scripts.yahoo_5m_regime_lab import regime_features


def run(source,output):
    if output.exists():raise FileExistsError('new output required')
    prior_paths=[ROOT/'data/factor_lab/20260906_yahoo_5m_multifactor_v1/report.json',
                 ROOT/'data/factor_lab/20260906_yahoo_5m_regime_v1/report.json']
    priors=[json.loads(p.read_text()) for p in prior_paths]
    configs=[]
    for prior in priors:
        entry=prior['selected']['config']
        for horizon in (3,6,12,None):
            for reward in (2.,None):
                configs.append({'name':f"{entry['name']}_hold{horizon}_reward{reward}",
                    'entry':entry,'max_holding_bars':horizon,'reward_multiple':reward})
    output.mkdir(parents=True)
    manifest={'created_at':datetime.now(timezone.utc).isoformat(),'source':str(source),
        'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'configs':configs,
        'partitions':priors[0]['manifest']['partitions'],
        'upstreams':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in prior_paths},
        'selection':'min10 train and3 validation trades; maximize worse train/validation daily Sharpe',
        'interpretation':'Previously observed history only. Stop always active; horizon exit at known future bar open. No concurrent positions.',
        'code':{}}
    for file in (Path(__file__),ROOT/'scripts/yahoo_5m_factor_lab.py',ROOT/'scripts/yahoo_5m_regime_lab.py',ROOT/'scripts/equity_factor_lab.py'):
        payload=file.read_bytes();(output/file.name).write_bytes(payload);manifest['code'][file.name]=hashlib.sha256(payload).hexdigest()
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2))
    data=load_data(source)
    fs={priors[0]['selected']['config']['name']:features(data),priors[1]['selected']['config']['name']:regime_features(data)}
    baseline_checks=[]
    for prior in priors:
        entry=prior['selected']['config']
        for part in ('train','validation'):
            result=simulate(data,fs[entry['name']],entry,manifest['partitions'][part])
            old=prior['selected'][part]
            np.testing.assert_allclose(result['metrics']['return_pct'],old['return_pct'],rtol=0,atol=1e-9)
            baseline_checks.append({'entry':entry['name'],'part':part,'return_pct_reproduced':True})
    def evaluate(config,part,fee=7):
        return simulate(data,fs[config['entry']['name']],config['entry'],manifest['partitions'][part],
            cost_bps=fee,max_holding_bars=config['max_holding_bars'],reward_multiple=config['reward_multiple'])
    rows=[]
    for cfg in configs:
        row={'config':cfg}
        for part in ('train','validation'):
            result=evaluate(cfg,part);row[part]=result['metrics']
            for kind in ('daily','trades'):
                pd.DataFrame(result[kind]).to_csv(output/f"{cfg['name']}_{part}_{kind}.csv",index=False)
        rows.append(row)
        print(cfg['name'],f"train={row['train']['return_pct']:.2f}% val={row['validation']['return_pct']:.2f}%",flush=True)
    eligible=[r for r in rows if r['train']['trades']>=10 and r['validation']['trades']>=3]
    selected=max(eligible,key=lambda r:(min(r[p]['sharpe_zero_rf'] for p in ('train','validation')),r['config']['name'])) if eligible else None
    (output/'selection_before_tail.json').write_text(json.dumps({'selected':selected,'rows':rows},indent=2))
    tail={}
    if selected:
        for fee in (7,15):
            result=evaluate(selected['config'],'final',fee);tail[f'fee{fee}']=result['metrics']
            for kind in ('daily','trades','bars'):
                pd.DataFrame(result[kind]).to_csv(output/f'selected_tail_fee{fee}_{kind}.csv',index=False)
    report={'manifest':manifest,'baseline_checks':baseline_checks,'rows':rows,'selected':selected,'tail':tail,
            'promotion_passed':False,'execution_enabled':False}
    (output/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False))
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();run(a.source,a.output)
