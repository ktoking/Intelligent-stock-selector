from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

import exchange_calendars as xc
import pandas as pd

from .core import Policy, catalog, load_panel, rank, scheduled, simulate, weights_for, market_exposure
from .experiment import run, write_json


def snapshot(args):
    panel=load_panel(asof=args.asof)
    if str(panel.dates[-1].date())!=args.asof:
        raise ValueError("Requested signal date has no completed cached session")
    policy=Policy(**json.loads(Path(args.policy_file).read_text())) if args.policy_file else catalog()[args.policy]
    result=simulate(panel,policy,args.start,args.asof,25)
    i=len(panel.dates)-1
    candidates=rank(panel,i,policy)[:policy.positions]
    weights=weights_for(panel,i,candidates,policy,market_exposure(panel,i,policy))
    calendar=xc.get_calendar("XNYS")
    future=calendar.sessions_in_range(panel.dates[-1]+pd.Timedelta(days=1),panel.dates[-1]+pd.Timedelta(days=10))
    next_day=str(future[0].date())
    nav=result["curve"].equity
    output={"status":"paper_replay_snapshot; not actual account or executable orders",
            "signal_date":args.asof,"next_session":next_day,"data":panel.metadata,
            "policy":asdict(policy),"paper_replay":{"metrics":result["metrics"],
            "cash":float(result["curve"].cash.iloc[-1]),"equity":float(result["curve"].equity.iloc[-1]),
            "positions":result["positions"],
            "one_day_return_pct":float((nav.iloc[-1]/nav.iloc[-2]-1)*100) if len(nav)>1 else None,
            "five_session_return_pct":float((nav.iloc[-1]/nav.iloc[-6]-1)*100) if len(nav)>5 else None,
            "today_orders":[o for o in result["orders"] if o["date"]==args.asof]},
            "market":{"symbol":"US.QQQ","close":float(panel.fields["close"][i,panel.qqq]),
                      "ema100":float(panel.fields["ema100"][i,panel.qqq]),
                      "ema200":float(panel.fields["ema200"][i,panel.qqq]),
                      "new_entry_gate":bool(market_exposure(panel,i,policy)>0)},
            "next_session_rebalance":scheduled(panel.dates.append(pd.DatetimeIndex([future[0]])),
                                              len(panel.dates),policy.schedule),
            "ranked_watchlist":[{"symbol":panel.symbols[j],"score":float(panel.fields[policy.factor][i,j]),
                                 "indicative_new_portfolio_weight":float(weights[j])} for j in candidates],
            "planning_note":"Indicative fresh-portfolio weights; held-rank buffer, stop exclusions, execution schedule and next-open cash alter actual paper rebalance. No orders generated."}
    if args.research_summary:
        review=json.loads(Path(args.research_summary).read_text())
        if review["data"]["end"]>args.asof:
            raise ValueError("Research summary contains data after snapshot date")
        output["retrospective_research_evidence"]={
            "candidate":review["retrospective_candidate"],
            "validation_claim":review["validation_claim"],
            "walk_forward_gate_passed":review["walk_forward"]["gate_passed"],
            "candidate_stress":review.get("candidate_stress",{}),
        }
    write_json(args.output,output)
    print(json.dumps({"output":args.output,"policy":policy.name,"asof":args.asof},ensure_ascii=False))


def main():
    parser=argparse.ArgumentParser(description="Daily factor lab: retrospective research and local paper snapshots")
    sub=parser.add_subparsers(dest="command",required=True)
    research=sub.add_parser("run")
    research.add_argument("--output",required=True,help="New immutable run directory")
    research.add_argument("--data-dir")
    research.add_argument("--risk-sensitivity",action="store_true")
    snap=sub.add_parser("snapshot")
    snap.add_argument("--asof",required=True)
    snap.add_argument("--start",default="2024-05-01")
    snap.add_argument("--policy",default="trend_buffer20",choices=list(catalog()))
    snap.add_argument("--policy-file")
    snap.add_argument("--output",required=True)
    snap.add_argument("--research-summary")
    args=parser.parse_args()
    if args.command=="run":
        run(args.output,args.data_dir,args.risk_sensitivity)
    else:
        snapshot(args)


if __name__=="__main__":
    main()
