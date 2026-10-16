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


def repair_symbol(broker,symbol,original,asof,reason):
    if 'Historical values revised' not in reason:
        from research.daily_factor_lab.refresh import merge_daily
        start=pd.to_datetime(str(int(original.date.max())),format='%Y%m%d').date()-timedelta(days=7)
        batch=broker.request('GET','/api/v1.0/quote/'+symbol+'/history-kline',
            {'start':str(start),'end':asof,'ktype':2,'num':370,'autype':1})['data']['kline_list']
        try:
            return merge_daily(original,batch,asof,allow_flow_revisions=True,allow_close_corrections=True),'incremental_retry'
        except ValueError as exc:
            if 'Historical values revised' not in str(exc):raise
    start=pd.to_datetime(str(int(original.date.min())),format='%Y%m%d').date().isoformat()
    return full_symbol(broker,symbol,start,asof),'full_history_revision'


def finalization_due(store,now=None):
    if not store.settings.auto_refresh:return False
    from .schedule import calendar_state
    state=calendar_state(now)
    stamp=pd.Timestamp(state['now'])
    return (not state['session'] and stamp>=pd.Timestamp(state['completed_close'])+pd.Timedelta(minutes=60)
        and not store.meta('data_finalized:'+state['completed_session']))


def refresh_cache(store,*,recheck=False):
    with (store.home/"market-data.lock").open("a") as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Market cache refresh already running") from None
        try:
            return _refresh_cache(store,recheck=recheck)
        finally:
            fcntl.flock(lock,fcntl.LOCK_UN)


def _refresh_cache(store,*,recheck=False):
    asof=latest_completed_session()
    source=Path(store.meta("current_data_dir",store.settings.data_dir))
    previous=load_panel(source)
    if previous.metadata["end"]==asof and not recheck:
        synchronize_execution_cache(store,source)
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
        output=parent/(asof+"-repaired-"+str(time.time_ns()))
        shutil.copytree(failed,output)
        repaired=[]
        from .files import atomic_write
        for index,failure in enumerate(report["failures"]):
            symbol=failure["symbol"]
            original=pd.read_pickle(source/(symbol[3:]+".pkl"))
            frame,repair_mode=repair_symbol(broker,symbol,original,asof,failure['reason'])
            frame.to_pickle(output/(symbol[3:]+".pkl"))
            repaired.append({"symbol":symbol,"reason":failure["reason"],"rows":len(frame),'repair_mode':repair_mode})
            atomic_write(output/'refresh_report.json',canonical({**report,'status':'incomplete',
                'repaired':repaired,'failures':report['failures'][index+1:]}))
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
    if recheck:retire_provisional_shadows(store,output)
    execution_ok=synchronize_execution_cache(store,output,source_candidates=[str(source)])
    store.set_meta('current_data_dir',str(output))
    if recheck:
        store.set_meta('data_finalized:'+asof,{'observed_at':pd.Timestamp.now(tz='UTC').isoformat(),
            'data_sha256':fresh.metadata['input_sha256'],'directory':str(output)})
        store.event('data_finalization_checked',{'asof':asof,'data_sha256':fresh.metadata['input_sha256'],
            'previous_sha256':previous.metadata['input_sha256'],'changed':fresh.metadata['input_sha256']!=previous.metadata['input_sha256']})
    store.event('data_refreshed',{**fresh.metadata,'directory':str(output),
        'execution_blocked':not execution_ok,'repaired':report.get('repaired',[])})
    return {**fresh.metadata,**({'execution_blocked':True} if not execution_ok else {})}


def retire_provisional_shadows(store,research_dir):
    """Revalidate unused same-session candidates after closing-bar corrections.

    Already observed Shadow and every paper owner retain their frozen state.
    """
    from .dataset import frozen_view
    from research.daily_factor_lab.paper import data_prefix_hash
    panel=load_panel(research_dir)
    for record in store.strategies(['shadow']):
        evidence=record['evidence'];identity=record['id']
        if evidence['seed_asof']!=panel.metadata['end']:continue
        state=store.meta('shadow_state:'+identity,{})
        if state.get('holdings') or store.meta('shadow_pending:'+identity):continue
        with store.transaction() as db:
            observed=db.execute('SELECT COUNT(*) FROM shadow WHERE strategy=?',(identity,)).fetchone()[0]
            ordered=db.execute('SELECT COUNT(*) FROM orders WHERE strategy=?',(identity,)).fetchone()[0]
        if observed or ordered:continue
        updated=frozen_view(panel,record)
        if data_prefix_hash(updated,evidence['seed_asof'])==evidence['seed_hash']:continue
        store.transition(identity,'shadow','rejected',{**evidence,
            'reason':'收盘数据后续修订；未开始观察的旧候选留档，新数据版本必须重新验证。',
            'superseded_by_data_sha256':panel.metadata['input_sha256']})


def synchronize_execution_cache(store,research_dir,*,source_candidates=()):
    """Publish each independently verified group; failed groups keep their seed."""
    from .execution_cache import prepare_execution_cache,inherited_review
    from .dataset import frozen_view
    from research.daily_factor_lab.paper import data_prefix_hash
    from .panel_cache import load_panel as cached_panel
    groups={};errors=[]
    for record in store.strategies(['paper','shadow']):
        candidates=[store.meta('execution_cache:'+record['id']),store.meta('execution_data_dir'),
                    *source_candidates,store.meta('current_data_dir'),str(research_dir)]
        for candidate in dict.fromkeys(c for c in candidates if c):
            try:seed=frozen_view(cached_panel(candidate),record)
            except (ValueError,OSError,EOFError):continue
            if data_prefix_hash(seed,record['evidence']['seed_asof'])==record['evidence']['seed_hash']:
                groups.setdefault(candidate,[]).append(record)
                store.set_meta('execution_cache:'+record['id'],candidate)
                break
        else:errors.append({'strategies':[record['id']],'reason':'No preserved execution seed for strategy'})
    latest=cached_panel(research_dir);research_hash=latest.metadata['input_sha256']
    reviews=[json.loads(p.read_text()) for p in (store.home/'execution-reviews').glob('*.json')]
    prepared=[]
    for candidate,records in groups.items():
        try:
            previous=cached_panel(candidate);source_hash=previous.metadata['input_sha256']
            if source_hash==research_hash:
                prepared.append((Path(candidate),records));continue
            review=next((r for r in reviews if r.get('source_sha256')==source_hash and r.get('research_sha256')==research_hash),None)
            if review is None:review=inherited_review(candidate,research_dir,previous,latest)
            if review and review.get('inherited_from') and previous.metadata['end']==latest.metadata['end']:
                prepared.append((Path(candidate),records));continue
            directory,_=prepare_execution_cache(store,research_dir,source=candidate,records=records,review=review)
            prepared.append((directory,records))
        except (ValueError,OSError,EOFError) as exc:
            from .observability import error_evidence
            errors.append({'strategies':[r['id'] for r in records],**error_evidence(exc),'reason':str(exc)[:240]})
    # Options depend only on their authorized underlying data, not on unrelated
    # stocks needing a corporate-action review. The same strict append gate applies.
    if store.settings.option_paper_enabled:
        try:synchronize_option_cache(store,research_dir)
        except (ValueError,OSError,EOFError) as exc:
            store.set_meta('option_data_blocked',{'error_type':type(exc).__name__,'reason':str(exc)[:240]})
    evidence=({'asof':latest_completed_session(),'error_type':errors[0].get('error_type','ValueError'),
        'reason':errors[0]['reason'],'groups':errors} if errors else None)
    with store.transaction() as db:
        for directory,records in prepared:
            for record in records:store.set_meta('execution_cache:'+record['id'],str(directory),db)
            if any(r['stage']=='paper' for r in records):store.set_meta('execution_data_dir',str(directory),db)
            store.event('execution_data_appended',{'directory':str(directory),'asof':latest_completed_session(),'strategies':[r['id'][:12] for r in records]},db)
        store.set_meta('execution_data_blocked',evidence,db)
        if evidence:store.event('execution_data_blocked',evidence,db)
    return not errors


def synchronize_option_cache(store,research_dir):
    """Strict as-observed underlying history; no revision approval or rule change."""
    from .execution_cache import append_observed
    from .config import digest
    from .files import atomic_write
    import pickle
    source=Path(store.meta('option_data_dir',store.meta('execution_data_dir',store.settings.data_dir)))
    source_types=json.loads((source/'security_types.json').read_text())
    names=[s for s in ('US.NVDA','US.QQQ') if (source/(s[3:]+'.pkl')).exists()]
    if not names:raise ValueError('Option underlying history missing')
    frames={}
    for symbol in names:
        old=pd.read_pickle(source/(symbol[3:]+'.pkl'));new=pd.read_pickle(Path(research_dir)/(symbol[3:]+'.pkl'))
        frames[symbol],_=append_observed(old,new)
    tag=digest({s:__import__('hashlib').sha256(pickle.dumps(f)).hexdigest() for s,f in frames.items()})[:20]
    output=store.home/'option-signals'/tag;output.mkdir(parents=True,exist_ok=True,mode=0o700)
    if (output/'completed.json').exists():
        for symbol,frame in frames.items():
            if not frame.equals(pd.read_pickle(output/(symbol[3:]+'.pkl'))):raise ValueError('Published option signal data changed')
        panel=load_panel(output)
        if panel.metadata['input_sha256']!=json.loads((output/'completed.json').read_text())['input_sha256']:raise ValueError('Published option signal manifest changed')
        store.set_meta('option_data_dir',str(output));store.set_meta('option_data_blocked',None)
        return panel
    for symbol,frame in frames.items():atomic_write(output/(symbol[3:]+'.pkl'),pickle.dumps(frame,protocol=pickle.HIGHEST_PROTOCOL))
    atomic_write(output/'universe.json',canonical({'symbols':names}))
    atomic_write(output/'security_types.json',canonical({s:source_types[s] for s in names}))
    panel=load_panel(output)
    if panel.metadata['end']!=load_panel(research_dir).metadata['end']:raise ValueError('Option signals do not reach completed session')
    atomic_write(output/'completed.json',canonical(panel.metadata))
    store.set_meta('option_data_dir',str(output));store.set_meta('option_data_blocked',None)
    return panel
