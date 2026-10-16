"""Component incidents and narrowly authorized recovery, never a blanket resume."""
from datetime import datetime,timezone
import json
from .observability import error_evidence,emit
from .store import utcnow


def failed(store,component,exc,*,scope='research'):
    key='component:'+component;old=store.meta(key,{})
    value={'component':component,'scope':scope,'status':'degraded','first_failed_at':old.get('first_failed_at',utcnow()),
           'last_failed_at':utcnow(),'failures':old.get('failures',0)+1,**error_evidence(exc)}
    store.set_meta(key,value);emit('component_failed',**value)
    return value


def recovered(store,component):
    key='component:'+component;old=store.meta(key)
    if old and old['status']!='ok':
        value={**old,'status':'ok','recovered_at':utcnow()};store.set_meta(key,value)
        store.event('component_recovered',{'component':component,'failures':old['failures']});emit('component_recovered',component=component)


def resume_confirmed_acknowledgement(store):
    if not store.meta('paused'):return False
    policy=store.meta('pause_policy',{})
    if not policy.get('recoverable') or policy.get('category') not in ('order_acknowledgement','cancellation'):return False
    orders=store.orders()
    if any(o['status'] in ('unknown','submitting') for o in orders):return False
    affected=set(policy.get('orders',[]));by_id={o['id']:o for o in orders}
    if not affected or not affected.issubset(by_id):return False
    if any(not by_id[key]['broker_id'] for key in affected):return False
    if policy['category']=='cancellation':
        if any(by_id[key]['status'] not in ('filled','cancelled','rejected') for key in affected):return False
    # Caller has just verified account identity, positions, cumulative fills and
    # cash risk limits. Only a confirmed acknowledgement may clear this pause.
    with store.transaction() as db:
        current=json.loads(db.execute("SELECT value FROM meta WHERE key='pause_policy'").fetchone()[0])
        if current!=policy:return False
        store.set_meta('paused',False,db);store.set_meta('pause_reason',None,db)
        store.set_meta('pause_policy',{**policy,'recoverable':False,'recovered_at':utcnow()},db)
        store.event('acknowledgement_recovered',{'orders':policy['orders'],'category':policy['category']},db)
    return True


def summary(store):
    with store.read() as db:
        rows=[json.loads(r[0]) for r in db.execute("SELECT value FROM meta WHERE key LIKE 'component:%'")]
    return rows
