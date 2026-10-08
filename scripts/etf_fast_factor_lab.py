#!/usr/bin/env python3
"""Faster trend, channel and downside-risk factors with causal selection.

No user drawdown limit is presumed. Return-first and Calmar selectors are
reported side by side with fixed ensembles; all reused history is diagnostic.
"""
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
import argparse
import hashlib
import json
import sys

import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.equity_factor_lab import daily_data, metrics, bootstrap
from scripts.factor_portfolio_engine import simulate


@dataclass(frozen=True)
class Config:
    core: str
    factor: str
    sizing: str
    @property
    def name(self):
        return f"{self.core}_{self.factor}_{self.sizing}"


def channel(close, enter, exit_):
    upper=close.shift(1).rolling(enter).max()
    lower=close.shift(1).rolling(exit_).min()
    active=False
    result=[]
    for i,value in enumerate(close):
        if value > upper.iloc[i]:active=True
        elif value < lower.iloc[i]:active=False
        result.append(float(active))
    return pd.Series(result,index=close.index)


def signals(close):
    returns=close.pct_change(fill_method=None)
    down=np.sqrt(returns.clip(upper=0).pow(2).rolling(20).mean()*252)
    result={
        "ma50":(close>close.rolling(50).mean()).astype(float),
        "ma100":(close>close.rolling(100).mean()).astype(float),
        "ema10_50":(close.ewm(span=10,adjust=False).mean()>close.ewm(span=50,adjust=False).mean()).astype(float),
        "ema20_100":(close.ewm(span=20,adjust=False).mean()>close.ewm(span=100,adjust=False).mean()).astype(float),
        "channel20_10":channel(close,20,10),
        "channel55_20":channel(close,55,20),
        "momentum20":(close.pct_change(20)>0).astype(float),
        "momentum63":(close.pct_change(63)>0).astype(float),
        "downside_gate":((close.pct_change(63)>0)&(down<down.shift(1).rolling(252,min_periods=200).quantile(.8))).astype(float),
    }
    result["ensemble"] = sum(result[x] for x in ("ma50","ma100","ema20_100","channel55_20","momentum63"))/5
    return result


def all_targets(p):
    result,configs={},[]
    c=p["close"]
    for core,ref in (("TQQQ","QQQ"),("UPRO","SPY")):
        vol=c[core].pct_change(fill_method=None).rolling(20).std()*np.sqrt(252)
        for name,signal in signals(c[ref]).items():
            for sizing in ("full","vol40","vol60"):
                cfg=Config(core,name,sizing)
                w=pd.DataFrame(0.,index=c.index,columns=c.columns)
                scale=1. if sizing=="full" else (int(sizing[3:])/100/vol).clip(upper=1).fillna(0)
                w[core]=signal*scale
                w.iloc[:253]=0.
                result[cfg.name]=w
                configs.append(cfg)
    return configs,result


def choose(board, policy):
    def value(r):
        if policy=="return_first":
            return min(r[x]["annualized_pct"] for x in ("train","validation"))
        return min(r[x]["annualized_pct"]/max(5.,r[x]["max_drawdown_close_pct"]) for x in ("train","validation"))
    return max(board,key=lambda r:(value(r),r["name"]))


def run(source,output):
    if (output/"report.json").exists():raise FileExistsError("completed study is immutable")
    output.mkdir(parents=True,exist_ok=True)
    manifest={"created_at":datetime.now(timezone.utc).isoformat(),"families":["ma50","ma100","ema10_50","ema20_100","channel20_10","channel55_20","momentum20","momentum63","downside_gate","ensemble"],
              "cores":["TQQQ","UPRO"],"sizing":["full","vol40","vol60"],"train":252,"validation":63,"test":63,
              "selectors":["return_first","calmar"],"drawdown_hard_limit":None,
              "return_selector":"maximize lower train/validation CAGR; no performance-triggered full-quarter cash lockout",
              "target_annual_pct":[40,110],"target_daily_pct":2,"cost_bps_each_side":[7,12.5,20],
              "signal_execution":"reference index closed signal -> next open actual leveraged ETF; daily rebalance",
              "history_reuse":"all comparisons diagnostic; final 2026-06-08 through 2026-09-04 already inspected",
              "code":{}}
    for file in (Path(__file__),ROOT/"scripts/factor_portfolio_engine.py",ROOT/"scripts/equity_factor_lab.py"):
        payload=file.read_bytes();(output/file.name).write_bytes(payload);manifest["code"][file.name]=hashlib.sha256(payload).hexdigest()
    (output/"manifest.json").write_text(json.dumps(manifest,indent=2))
    p,data=daily_data(source)
    configs,target=all_targets(p)
    dates=p["close"].index
    first,last=dates.get_loc("2013-04-24"),dates.get_loc("2026-06-08")
    policy_daily={key:[] for key in manifest["selectors"]}
    policy_orders={key:[] for key in policy_daily}
    cost_daily={key:{c:[] for c in (12.5,20)} for key in policy_daily}
    folds=[]
    def select_at(end):
        board=[{"name":cfg.name,
                "train":simulate(p,target[cfg.name],end-315,end-63,record_orders=False)["metrics"],
                "validation":simulate(p,target[cfg.name],end-63,end,record_orders=False)["metrics"]} for cfg in configs]
        return {key:choose(board,key) for key in policy_daily},board
    def append(series,w,start,end,cost=7.,orders=False):
        r=simulate(p,w,start,end,cost_bps=cost,record_orders=orders,initial_equity=series[-1]["equity"] if series else 100_000.)
        series.extend(r["daily"])
        return r
    for start in range(first,last,63):
        end=min(start+63,last)
        picks,board=select_at(start)
        record={"start":str(dates[start]),"end":str(dates[end-1]),"choices":picks,"leaderboard":board,"test":{}}
        for policy,pick in picks.items():
            w=target[pick["name"]]
            r=append(policy_daily[policy],w,start,end,orders=True)
            policy_orders[policy].extend({**order,"fold":len(folds)} for order in r["orders"])
            record["test"][policy]=r["metrics"]
            for cost in cost_daily[policy]:append(cost_daily[policy][cost],w,start,end,cost=cost)
        folds.append(record)
        print(f"fold {len(folds)} {record['start']} return-first={picks['return_first']['name']} net={record['test']['return_first']['return_pct']:.2f}%",flush=True)
    picks,board=select_at(last)
    (output/"final_selection_before_evaluation.json").write_text(json.dumps({"picks":picks,"leaderboard":board},indent=2))
    final={}
    for policy,pick in picks.items():
        r=simulate(p,target[pick["name"]],last,len(dates))
        final[policy]={"selected":pick,"metrics":r["metrics"],"bootstrap":bootstrap(r["daily"])}
        pd.DataFrame(r["daily"]).to_csv(output/f"{policy}_final_daily.csv",index=False)
        pd.DataFrame(r["orders"]).to_csv(output/f"{policy}_final_orders.csv",index=False)
    # All fixed rules are evaluated only after selector picks have been saved.
    fixed=[]
    for cfg in configs:
        r=simulate(p,target[cfg.name],first,last,record_orders=False)
        fixed.append({"config":asdict(cfg),"name":cfg.name,"metrics":r["metrics"]})
        pd.DataFrame(r["daily"]).to_csv(output/f"fixed_{cfg.name}_daily.csv",index=False)
    frontier=[r for r in fixed if not any(other["metrics"]["annualized_pct"]>=r["metrics"]["annualized_pct"] and
              other["metrics"]["max_drawdown_close_pct"]<=r["metrics"]["max_drawdown_close_pct"] and
              (other["metrics"]["annualized_pct"]>r["metrics"]["annualized_pct"] or other["metrics"]["max_drawdown_close_pct"]<r["metrics"]["max_drawdown_close_pct"])
              for other in fixed)]
    report={"manifest":manifest,"data":data,"folds":folds,"walkforward":{
               k:{"metrics":metrics(v),"bootstrap":bootstrap(v),"cost_stress":{str(c):metrics(x) for c,x in cost_daily[k].items()}} for k,v in policy_daily.items()},
            "final_diagnostic":final,"fixed_rule_diagnostic":fixed,"diagnostic_pareto_frontier":frontier,
            "promotion_passed":False,"execution_enabled":False,
            "limitations":["Current research adaptively follows inspected studies. All reused dates including final interval are diagnostic.",
              "Pareto frontier selected on full historical diagnostic; not an out-of-sample winner list.",
              "No user drawdown ceiling assumed; risk and return selectors are separate experiments.",
              "Actual ETF daily-reset paths, not P&L multiplied by leverage. Close-only drawdown; fractional shares and proportional cost proxy.",
              "Twenty-day volatility estimate can react after price gaps; a volatility target is not a loss guarantee."]}
    (output/"report.json").write_text(json.dumps(report,indent=2,allow_nan=False))
    for policy,rows in policy_daily.items():
        pd.DataFrame(rows).to_csv(output/f"{policy}_walk_daily.csv",index=False)
        pd.DataFrame(policy_orders[policy]).to_csv(output/f"{policy}_walk_orders.csv",index=False)
    print(json.dumps({"output":str(output),"walk":{k:v['metrics'] for k,v in report['walkforward'].items()},"frontier":frontier},indent=2))
    return report


if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--source",type=Path,required=True);parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args();run(args.source,args.output)
