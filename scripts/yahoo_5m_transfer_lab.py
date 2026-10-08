#!/usr/bin/env python3
"""Apply a frozen entry/exit rule to predeclared symbols without fitting parameters."""
import argparse
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


def run(folder):
    if (folder/'report.json').exists():raise FileExistsError('completed result is immutable')
    manifest=json.loads((folder/'manifest.json').read_text())
    upstream=Path(manifest['upstream'])
    if hashlib.sha256(upstream.read_bytes()).hexdigest()!=manifest['upstream_sha256']:
        raise ValueError('upstream changed')
    code={}
    for file in (Path(__file__),ROOT/'scripts/yahoo_5m_factor_lab.py',ROOT/'scripts/yahoo_5m_regime_lab.py',ROOT/'scripts/equity_factor_lab.py'):
        payload=file.read_bytes();(folder/file.name).write_bytes(payload);code[file.name]=hashlib.sha256(payload).hexdigest()
    source=folder/'data.pkl';source_manifest=json.loads((folder/'data_manifest.json').read_text())
    if hashlib.sha256(source.read_bytes()).hexdigest()!=source_manifest['sha256']:raise ValueError('data changed')
    raw=pd.read_pickle(source)
    expected=raw['SOXL'].index.tz_convert('UTC')
    invalid={}
    for name,frame in raw.items():
        index=frame.index.tz_convert('UTC')
        missing=expected.difference(index);extra=index.difference(expected)
        if len(missing) or len(extra) or index.has_duplicates:
            invalid[name]={'missing':[str(t) for t in missing],'extra':[str(t) for t in extra],'duplicates':bool(index.has_duplicates)}
    pairs=[(s,ref) for s,ref in manifest['pairs'].items() if s not in invalid and ref not in invalid]
    quality={'invalid_symbols':invalid,'eligible_trade_symbols':[s for s,_ in pairs],
             'rule':'Exclude instruments with incomplete bars, before observing returns. Never fill missing prices.'}
    (folder/'data_quality_before_returns.json').write_text(json.dumps(quality,indent=2))
    names=list(dict.fromkeys([s for pair in pairs for s in pair]))
    data=load_data(source,symbols=names);fs=regime_features(data,pairs=pairs)
    cfg=manifest['fixed_config'];days=sum(manifest['partitions'].values(),[])
    groups={s:[s] for s,_ in pairs}
    groups['baseline_pair']=['SOXL','TQQQ'];groups['expanded_account']=[s for s,_ in pairs]
    rows=[]
    for label,symbols in groups.items():
        row={'name':label,'symbols':symbols,'parts':{}}
        for fee in (7,15):
            result=simulate(data,fs,cfg['entry'],days,cost_bps=fee,
                max_holding_bars=cfg['max_holding_bars'],reward_multiple=cfg['reward_multiple'],symbols=symbols)
            row[f'full_fee{fee}']=result['metrics']
            for kind in ('daily','trades'):
                pd.DataFrame(result[kind]).to_csv(folder/f'{label}_full_fee{fee}_{kind}.csv',index=False)
        for part,part_days in manifest['partitions'].items():
            row['parts'][part]=simulate(data,fs,cfg['entry'],part_days,
                max_holding_bars=cfg['max_holding_bars'],reward_multiple=cfg['reward_multiple'],symbols=symbols)['metrics']
        rows.append(row)
        print(label,f"full {row['full_fee7']['return_pct']:.2f}% trades {row['full_fee7']['trades']} cost-stress {row['full_fee15']['return_pct']:.2f}%",flush=True)
    baseline=next(x for x in rows if x['name']=='baseline_pair')
    prior_full=json.loads((upstream.parent/'continuous_diagnostic.json').read_text())['metrics']
    for fee in (7,15):
        for metric in ('return_pct','max_drawdown_5m_close_pct','trades'):
            np.testing.assert_allclose(baseline[f'full_fee{fee}'][metric],prior_full[f'fee{fee}'][metric],rtol=0,atol=1e-9)
    report={'manifest':manifest,'data_manifest':source_manifest,'data_quality':quality,'code_sha256':code,'results':rows,
        'baseline_parity_passed':True,'promotion_passed':False,'execution_enabled':False,
        'limitations':['Single-name accounts are independent hypothetical accounts; their returns cannot be added.',
                       'Expanded account has one position and shared funds; no post-result winner-only selection.',
                       'Only36 evaluation sessions; historical cross-instrument diagnostic, not validated yearly performance.',
                       'Same fixed slippage/fractional-fill/buying-power assumptions as upstream.']}
    (folder/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False))
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--folder',type=Path,required=True)
    run(p.parse_args().folder)
