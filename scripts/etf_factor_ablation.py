#!/usr/bin/env python3
"""Fixed ETF factor ablation; all reused dates explicitly diagnostic."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.equity_factor_lab import daily_data, simulate


def targets(p, core, rule):
    c = p["close"]
    ref = "QQQ" if core == "TQQQ" else core
    momentum = c.pct_change(63, fill_method=None)
    trend = c[ref] > c[ref].rolling(200).mean()
    if "momentum" in rule:
        trend &= momentum[ref] > 0
    if rule == "always":
        trend[:] = True
    weights = pd.DataFrame(0., index=c.index, columns=c.columns)
    weights.loc[trend,core] = 1.
    if "defense" in rule:
        defense = momentum[["GLD","IEF"]].fillna(-np.inf).idxmax(axis=1)
        valid = momentum[["GLD","IEF"]].max(axis=1) > 0
        for s in ("GLD","IEF"):
            weights.loc[~trend & valid & (defense==s),s] = 1.
        weights.loc[~trend & ~valid,"SHY"] = 1.
    if "vol40" in rule:
        vol = c.pct_change(fill_method=None).rolling(63).std() * np.sqrt(252)
        weights *= (.4/vol).clip(upper=1).fillna(0)
    weights.iloc[:200] = 0.
    return weights


def run(source, output):
    if (output/"report.json").exists():
        raise FileExistsError("completed ablation is immutable")
    output.mkdir(parents=True,exist_ok=True)
    rules = ("always","ma200","ma200_momentum","ma200_momentum_defense","ma200_momentum_defense_vol40")
    manifest = {"created_at":datetime.now(timezone.utc).isoformat(),"cores":["QQQ","TQQQ"],"rules":rules,
                "rebalance_sessions":1,"costs_bps_per_side":[7,12.5,20],
                "type":"fixed-rule attribution, not optimized or new holdout",
                "primary_start":"2013-04-24","primary_end":"2026-06-05",
                "recent_start":"2026-06-08","recent_end":"2026-09-04",
                "source_code":{}}
    for f in (Path(__file__),ROOT/"scripts/equity_factor_lab.py"):
        payload=f.read_bytes();(output/f.name).write_bytes(payload)
        manifest["source_code"][f.name]=hashlib.sha256(payload).hexdigest()
    (output/"manifest.json").write_text(json.dumps(manifest,indent=2))
    p, data = daily_data(source)
    index = p["close"].index
    first,last = index.get_loc(manifest["primary_start"]),index.get_loc(manifest["recent_start"])
    results=[]
    for core in manifest["cores"]:
        for rule in rules:
            w = targets(p,core,rule)
            long = simulate(p,w,first,last,rebalance=1)
            recent = simulate(p,w,last,len(index),rebalance=1)
            name = f"{core}_{rule}"
            eras={}
            for start,end in (("2013-04-24","2020-01-01"),("2020-01-01","2024-01-01"),("2024-01-01","2026-06-08")):
                a,b=index.searchsorted(start),index.searchsorted(end)
                eras[f"{start}--{index[b-1]}"]=simulate(p,w,a,b,rebalance=1)["metrics"]
            record={"name":name,"primary":long["metrics"],"recent":recent["metrics"],"eras":eras,
                    "cost_stress":{str(cost):simulate(p,w,first,last,cost_bps=cost,rebalance=1)["metrics"] for cost in (12.5,20)}}
            results.append(record)
            pd.DataFrame(long["daily"]).to_csv(output/f"{name}_daily.csv",index=False)
            pd.DataFrame(long["orders"]).to_csv(output/f"{name}_orders.csv",index=False)
            print(name,f"CAGR {long['metrics']['annualized_pct']:.2f}% DD {long['metrics']['max_drawdown_close_pct']:.2f}% recent {recent['metrics']['return_pct']:.2f}%",flush=True)
    report={"manifest":manifest,"data":data,"results":results,"promotion_passed":False,"execution_enabled":False,
            "limitations":["All dates reused historical diagnostics; no selected winner or forward claim.",
                            "Real leveraged-fund adjusted prices; actual fund path rather than synthetic scaled profits.",
                            "Daily close signals trade next open; fractional units/proportional costs; no intraday drawdown or depth.",
                            "Corporate actions use one provider; no independent source reconciliation."]}
    (output/"report.json").write_text(json.dumps(report,indent=2,allow_nan=False))
    return report


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--source",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args();run(args.source,args.output)
