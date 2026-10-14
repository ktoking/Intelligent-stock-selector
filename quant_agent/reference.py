"""Decode a native Quant container as evidence. Never execute its code/actions."""
from hashlib import sha256
import json
from pathlib import Path

from scripts.moomoo_quant_binary import _strategy_class
from .config import canonical, digest
from .pipeline import artifact


def import_reference(store,path):
    from google.protobuf.json_format import MessageToDict
    path=Path(path);payload=path.read_bytes()
    if len(payload)>5_000_000:
        raise ValueError("Reference container exceeds size limit")
    strategy=_strategy_class().FromString(payload)
    decoded=MessageToDict(strategy,preserving_proto_field_name=True)
    variables={}
    sources=[]
    literals=set()
    def walk(value):
        if isinstance(value,dict):
            for k,v in value.items():
                if k=="value" and isinstance(v,str):literals.add(v)
                walk(v)
        elif isinstance(value,list):
            for v in value:walk(v)
    walk(decoded)
    for card in decoded.get("canvasCardList",[]):
        for var in card.get("canvasCardStart",{}).get("varList",[]):
            if var.get("isExplicit"):
                variables[var["name"]]=var.get("initValue",{}).get("value")
        if card.get("isUserCode"):
            source="".join(card.get("userCode",{}).get("userCode",[]))
            sources.append({"card":card["id"],"sha256":sha256(source.encode()).hexdigest(),"source":source})
    summary={"name":strategy.strategyName,"file_sha256":sha256(payload).hexdigest(),
             "container_type":strategy.strategyType,"canvas_cards":len(strategy.canvasCardList),"variables":variables,
             "observed_bar_types":sorted(x for x in literals if x.startswith("K_")),
             "observed_times":sorted(x for x in literals if len(x)==8 and x[2]==":" and x[5]==":"),
             "uses_account_net_asset":any("net_asset(" in s["source"] for s in sources),
             "uses_market_orders":any("place_market(" in s["source"] for s in sources),
             "status":"reference_only_not_executed_or_backtested",
             "constraints":"Untrusted reference. Native 5-minute Canvas semantics require a separate faithfully ported engine; daily factor simulation is not its backtest."}
    name="reference-"+summary["file_sha256"][:16]
    if not (store.home/"artifacts"/(name+".json")).exists():
        artifact(store,name,{"summary":summary,"decoded":decoded,"sources":sources})
    existing=store.meta("strategy_references",[])
    existing=[x for x in existing if x["file_sha256"]!=summary["file_sha256"]]+[summary]
    store.set_meta("strategy_references",existing)
    store.event("strategy_reference_imported",summary)
    return summary
