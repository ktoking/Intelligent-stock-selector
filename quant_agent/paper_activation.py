"""Explicit operator authorization to compare validated strategies in paper."""
from decimal import Decimal
import json

from .config import canonical
from .store import utcnow


def activate_batch(store, strategy_ids, authorization):
    if not store.settings.parallel_paper or not store.settings.paper_enabled or not authorization:
        raise ValueError('Explicit paper authorization and parallel flag required')
    ids=sorted(set(strategy_ids))
    if not ids:raise ValueError('No strategies selected')
    records={r['id']:r for r in store.strategies()}
    if any(k not in records or records[k]['stage']!='shadow' or not records[k]['evidence'].get('passed') for k in ids):
        raise ValueError('Only historically validated frozen shadow strategies may be selected')
    cents=int(Decimal(str(store.settings.stock_budget))*100)
    q,remainder=divmod(cents,len(ids))
    allocations={k:(q+(i<remainder))/100 for i,k in enumerate(ids)}
    with store.transaction() as db:
        if db.execute('SELECT 1 FROM orders LIMIT 1').fetchone() or db.execute('SELECT 1 FROM positions WHERE qty!=0 LIMIT 1').fetchone():
            raise ValueError('Cannot repartition an account with trading history')
        if db.execute("SELECT 1 FROM strategies WHERE stage IN ('paper','live_review') LIMIT 1").fetchone():
            raise ValueError('Cannot reallocate an active paper strategy')
        previous=db.execute("SELECT value FROM meta WHERE key='paper_allocations'").fetchone()
        option_allocations=json.loads(previous['value']).get('option',{}) if previous else {}
        store.set_meta('paper_allocations',{'stock':allocations,'option':option_allocations},db)
        stamp=utcnow()
        for k in ids:
            r=records[k]
            evidence={**r['evidence'],'paper_authorization':{'observed_at':stamp,'source':authorization,
                       'forward_wait_waived':True,'forward_validation_passed':False,'stock_allocation':allocations[k]}}
            if db.execute("UPDATE strategies SET stage='paper',evidence=? WHERE id=? AND stage='shadow'",(canonical(evidence),k)).rowcount!=1:
                raise ValueError('Concurrent strategy transition')
            store.event('stage',{'id':k,'from':'shadow','to':'paper','reason':'operator-authorized simulation; no forward PASS claimed'},db)
        store.set_meta('paper_authorization',{'observed_at':stamp,'strategy_count':len(ids),
            'forward_wait_waived':True,'stock_budget':store.settings.stock_budget,'option_budget':store.settings.option_budget,
            'source':authorization,'live_enabled':False},db)
    return allocations
