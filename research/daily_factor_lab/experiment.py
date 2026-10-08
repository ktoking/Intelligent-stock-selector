"""Fixed hypotheses, same-cost QQQ comparison, retrospective walk-forward."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import time

import numpy as np
import pandas as pd

from .core import Policy, benchmark, catalog, load_panel, simulate


WINDOWS = {
    "full": ("2024-05-01", "2026-09-22"),
    "early": ("2024-05-01", "2025-03-31"),
    "middle": ("2025-04-01", "2025-12-31"),
    "recent": ("2026-01-01", "2026-09-22"),
    "recent_half": ("2026-03-23", "2026-09-22"),
}
FOLDS = [
    ("2025-03-31", "2025-04-01", "2025-09-30"),
    ("2025-09-30", "2025-10-01", "2026-03-31"),
    ("2026-03-31", "2026-04-01", "2026-09-22"),
]


def serializable(value):
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    if isinstance(value, np.bool_):
        return bool(value)
    raise TypeError(type(value).__name__)


def write_json(path, value):
    with Path(path).open("x") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, default=serializable, allow_nan=False)


def comparison(result, reference):
    ans = dict(result)
    ans["excess_return_points"] = result["return_pct"]-reference["return_pct"]
    ans["drawdown_reduction_points"] = reference["max_drawdown_pct"]-result["max_drawdown_pct"]
    ans["beats_qqq_return_and_drawdown"] = bool(ans["excess_return_points"] > 0
                                                  and ans["drawdown_reduction_points"] > 0)
    return ans


def rolling_comparison(curve, reference, horizon=42):
    a, b = curve.equity.to_numpy(), reference.equity.to_numpy()
    if len(a) <= horizon:
        return {}
    ar, br = a[horizon:]/a[:-horizon]-1, b[horizon:]/b[:-horizon]-1
    return {"horizon_sessions":horizon,"observations":len(ar),
            "strategy_min_pct":float(ar.min()*100),"median_pct":float(np.median(ar)*100),
            "beat_qqq_fraction":float((ar>br).mean()),
            "worst_excess_points":float((ar-br).min()*100),
            "overlapping_not_independent":True}


def attribution(panel, result):
    """Contribution = cumulative sell cash - buy cash + terminal stock value."""
    pnl = {}
    for order in result["orders"]:
        signed = -1 if order["side"] == "BUY" else 1
        pnl[order["symbol"]] = pnl.get(order["symbol"],0.) + signed*order["qty"]*order["price"]
    for position in result["positions"]:
        pnl[position["symbol"]] = pnl.get(position["symbol"],0.) + position["qty"]*position["last_close"]
    expected = float(result["curve"].equity.iloc[-1])-10000.
    if abs(sum(pnl.values())-expected)>1e-6:
        raise AssertionError("Attribution does not reconcile")
    ranked = sorted(pnl.items(), key=lambda x:-x[1])
    return {"total_pnl":expected,"symbols":[{"symbol":s,"pnl":v} for s,v in ranked],
            "top5_pnl":sum(v for _,v in ranked[:5])}


def factor_diagnostics(panel):
    """Cross-sectional Spearman IC is descriptive, not a significance claim."""
    fields, dates = panel.fields, panel.dates
    report = {}
    for name in ("momentum_risk", "momentum_blend", "residual_momentum", "path_quality", "near_high"):
        report[name] = {}
        for label, (start,end) in {"early":WINDOWS["early"],"recent":WINDOWS["recent"]}.items():
            report[name][label] = {}
            for horizon in (5,20):
                values = []
                for i,date in enumerate(dates):
                    if not pd.Timestamp(start)<=date<=pd.Timestamp(end) or i+horizon>=len(dates):
                        continue
                    if dates[i+horizon]>pd.Timestamp(end):
                        continue  # Labels never cross the diagnostic partition end.
                    valid = panel.stock_mask & (fields["liquid20"][i]>=5e6)
                    score=fields[name][i]
                    future=fields["close"][i+horizon]/fields["open"][i+1]-1
                    valid &= np.isfinite(score)&np.isfinite(future)
                    if valid.sum()<30:
                        continue
                    left=pd.Series(score[valid]).rank().to_numpy()
                    right=pd.Series(future[valid]).rank().to_numpy()
                    corr=float(np.corrcoef(left,right)[0,1])
                    if np.isfinite(corr):
                        values.append(corr)
                report[name][label][str(horizon)]={"days":len(values),
                    "mean_rank_ic":float(np.mean(values)) if values else None,
                    "positive_day_fraction":float((np.array(values)>0).mean()) if values else None,
                    "overlapping_labels":True}
    return report


def plot_result(curves, output):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2,1,figsize=(12,7),sharex=True,
                             gridspec_kw={"height_ratios":[2,1]},layout="constrained")
    for name,frame in curves.items():
        date=pd.to_datetime(frame.date)
        equity=frame.equity.to_numpy()
        axes[0].plot(date,equity/10000,label=name,linewidth=1.5)
        peak=np.maximum.accumulate(np.r_[10000.,equity])[1:]
        axes[1].plot(date,(equity/peak-1)*100,label=name,linewidth=1)
    axes[0].set_ylabel("Equity / starting capital")
    axes[1].set_ylabel("Drawdown (%)")
    axes[0].legend(fontsize=8)
    for ax in axes:
        ax.grid(alpha=.2)
    fig.suptitle("Retrospective daily factor research | 25 bps each side | price returns")
    fig.savefig(output,dpi=140)
    plt.close(fig)


def run(output, data_dir=None, risk_sensitivity=False):
    output=Path(output)
    output.mkdir(parents=True,exist_ok=False)
    started=time.perf_counter()
    panel=load_panel(**({"directory":data_dir} if data_dir else {}))
    policies=catalog()
    if risk_sensitivity:
        for target in (.32, .36, .40):
            name=f"trend_heavy{round(target*100)}"
            policies[name]=Policy(name,annual_vol_target=target,rank_buffer=5)
    protocol={"created_at":datetime.now(timezone.utc).isoformat(),"data":panel.metadata,
              "windows":WINDOWS,"folds":FOLDS,"cost_bps":[10,25,50],
              "policies":{name:asdict(p) for name,p in policies.items()},
              "research_stage": "v2 adds risk-budget sensitivity after viewing v1; all findings retrospective"
              if risk_sensitivity else "initial fixed hypotheses",
              "selection":"At each fold train from 2024-05-01 through prior fold-end; require excess return >0 and drawdown below QQQ; choose highest excess return minus max drawdown; cash if none.",
              "release_gate":"Pooled walk-forward beats same-cost QQQ return and drawdown at 25 and 50bps, with >=2 of 3 folds beating both at 25bps. All still retrospective.",
              "status":"predeclared for this run; retrospective hypothesis selection, not independent OOS"}
    source_hash=sha256()
    for file in sorted(Path(__file__).parent.glob("*.py")):
        source_hash.update(file.name.encode());source_hash.update(file.read_bytes())
    protocol["source_sha256"]=source_hash.hexdigest()
    write_json(output/"protocol.json",protocol)
    summary={"data":panel.metadata,"policies":{},"benchmarks":{},"walk_forward":{},
             "validation_claim":"retrospective only; no historical AI or broker execution"}
    results={}
    for label,window in WINDOWS.items():
        bench=benchmark(panel,*window,25)
        summary["benchmarks"][label]=bench["metrics"]
        results[("QQQ",label)]=bench
        for name,policy in policies.items():
            result=simulate(panel,policy,*window,25)
            results[(name,label)]=result
            summary["policies"].setdefault(name,{})[label]=comparison(result["metrics"],bench["metrics"])
        print(f"completed {label}: {len(policies)} fixed policies",flush=True)
    cash=Policy("cash",positions=0)
    changes={};folds=[]
    for train_end,start,end in FOLDS:
        ref=benchmark(panel,"2024-05-01",train_end,25)["metrics"]
        train={name:comparison(simulate(panel,p,"2024-05-01",train_end,25)["metrics"],ref)
               for name,p in policies.items()}
        qualified=[name for name,m in train.items() if m["beats_qqq_return_and_drawdown"]]
        selected=max(qualified,key=lambda name:train[name]["excess_return_points"]-train[name]["max_drawdown_pct"]) if qualified else "cash"
        policy=policies.get(selected,cash)
        first=next(day for day in panel.dates if day>=pd.Timestamp(start))
        changes[str(first.date())]=policy
        fold=simulate(panel,policy,start,end,25)
        fold_ref=benchmark(panel,start,end,25)
        folds.append({"train_end":train_end,"test_start":start,"test_end":end,
                      "selected":selected,"qualified_training":qualified,"training":train,
                      "fold_from_cash":comparison(fold["metrics"],fold_ref["metrics"]),
                      "qqq":fold_ref["metrics"]})
    wf={"folds":folds,"costs":{}}
    curves={}
    initial=next(iter(changes.values()))
    for bps in (10,25,50):
        continuous=simulate(panel,initial,FOLDS[0][1],FOLDS[-1][2],bps,changes)
        reference=benchmark(panel,FOLDS[0][1],FOLDS[-1][2],bps)
        stats=comparison(continuous["metrics"],reference["metrics"])
        stats["qqq"]=reference["metrics"]
        stats["rolling42"]=rolling_comparison(continuous["curve"],reference["curve"],42)
        stats["rolling126"]=rolling_comparison(continuous["curve"],reference["curve"],126)
        wf["costs"][str(bps)]=stats
        continuous["curve"].to_csv(output/f"walk_forward_{bps}bps_curve.csv",index=False)
        pd.DataFrame(continuous["orders"]).to_csv(output/f"walk_forward_{bps}bps_orders.csv",index=False)
        if bps==25:
            wf["attribution"]=attribution(panel,continuous)
            curves={"walk_forward":continuous["curve"],"QQQ":reference["curve"]}
    wf["gate_passed"]=bool(all(wf["costs"][str(b)]["beats_qqq_return_and_drawdown"] for b in (25,50))
                           and sum(f["fold_from_cash"]["beats_qqq_return_and_drawdown"] for f in folds)>=2)
    summary["walk_forward"]=wf
    summary["factor_diagnostics"]=factor_diagnostics(panel)
    # Nominate with all windows visible. This is explicitly a retrospective
    # research candidate and cannot replace past walk-forward choices.
    nominees=[name for name,metrics in summary["policies"].items()
              if all(metrics[label]["beats_qqq_return_and_drawdown"] for label in ("full","recent_half"))]
    nominee=max(nominees,key=lambda name:min(summary["policies"][name][x]["excess_return_points"]
                                            for x in ("early","middle","recent"))) if nominees else None
    summary["retrospective_candidate"]=nominee
    summary["retrospective_candidates"]=nominees
    if nominee:
        chosen=results[(nominee,"full")]
        chosen["curve"].to_csv(output/"candidate_full_curve.csv",index=False)
        pd.DataFrame(chosen["orders"]).to_csv(output/"candidate_full_orders.csv",index=False)
        write_json(output/"candidate_policy.json",asdict(policies[nominee]))
        summary["candidate_attribution"]=attribution(panel,chosen)
        summary["candidate_stress"]={}
        for bps in (10,50):
            summary["candidate_stress"][str(bps)]={label:comparison(
                simulate(panel,policies[nominee],*WINDOWS[label],bps)["metrics"],
                benchmark(panel,*WINDOWS[label],bps)["metrics"])
                for label in ("full","recent_half")}
        summary["candidate_stress"]["extra_signal_delay"]={label:comparison(
            simulate(panel,policies[nominee],*WINDOWS[label],25,signal_lag=2)["metrics"],
            benchmark(panel,*WINDOWS[label],25)["metrics"])
            for label in ("full","recent_half")}
        start,end=FOLDS[0][1],FOLDS[-1][2]
        curves[nominee]=simulate(panel,policies[nominee],start,end,25)["curve"]
    summary["elapsed_seconds"]=time.perf_counter()-started
    plot_result(curves,output/"equity_drawdown.png")
    rows=[]
    for name,windows in summary["policies"].items():
        for window,m in windows.items():
            rows.append({"policy":name,"window":window,**m})
    pd.DataFrame(rows).to_csv(output/"comparison.csv",index=False)
    write_json(output/"summary.json",summary)
    write_json(output/"completed.json",{"status":"complete","data_sha256":panel.metadata["input_sha256"],
               "source_sha256":protocol["source_sha256"]})
    print(json.dumps({"output":str(output),"retrospective_candidate":nominee,
                      "walk_forward_gate":wf["gate_passed"],"elapsed_seconds":summary["elapsed_seconds"]},indent=2))
    return summary
