"""Explicit paper-only experiment policy; account and acknowledgement fences remain."""
import math

import pandas as pd

from .broker import QuoteUnavailable, executable_quote, session_now
from .store import utcnow


def risk_breach(store,reason):
    if not store.settings.continuous_paper:
        store.pause(reason,category='risk');return
    session=session_now() or 'closed'
    store.set_meta('latest_risk_breach',{'reason':reason,'observed_at':utcnow(),'action':'paper_warning_only'})
    if store.once('paper-risk-warning:'+session+':'+reason.split(':')[0]):
        store.event('paper_risk_warning',{'session':session,'reason':reason,'action':'continue_simulated_experiment'})


def resume_drawdown_pause(store,remote):
    """User-authorized migration only after an account/positions/fills reconciliation."""
    if not store.settings.continuous_paper or not store.meta('paused'):return False
    policy=store.meta('pause_policy',{});reason=store.meta('pause_reason') or ''
    if policy.get('category')!='risk' or not reason.startswith(('Risk breach:','Shared-account risk breach:')):return False
    if float(remote['cash']['balance'])<0 or any(o['status'] in ('unknown','submitting') for o in store.orders()):return False
    with store.transaction() as db:
        if store.meta('pause_policy')!=policy:return False
        store.set_meta('paused',False,db);store.set_meta('pause_reason',None,db)
        store.set_meta('pause_policy',{**policy,'recoverable':False,'resumed_at':utcnow(),'authorization':'2026-10-20 continuous simulated experiments'},db)
        store.event('paper_risk_pause_resumed',{'previous_reason':reason,'previous_policy':policy,'basis':'account reconciliation passed; user authorized continuous paper experiments'},db)
    return True


def stock_price(store,symbol,quote,side,reference=None,quote_fn=None):
    """A market reservation price is an estimate, never an executable limit."""
    try:
        bid,ask=(quote_fn or executable_quote)(quote,store.settings)
        return (math.floor(bid*100)/100 if side=='SELL' else math.ceil(ask*100)/100),1
    except QuoteUnavailable as exc:
        if not store.settings.stock_market_fallback or str(exc).startswith('Spread gate exceeded'):raise
    session=session_now()
    if not session:raise QuoteUnavailable('Market fallback outside regular session')
    price=float(reference or quote.get('last_price') or 0)
    # BUY references come from a separately freshness-checked closed minute
    # signal. A covered SELL may use the last known positive mark to journal
    # the intent; the broker supplies the actual market fill price.
    if not math.isfinite(price) or price<=0:raise QuoteUnavailable('Market reservation price unavailable')
    if side=='BUY' and not reference:raise QuoteUnavailable('Market BUY needs a fresh completed-bar reference')
    reserve=price*1.1 if side=='BUY' else price
    return round(reserve,2),3
