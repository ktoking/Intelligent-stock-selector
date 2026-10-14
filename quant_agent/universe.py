"""Current index constituents and user watchlist; movers enter research first."""
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import time
import math

from research.daily_factor_lab.core import load_panel
from research.daily_factor_lab.refresh import latest_completed_session
from .broker import FutuSim
from .config import canonical, digest
from .market_data import full_symbol
from .pipeline import artifact
from .store import utcnow


def mover(quote, panel=None, stock_type=None):
    symbol=quote["code"]
    price=float(quote.get("last_price") or 0)
    previous=float(quote.get("prev_close_price") or 0)
    turnover=float(quote.get("turnover") or 0)
    if not all(math.isfinite(v) for v in (price,previous,turnover)):
        return None
    if price<5 or previous<=0 or (quote.get("stock_type") or stock_type)!="STOCK" or turnover<20_000_000:
        return None
    change=(price/previous-1)*100
    if abs(change)<3:
        return None
    ratio=None
    if panel and symbol in panel.symbols and panel.metadata["end"]==str(quote.get("data_date")):
        j=panel.symbols.index(symbol);v=panel.fields["volume"][-21:-1,j]
        import numpy as np
        if len(v)==20 and np.isfinite(v).all() and v.mean()>0:
            ratio=float(panel.fields["volume"][-1,j]/v.mean())
    return {"symbol":symbol,"observed_at":utcnow(),"data_date":quote.get("data_date"),"price":price,
            "change_pct":change,"turnover":turnover,"volume_ratio":ratio,
            "status":"research_candidate_not_order","source":"Futunn REST snapshot"}


def scan(store,refresh_members=True):
    broker=FutuSim(store.settings)
    from .indices import refresh_indices
    indexes=refresh_indices(store) if refresh_members else store.meta('index_universes')
    if indexes is None:indexes=refresh_indices(store)
    watchlist=set(store.meta('watchlist_symbols',[]))
    if refresh_members or not watchlist:
        response=broker.request("GET","/api/v1.0/quote/user-security",{"group_name":"US"})
        rows=response["data"]["security_list"]
        watchlist={x["code"] for x in rows if x.get("stock_type")=="STOCK" and x["code"].startswith("US.")}
        store.set_meta('watchlist_symbols',sorted(watchlist))
    ndx=set(indexes['indexes']['nasdaq100']['members']);spx=set(indexes['indexes']['sp500']['members'])
    broader=ndx|spx
    panel=load_panel(store.meta("current_data_dir",store.settings.data_dir))
    symbols=sorted(watchlist|broader)
    hits=[]
    received=set()
    for i in range(0,len(symbols),400):
        for quote in broker.snapshots(symbols[i:i+400]):
            received.add(quote['code'])
            if quote.get('suspension') or quote.get('equity_valid') is False:continue
            hit=mover(quote,panel,stock_type="STOCK")
            if hit:
                hit['memberships']=[name for name,pool in [('watchlist',watchlist),('nasdaq100',ndx),('sp500',spx)] if hit['symbol'] in pool]
                hit["origin"]="user_watchlist" if hit["symbol"] in watchlist else 'nasdaq100' if hit['symbol'] in ndx else 'sp500'
                hit["classification_basis"]="Futunn current watchlist or paginated index constituents"
                hits.append(hit)
        time.sleep(.3)
    hits.sort(key=lambda r:(-abs(r["change_pct"]),r["symbol"]))
    result={"created_at":utcnow(),"watchlist_stocks":len(watchlist),"scanned":len(symbols),"hits":hits[:30],
            'nasdaq100_count':len(ndx),'sp500_count':len(spx),'missing_symbols':sorted(set(symbols)-received),
            'index_observed_at':indexes['observed_at'],'all_hits_count':len(hits),
            "universe_source":"Futunn current US watchlist + Nasdaq100 + S&P500 constituents, deduplicated",
            "selection_bias":"Today-observed movers may enter future versions only. Retrospective backtests on today's selected names remain selection biased."}
    store.set_meta("universe_scan",result)
    store.event("universe_scanned",{"scanned":len(symbols),"hits":len(hits)})
    for hit in hits:
        key='anomaly:'+hit['symbol']+':'+str(hit['data_date'])+':'+('up' if hit['change_pct']>0 else 'down')
        if store.once(key):
            store.event('price_anomaly',{k:hit[k] for k in ('symbol','change_pct','data_date','source')})
    artifact(store,"universe-scan-"+digest(result)[:16],result)
    return result


def enrich(store,scan_result,limit=3):
    if not 1<=limit<=3:
        raise ValueError("History expansion capped at 3 newly observed names per run")
    source=Path(store.meta("current_data_dir",store.settings.data_dir))
    panel=load_panel(source)
    asof=latest_completed_session()
    if panel.metadata["end"]!=asof:
        raise ValueError("Refresh existing dataset before adding symbols")
    names=[r["symbol"] for r in scan_result["hits"] if r["symbol"] not in panel.symbols and r["data_date"]==asof][:limit]
    if not names:
        return {"added":[],"reason":"No new verified current movers"}
    output=store.home/"market-data"/(asof+"-universe-"+digest([panel.metadata["input_sha256"],names])[:12])
    if output.exists():
        raise ValueError("Universe version already exists")
    shutil.copytree(source,output)
    broker=FutuSim(store.settings)
    for symbol in names:
        frame=full_symbol(broker,symbol,panel.metadata["start"],asof)
        frame.to_pickle(output/(symbol[3:]+".pkl"))
    universe=json.loads((output/"universe.json").read_text());types=json.loads((output/"security_types.json").read_text())
    universe["symbols"]=sorted(set(universe["symbols"])|set(names))
    universe["addition_observed_at"]=scan_result["created_at"]
    universe["addition_reason"]="Current anomaly research intake; not point-in-time historical membership"
    for s in names:types[s]="STOCK"
    (output/"universe.json").write_text(canonical(universe));(output/"security_types.json").write_text(canonical(types))
    fresh=load_panel(output)
    (output/"completed.json").write_text(canonical({"status":"complete","input_sha256":fresh.metadata["input_sha256"]}))
    # Adding names cannot alter the frozen ranking universe of a forward/paper strategy.
    store.set_meta("current_data_dir",str(output))
    store.event("research_universe_expanded",{"added":names,"directory":str(output),"observed_at":scan_result["created_at"]})
    return {"added":names,"directory":str(output),"data":fresh.metadata}
