"""Durable, version-bound research steps; no trading endpoint or automatic approval."""
from contextlib import contextmanager
from datetime import datetime,timezone,timedelta
import fcntl
import json
import os
from pathlib import Path
import time

from .config import canonical,digest
from .observability import error_evidence,emit

VERSION=1


class StepDeferred(RuntimeError):
    pass


def stamp():return datetime.now(timezone.utc).isoformat()


@contextmanager
def workflow_lock(store):
    with (store.home/'daily-workflow.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise StepDeferred('Research workflow already owned by another process') from None
        try:yield
        finally:fcntl.flock(lock,fcntl.LOCK_UN)


def run_step(store,job,asof,version,action):
    key=digest([VERSION,job,asof,version,store.settings.sha256])
    with (store.home/('checkpoint-'+job+'.lock')).open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise StepDeferred('Research step already running: '+job) from None
        try:
            with store.transaction() as db:
                row=db.execute('SELECT * FROM checkpoints WHERE id=?',(key,)).fetchone()
                old=dict(row) if row else {}
                if old.get('status')=='completed':
                    store.set_meta('task:'+job,json.loads(old['state']),db)
                    return json.loads(old['result'])
                if old.get('retry_after') and old['retry_after']>stamp():
                    raise StepDeferred('Research step waiting for persisted retry: '+job)
                attempt=old.get('attempts',0)+1
                state={'status':'running','asof':asof,'data_dir':store.meta('current_data_dir',store.settings.data_dir),'started_at':stamp(),'attempt':attempt,'checkpoint':key,
                       'input_version':version,
                       'recovered_interruption':old.get('status')=='running'}
                db.execute('INSERT OR REPLACE INTO checkpoints VALUES (?,?,?,?,?,?,?,?,?)',
                    (key,job,asof,version,'running',attempt,None,None,canonical(state)))
                store.set_meta('task:'+job,state,db)
                store.event('checkpoint_started',{'job':job,'checkpoint':key,'attempt':attempt,'interrupted':state['recovered_interruption']},db)
            started=time.monotonic()
            try:
                result=action()
                encoded=canonical(result)
            except Exception as exc:
                retry=(datetime.now(timezone.utc)+timedelta(seconds=min(1800,30*2**min(attempt-1,6)))).isoformat()
                state={**state,'status':'failed','finished_at':stamp(),'duration_seconds':round(time.monotonic()-started,2),'retry_after':retry,**error_evidence(exc)}
                with store.transaction() as db:
                    db.execute("UPDATE checkpoints SET status='failed',retry_after=?,state=? WHERE id=?",(retry,canonical(state),key))
                    store.set_meta('task:'+job,state,db);store.event('checkpoint_failed',{'job':job,**state},db)
                emit('checkpoint_failed',job=job,**state)
                raise
            state={**state,'status':'completed','finished_at':stamp(),'duration_seconds':round(time.monotonic()-started,2)}
            # The result and completion flag commit together; restart can never
            # observe a completed step whose persisted result is missing.
            with store.transaction() as db:
                db.execute("UPDATE checkpoints SET status='completed',result=?,retry_after=NULL,state=? WHERE id=?",(encoded,canonical(state),key))
                store.set_meta('task:'+job,state,db);store.event('checkpoint_completed',{'job':job,**state},db)
            emit('checkpoint_completed',job=job,checkpoint=key,attempt=attempt)
            return result
        finally:fcntl.flock(lock,fcntl.LOCK_UN)


def summary(store):
    with store.read() as db:
        rows=[dict(r) for r in db.execute('SELECT job,asof,version,status,attempts,retry_after,state FROM checkpoints ORDER BY rowid DESC LIMIT 30')]
    for row in rows:row['state']=json.loads(row['state'])
    return rows


def retry_failed(store,job=None):
    """Explicit operator retry; never resets successful results or order state."""
    with store.transaction() as db:
        if job:
            db.execute("UPDATE checkpoints SET retry_after=NULL WHERE status='failed' AND job=?",(job,))
        else:db.execute("UPDATE checkpoints SET retry_after=NULL WHERE status='failed'")
        rows=db.execute("SELECT key FROM meta WHERE key LIKE 'daily_retry_after:%'").fetchall()
        for row in rows:store.set_meta(row[0],None,db)
        store.event('checkpoint_retry_requested',{'job':job},db)


def recover_factor_result(store):
    """Close the artifact->metadata crash gap without changing the frozen DSL."""
    from .panel_cache import load_panel
    from .pipeline import engine_hash
    from hashlib import sha256
    paths=sorted((store.home/'artifacts').glob('evolution-*.json'))
    if not paths:
        # No artifact may mean the process stopped immediately after writing
        # the generated candidate source. Inspect undeployed candidates only.
        from .panel_cache import load_panel
        proposed=[r for r in store.strategies(['proposed']) if r['spec'].get('factor_expression')]
        if proposed:recover_unpublished_factor_code(store,load_panel(store.meta('current_data_dir',store.settings.data_dir)))
        return
    panel=load_panel(store.meta('current_data_dir',store.settings.data_dir));version=panel.metadata['input_sha256']
    if store.meta('evolution_completed:'+version):return
    records={r['id']:r for r in store.strategies()}
    for path in paths:
        try:
            value=json.loads(path.read_text())
            if not isinstance(value,dict):raise ValueError('Evolution artifact is not an object')
        except (OSError,ValueError) as exc:
            store.event('factor_artifact_read_failed',{'artifact':path.name,**error_evidence(exc)})
            continue
        record=records.get(value.get('strategy'))
        if not record or record['spec'].get('research_data_sha256')!=version:continue
        if record['spec']['engine_sha256']!=engine_hash(record['spec']):continue
        if value.get('status')=='code_generated_causal_checks_passed':
            code=Path(value['code_path'])
            if not code.is_file() or sha256(code.read_bytes()).hexdigest()!=value.get('source_sha256'):continue
        elif value.get('status')!='rejected_causal_checks':continue
        store.set_meta('evolution_completed:'+version,value)
        store.event('factor_checkpoint_recovered',{'strategy':record['id'],'data_sha256':version})
        return
    recover_unpublished_factor_code(store,panel)


def recover_unpublished_factor_code(store,panel):
    """Recover only an undeployed generated candidate, never frozen trading code."""
    import ast
    from copy import deepcopy
    from hashlib import sha256
    from .config import ROOT
    from .files import atomic_write
    from .evolution import causal_checks
    from .pipeline import artifact,engine_hash
    version=panel.metadata['input_sha256']
    for record in store.strategies(['proposed']):
        spec=record['spec'];expression=spec.get('factor_expression')
        if not expression or spec.get('research_data_sha256')!=version or spec.get('engine_sha256')!=engine_hash(spec):continue
        path=ROOT/'research/evolved_factors'/('factor_'+record['id'][:12]+'.py')
        result_path=store.home/'artifacts'/('evolution-'+record['id'][:16]+'.json')
        if not path.is_file() or path.is_symlink() or result_path.exists():continue
        if any(o['strategy']==record['id'] for o in store.orders()):continue
        source=path.read_bytes();source_hash=sha256(source).hexdigest()
        template='"""Generated factor candidate; live eligibility requires fresh validation."""\nfrom quant_agent.evolution import expression_score\n\nFACTOR_EXPRESSION = '+repr(expression)+'\n\ndef score(fields, stock_mask=None):\n    return expression_score(FACTOR_EXPRESSION, fields, stock_mask)\n'
        try:
            actual=ast.parse(source);expected=ast.parse(template)
            def normalize(tree):
                assignments=[n for n in tree.body if isinstance(n,ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],ast.Name) and n.targets[0].id=='FACTOR_EXPRESSION']
                if len(assignments)!=1 or ast.literal_eval(assignments[0].value)!=expression:raise ValueError('Candidate expression differs')
                assignments[0].value=ast.Constant(None)
                return ast.dump(tree,include_attributes=False)
            if normalize(actual)!=normalize(expected):raise ValueError('Candidate module differs from generated template')
            checks=causal_checks(expression,panel)
        except (ValueError,SyntaxError,AssertionError):
            archive=store.home/'failed-candidate-code'/(record['id']+'-'+source_hash+'.py')
            atomic_write(archive,source)
            if path.read_bytes()!=source:raise ValueError('Candidate changed during recovery')
            path.unlink()
            store.event('partial_candidate_code_archived',{'strategy':record['id'],'source_sha256':source_hash,'deployed':False})
            continue
        value={'created_at':stamp(),'strategy':record['id'],'name':spec['factor_name'],'hypothesis':spec['hypothesis'],
               'falsification':spec['falsification'],'expression':expression,'code_path':str(path),'checks':checks,
               'source_sha256':source_hash,'status':'code_generated_causal_checks_passed','deployment':'recovered unpublished candidate; no running strategy changed'}
        artifact(store,'evolution-'+record['id'][:16],value);store.set_meta('evolution_completed:'+version,value)
        store.event('candidate_code_checkpoint_recovered',{'strategy':record['id'],'source_sha256':source_hash})
        return
