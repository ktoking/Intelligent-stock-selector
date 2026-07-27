#!/usr/bin/env python3
"""Walk-forward comparison of simple momentum and reversion signal families."""
from __future__ import annotations

import json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import DEFAULT_SYMBOLS, load_market_data, weekday_sessions  # noqa: E402
from scripts.okx_return_model_research import dataset, metric  # noqa: E402

NY = ZoneInfo("America/New_York"); OUTPUT = ROOT / "data" / "okx_signal_family_90d.json"


def trade(rows, score, horizon, threshold):
    value = rows.copy(); value["prediction"] = score
    value = value[abs(value.prediction) >= threshold]
    active=[]; result=[]
    for entry, group in value.groupby("entry_time", sort=True):
        active=[x for x in active if x["exit_time"] > entry]
        slots=5-len(active)
        for _, row in group.reindex(group.prediction.abs().sort_values(ascending=False).index).head(slots).iterrows():
            direction=1 if row.prediction>0 else -1
            item={"entry_time":int(entry),"exit_time":int(row[f"exit_time_{horizon}"]),
                  "side":"LONG" if direction>0 else "SHORT",
                  "net_r":(direction*float(row[f"return_bps_{horizon}"])-14)/100}
            active.append(item); result.append(item)
    return result


def main():
    end=datetime.now(NY).date()-timedelta(days=1); sessions=weekday_sessions(end,90)
    days=[x.isoformat() for x in sessions]
    rows=dataset(load_market_data(OKX(settings()),DEFAULT_SYMBOLS,sessions))
    specs=[]
    for feature in ("r1","r3","r6","r12","ema_gap"):
        for direction in (1,-1):
            for horizon in (6,12):
                for threshold in (5,10,20,40):
                    specs.append((feature,direction,horizon,threshold))
    results=[]
    for split,target in (("validation",days[40:50]),("development",days[50:60]),("final",days[60:90])):
        subset=rows[rows.date.isin(target)]
        for feature,direction,horizon,threshold in specs:
            values=subset[feature]*10000*direction
            m=metric(trade(subset,values,horizon,threshold))
            results.append({"split":split,"feature":feature,"style":"momentum" if direction==1 else "reversion",
                            "horizon":horizon,"threshold":threshold,**m})
    # Rank only on validation + development; final remains diagnostic.
    keyed={}
    for row in results:
        key=(row["feature"],row["style"],row["horizon"],row["threshold"])
        keyed.setdefault(key,{})[row["split"]]=row
    eligible=[]
    for key,parts in keyed.items():
        if all(s in parts for s in ("validation","development","final")):
            v,d=parts["validation"],parts["development"]
            if v["trades"]>=40 and d["trades"]>=40 and v["net_r"]>0 and d["net_r"]>0:
                eligible.append({"config":key,"validation":v,"development":d,"final":parts["final"]})
    eligible.sort(key=lambda x:min(x["validation"]["profit_factor"] or 0,x["development"]["profit_factor"] or 0),reverse=True)
    report={"generated_at":datetime.now(timezone.utc).isoformat(),"eligible":eligible[:20],
            "tested":len(specs),"warning":"family research; final dates already diagnostic"}
    OUTPUT.write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=="__main__": main()
