#!/usr/bin/env python3
"""User-authorized $10,000 research capital with explicitly hypothetical cost schedules."""
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
    if output.exists():raise FileExistsError('new output directory required')
    prior=json.loads((parent/'report.json').read_text());cfg=prior['manifest']['fixed_config']
    allowed=prior['data_quality']['eligible_trade_symbols']
    pairs=[(s,ref) for s,ref in prior['manifest']['pairs'].items() if s in allowed]
    output.mkdir(parents=True)
    scenarios=[(7,0),(7,2),(1,1),(1,2),(3,2),(15,2)]
    manifest={'created_at':datetime.now(timezone.utc).isoformat(),'initial_equity':10000.,
        'capital_source':'user explicitly specified 1w USD','scenarios':scenarios,'slippage_bps_each_side':2,
        'fee_formula':'max(order_notional*fee_bps/10000,minimum_fee_usd), charged on both entry and exit',
        'fee_status':'Hypothetical sensitivity cases; broker/account tariff has not been supplied or verified',
        'upstream_sha256':hashlib.sha256((parent/'report.json').read_bytes()).hexdigest(),'code':{}}
    for file in (Path(__file__),ROOT/'scripts/yahoo_5m_factor_lab.py',ROOT/'scripts/yahoo_5m_regime_lab.py',ROOT/'scripts/equity_factor_lab.py'):
        payload=file.read_bytes();(output/file.name).write_bytes(payload);manifest['code'][file.name]=hashlib.sha256(payload).hexdigest()
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2))
    source=parent/'data.pkl'
    if hashlib.sha256(source.read_bytes()).hexdigest()!=prior['data_manifest']['sha256']:raise ValueError('data changed')
    data=load_data(source,symbols=list(dict.fromkeys(x for pair in pairs for x in pair)))
    fs=regime_features(data,pairs=pairs);days=sum(prior['manifest']['partitions'].values(),[])
    rows=[]
    for group,symbols in {'baseline_pair':['SOXL','TQQQ'],'expanded_account':allowed}.items():
        for fee,minimum in scenarios:
            result=simulate(data,fs,cfg['entry'],days,initial_equity=10000.,cost_bps=fee,minimum_fee_usd=minimum,
                reward_multiple=cfg['reward_multiple'],max_holding_bars=cfg['max_holding_bars'],symbols=symbols)
            m=result['metrics'];equity=result['daily'][-1]['equity']
            row={'group':group,'fee_bps':fee,'minimum_fee_usd':minimum,'metrics':m,'final_equity':equity,'pnl_usd':equity-10000}
            rows.append(row)
            for kind in ('daily','trades'):
                pd.DataFrame(result[kind]).to_csv(output/f'{group}_fee{fee}_min{minimum}_{kind}.csv',index=False)
            if fee==7 and minimum==0:
                old=next(x for x in prior['results'] if x['name']==group)['full_fee7']
                np.testing.assert_allclose(m['return_pct'],old['return_pct'],rtol=0,atol=1e-8)
            np.testing.assert_allclose(sum(t['pnl'] for t in result['trades']),equity-10000,rtol=0,atol=1e-7)
            print(group,fee,minimum,f"pnl ${equity-10000:.2f} return {m['return_pct']:.2f}%",flush=True)
    report={'manifest':manifest,'results':rows,'percentage_cost_scale_parity_passed':True,
        'trade_pnl_to_equity_reconciled':True,'promotion_passed':False,'execution_enabled':False}
    (output/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--parent',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();run(a.parent,a.output)
