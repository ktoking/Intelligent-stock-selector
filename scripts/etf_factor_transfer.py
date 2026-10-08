#!/usr/bin/env python3
"""Apply previously locked factor settings to additional actual ETF paths."""
import argparse
from pathlib import Path
import sys
import json
import hashlib

import pandas as pd
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.equity_factor_lab import daily_data
from scripts.etf_fast_factor_lab import signals
from scripts.factor_portfolio_engine import simulate


def run(source,output):
    if (output/"report.json").exists():raise FileExistsError("completed transfer is immutable")
    manifest=json.loads((output/"manifest.json").read_text())
    if hashlib.sha256(Path(manifest["upstream_report"]).read_bytes()).hexdigest()!=manifest["upstream_sha256"]:
        raise ValueError("upstream selection artifact changed")
    hashes={}
    for file in (Path(__file__),ROOT/"scripts/etf_fast_factor_lab.py",ROOT/"scripts/equity_factor_lab.py",ROOT/"scripts/factor_portfolio_engine.py"):
        payload=file.read_bytes();(output/file.name).write_bytes(payload);hashes[file.name]=hashlib.sha256(payload).hexdigest()
    p,data=daily_data(source)
    c=p["close"];index=c.index
    first,last=index.get_loc(manifest["primary"][0]),index.get_loc(manifest["recent"][0])
    rows=[]
    for core,ref in manifest["pairs"].items():
        signal_set=signals(c[ref])
        vol=c[core].pct_change(fill_method=None).rolling(20).std()*np.sqrt(252)
        for cfg in [{'factor':'always','sizing':'full'},*manifest['fixed_rules']]:
            name=f"{core}_{cfg['factor']}_{cfg['sizing']}"
            w=pd.DataFrame(0.,index=index,columns=c.columns)
            signal=1. if cfg['factor']=='always' else signal_set[cfg['factor']]
            scale=1. if cfg['sizing']=='full' else (int(cfg['sizing'][3:])/100/vol).clip(upper=1).fillna(0)
            w[core]=signal*scale;w.iloc[:253]=0.
            primary=simulate(p,w,first,last)
            recent=simulate(p,w,last,len(index))
            eras={}
            for start,end in (("2013-04-24","2020-01-01"),("2020-01-01","2024-01-01"),("2024-01-01","2026-06-08")):
                a,b=index.searchsorted(start),index.searchsorted(end)
                eras[f"{start}--{index[b-1]}"]=simulate(p,w,a,b,record_orders=False)["metrics"]
            rows.append({"name":name,"config":cfg,"reference":ref,"primary":primary['metrics'],"recent":recent['metrics'],
                         "eras":eras,"cost_stress":{str(cost):simulate(p,w,first,last,cost_bps=cost,record_orders=False)['metrics'] for cost in (12.5,20)}})
            for label,result in (('primary',primary),('recent',recent)):
                pd.DataFrame(result['daily']).to_csv(output/f'{name}_{label}_daily.csv',index=False)
                pd.DataFrame(result['orders']).to_csv(output/f'{name}_{label}_orders.csv',index=False)
            print(name,f"annual {primary['metrics']['annualized_pct']:.2f}% DD {primary['metrics']['max_drawdown_close_pct']:.2f}% recent {recent['metrics']['return_pct']:.2f}%",flush=True)
    report={'manifest':manifest,'source_code':hashes,'data':data,'results':rows,'promotion_passed':False,'execution_enabled':False,
            'limitations':['Cross-instrument historical diagnostic; rule choice followed observed TQQQ results. No new temporal holdout.',
                           'SMH is a semiconductor proxy, not exact SOXL benchmark.',
                           'Real leveraged fund paths; fractional shares, proportional fees, closing drawdown. No independent price source or depth validation.']}
    (output/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False))
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();run(args.source,args.output)
