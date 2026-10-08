#!/usr/bin/env python3
"""Fixed-signal daily entry limits, with a fixed cost schedule and $10,000 capital."""
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
from scripts.yahoo_5m_factor_lab import load_data,simulate
from scripts.yahoo_5m_regime_lab import regime_features


def run(parent,output):
    if output.exists():raise FileExistsError('new output required')
    prior=json.loads((parent/'report.json').read_text())
    allowed=prior['data_quality']['eligible_trade_symbols']
    pairs=[(s,ref) for s,ref in prior['manifest']['pairs'].items() if s in allowed]
    cfg=prior['manifest']['fixed_config']
    output.mkdir(parents=True)
    manifest={'created_at':datetime.now(timezone.utc).isoformat(),'initial_equity':10000,
        'max_daily_entries':[1,2,3],'cost_bps_each_side':[7,15],'slippage_bps_each_side':2,'minimum_fee_usd':2,
        'fixed_config':cfg,'partitions':prior['manifest']['partitions'],
        'upstream_sha256':hashlib.sha256((parent/'report.json').read_bytes()).hexdigest(),
        'data_sha256':hashlib.sha256((parent/'data.pkl').read_bytes()).hexdigest(),
        'selection':'No selection; report every predeclared entry-limit comparison.',
        'interpretation':'Previously observed short history; not new out-of-sample evidence. Fees are scenario assumptions.',
        'code':{}}
    if manifest['data_sha256']!=prior['data_manifest']['sha256']:raise ValueError('data changed')
    for file in (Path(__file__),ROOT/'scripts/yahoo_5m_factor_lab.py',ROOT/'scripts/yahoo_5m_regime_lab.py',ROOT/'scripts/equity_factor_lab.py'):
        payload=file.read_bytes();(output/file.name).write_bytes(payload);manifest['code'][file.name]=hashlib.sha256(payload).hexdigest()
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2))
    data=load_data(parent/'data.pkl',symbols=list(dict.fromkeys(x for pair in pairs for x in pair)))
    fs=regime_features(data,pairs=pairs);days=sum(manifest['partitions'].values(),[])
    rows=[]
    for group,symbols in {'baseline_pair':['SOXL','TQQQ'],'expanded_account':allowed}.items():
        for limit in (1,2,3):
            for fee in (7,15):
                result=simulate(data,fs,cfg['entry'],days,initial_equity=10000,cost_bps=fee,minimum_fee_usd=2,
                    max_daily_entries=limit,max_holding_bars=cfg['max_holding_bars'],reward_multiple=cfg['reward_multiple'],symbols=symbols)
                row={'group':group,'limit':limit,'fee':fee,'metrics':result['metrics'],'parts':{}}
                for part,part_days in manifest['partitions'].items():
                    records=[x for x in result['daily'] if x['date'] in part_days]
                    returns=np.array([x['return'] for x in records])
                    row['parts'][part]={'return_pct':float((np.prod(1+returns)-1)*100),'sessions':len(records)}
                rows.append(row)
                for kind in ('daily','trades'):
                    pd.DataFrame(result[kind]).to_csv(output/f'{group}_limit{limit}_fee{fee}_{kind}.csv',index=False)
                np.testing.assert_allclose(sum(t['pnl'] for t in result['trades']),result['daily'][-1]['equity']-10000,atol=1e-7,rtol=0)
                print(group,limit,fee,f"net{row['metrics']['return_pct']:.2f}% trades{row['metrics']['trades']}",flush=True)
    report={'manifest':manifest,'results':rows,'promotion_passed':False,'execution_enabled':False}
    (output/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--parent',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();run(a.parent,a.output)
