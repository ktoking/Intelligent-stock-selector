"""Create a new cache version; corporate-action revisions trigger full reloads."""
from datetime import date, timedelta
import json
from pathlib import Path
import shutil
import time

import pandas as pd

from research.daily_factor_lab.core import load_panel
from research.daily_factor_lab.refresh import refresh
from .exchange_clock import latest_completed_session
from .broker import BrokerError,FutuSim
from .config import canonical
import fcntl


def full_symbol(broker,symbol,start,end):
    cursor,finish=date.fromisoformat(start),date.fromisoformat(end)
    rows=[]
    while cursor<=finish:
        last=min(finish,cursor+timedelta(days=300))
        for attempt in range(4):
            try:
                result=broker.request("GET","/api/v1.0/quote/"+symbol+"/history-kline",
                                      {"start":str(cursor),"end":str(last),"ktype":2,"num":370,"autype":1})
                break
            except BrokerError as exc:
                if exc.ret_code!=-11 or attempt==3:
                    raise
                time.sleep(min(60,(exc.retry_after or 15)+1))
        batch=result["data"]["kline_list"]
        if len(batch)>=370:
            raise ValueError("Full reload may be truncated")
        rows.extend(batch);cursor=last+timedelta(days=1);time.sleep(.8)
    frame=pd.DataFrame(rows)
    if frame.empty or "date" not in frame:
        raise ValueError("Full symbol history missing")
    frame=frame.drop_duplicates("date").sort_values("date").reset_index(drop=True)
    if str(int(frame.date.iloc[-1]))!=end.replace("-", ""):
        raise ValueError("Full symbol history does not reach completed session")
    return frame


def refresh_cache(store):
    with (store.home/"market-data.lock").open("a") as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Market cache refresh already running") from None
        try:
            return _refresh_cache(store)
        finally:
            fcntl.flock(lock,fcntl.LOCK_UN)


def _refresh_cache(store):
    asof=latest_completed_session()
    source=Path(store.meta("current_data_dir",store.settings.data_dir))
    previous=load_panel(source)
    if previous.metadata["end"]==asof:
        return previous.metadata
    parent=store.home/"market-data";parent.mkdir(exist_ok=True,mode=0o700)
    broker=FutuSim(store.settings)
    # Reuse successful responses in an unpublished incomplete version instead of
    # hammering all symbols again. Published versions are never overwritten.
    incomplete=[]
    for path in parent.glob(asof+"*"):
        report_path=path/"refresh_report.json"
        if report_path.exists():
            report=json.loads(report_path.read_text())
            if report.get("status") in ("incomplete","complete","complete_new_version") and report.get("source_cache")==str(source.resolve()):
                incomplete.append(path)
    if incomplete:
        failed=max(incomplete,key=lambda p:(p/"refresh_report.json").stat().st_mtime)
    else:
        failed=parent/(asof+"-"+str(int(time.time())))
        try:
            refresh(source,failed,asof,fetch=lambda path,q:broker.request("GET",path,q),allow_flow_revisions=True,allow_close_corrections=True)
        except RuntimeError:
            if not (failed/"refresh_report.json").exists():
                raise
    report=json.loads((failed/"refresh_report.json").read_text())
    output=failed
    if report.get('failures'):
        output=parent/(asof+"-repaired-"+str(int(time.time())))
        shutil.copytree(failed,output)
        repaired=[]
        for failure in report["failures"]:
            symbol=failure["symbol"]
            original=pd.read_pickle(source/(symbol[3:]+".pkl"))
            start=pd.to_datetime(str(int(original.date.min())),format="%Y%m%d").date().isoformat()
            frame=full_symbol(broker,symbol,start,asof)
            frame.to_pickle(output/(symbol[3:]+".pkl"))
            repaired.append({"symbol":symbol,"reason":failure["reason"],"full_reload_rows":len(frame)})
        report={**report,"status":"complete_new_version","repaired":repaired,"original_failures":report["failures"],"failures":[]}
        (output/"refresh_report.json").write_text(canonical(report))
    # A successful HTTP response may still contain a provisional/invalid daily
    # candle. Re-fetch that symbol in the unpublished version on the next retry;
    # never repeatedly copy a known-bad "complete_new_version" or edit a published one.
    import numpy as np
    invalid=[]
    for symbol in previous.symbols:
        frame=pd.read_pickle(output/(symbol[3:]+'.pkl'))
        values=frame[['open','high','low','close','volume','turnover']].astype(float)
        prices=values[['open','high','low','close']]
        if (not np.isfinite(values.to_numpy()).all() or (prices<=0).any().any()
                or (values[['volume','turnover']]<0).any().any()
                or (values.high<prices.max(axis=1)).any() or (values.low>prices.min(axis=1)).any()):
            invalid.append(symbol)
    if invalid:
        if (output/'completed.json').exists():
            repaired_output=parent/(asof+'-validated-'+str(time.time_ns()))
            shutil.copytree(output,repaired_output)
            output=repaired_output
        repairs=list(report.get('quality_repairs',[]))
        for symbol in invalid:
            original=pd.read_pickle(source/(symbol[3:]+'.pkl'))
            start=pd.to_datetime(str(int(original.date.max())),format='%Y%m%d').date()-timedelta(days=7)
            batch=broker.request('GET','/api/v1.0/quote/'+symbol+'/history-kline',
                {'start':str(start),'end':asof,'ktype':2,'num':370,'autype':1})['data']['kline_list']
            from research.daily_factor_lab.refresh import merge_daily
            # Overlap revisions still require the full-reload/version gate.
            frame=merge_daily(original,batch,asof)
            frame.to_pickle(output/(symbol[3:]+'.pkl'))
            repairs.append({'symbol':symbol,'reason':'Invalid OHLCV response; re-fetched from Futunn','observed_at':pd.Timestamp.now(tz='UTC').isoformat()})
        report={**report,'quality_repairs':repairs}
        (output/'refresh_report.json').write_text(canonical(report))
    fresh=load_panel(output)
    if fresh.metadata["end"]!=asof or fresh.symbols!=previous.symbols:
        raise ValueError("New cache coverage or universe differs")
    # Load validates OHLCV; require a latest mark for each frozen asset as well.
    if not np.isfinite(fresh.fields["close"][-1]).all():
        raise ValueError("New cache has missing latest asset marks")
    (output/"completed.json").write_text(canonical({"status":"complete","input_sha256":fresh.metadata["input_sha256"]}))
    # Valid vendor research data remain usable even when an existing execution
    # seed needs corporate-action review. Preserve the old execution pointer.
    if not store.meta('execution_data_dir'):store.set_meta('execution_data_dir',str(source))
    store.set_meta('current_data_dir',str(output))
    from .execution_cache import prepare_execution_cache
    from .dataset import frozen_view
    from research.daily_factor_lab.paper import data_prefix_hash
    try:
        groups={}
        for record in store.strategies(['paper','shadow']):
            candidates=[store.meta('execution_cache:'+record['id']),store.meta('execution_data_dir'),str(source)]
            for candidate in dict.fromkeys(c for c in candidates if c):
                seed=frozen_view(load_panel(candidate),record);evidence=record['evidence']
                if data_prefix_hash(seed,evidence['seed_asof'])==evidence['seed_hash']:
                    groups.setdefault(candidate,[]).append(record);break
            else:raise ValueError('No preserved execution seed for strategy')
        prepared=[]
        for candidate,records in groups.items():
            directory,_=prepare_execution_cache(store,output,source=candidate,records=records)
            prepared.append((directory,records))
    except ValueError as exc:
        store.pause('Execution history price basis or frozen seed changed; review required')
        store.event('execution_data_blocked',{'asof':asof,'error_type':type(exc).__name__,'reason':str(exc)[:240]})
        store.event('data_refreshed',{**fresh.metadata,'directory':str(output),'execution_blocked':True})
        return {**fresh.metadata,'execution_blocked':True}
    with store.transaction() as db:
        for directory,records in prepared:
            for record in records:store.set_meta('execution_cache:'+record['id'],str(directory),db)
            if any(r['stage']=='paper' for r in records):store.set_meta('execution_data_dir',str(directory),db)
            store.event('execution_data_appended',{'directory':str(directory),'asof':asof,'strategies':[r['id'][:12] for r in records]},db)
    store.set_meta("current_data_dir",str(output))
    store.event("data_refreshed",{**fresh.metadata,"directory":str(output),"repaired":report.get("repaired",[])})
    return fresh.metadata
