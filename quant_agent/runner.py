"""Resident supervisor. Reconcile first; deterministic risk gates precede orders."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import math
from pathlib import Path
import time
import threading
import traceback
from decimal import Decimal

import exchange_calendars as xc
import pandas as pd

from research.daily_factor_lab.core import Policy, rank
from .panel_cache import load_panel
from research.daily_factor_lab.paper import data_prefix_hash
from research.daily_factor_lab.refresh import refresh
from .exchange_clock import us_calendar,latest_completed_session
from .ai import ask
from .broker import AccountIntegrityError, BrokerError, QuoteUnavailable, ReadUnavailable, SignalUnavailable, FutuSim, executable_quote, session_now
from .config import canonical, digest
from .engine import apply, empty, equity, plan
from .pipeline import artifact, engine_hash, evaluate, propose, shadow_record
from .store import utcnow
from .evolution import compile_strategy as compile_spec
from .dataset import frozen_view
from .schedule import activity, daily_due


def error_evidence(exc):
    from .observability import error_evidence as safe_evidence
    return safe_evidence(exc)


def research_step_version(store,job):
    directory=store.meta('current_data_dir',store.settings.data_dir)
    version=digest([directory,load_panel(directory).metadata['input_sha256']])
    if job=='promotion':
        from .account_selection import strict,version as selector_version
        if strict(store):version=digest([version,selector_version()])
    if store.meta('intraday_enabled',False):
        if job=='minute_research':
            from .intraday import engine_hash as minute_hash
            version=digest([version,'minute-research-wrapper-v1',minute_hash()])
        elif job not in ('discovery','backtest','evolution','factor_validation'):
            minute=store.meta('intraday_research') or {}
            version=digest([version,'5m',minute.get('data_sha256'),minute.get('engine_sha256')])
    return version


def frozen_review_input(store,asof,packet):
    """Retry the same evidence; live heartbeat timestamps cannot reset backoff."""
    key=digest(['review-input-v2',asof,research_step_version(store,'review'),store.settings.sha256])
    path=store.home/'artifacts'/('review-input-'+key+'.json')
    if not path.exists():artifact(store,'review-input-'+key,{'asof':asof,'packet':packet})
    return json.loads(path.read_text())['packet']


@contextmanager
def job_trace(store,job,asof):
    from .checkpoints import StepDeferred
    start=time.monotonic()
    directory=store.meta('current_data_dir',store.settings.data_dir)
    previous=store.meta('task:'+job,{})
    store.set_meta("task:"+job,{"status":"running","asof":asof,"data_dir":directory,"started_at":utcnow()})
    try:
        yield
    except StepDeferred as exc:
        saved=getattr(exc,'checkpoint_state',previous)
        store.set_meta('task:'+job,{**saved,'status':'waiting','asof':asof,'data_dir':directory,
            'reason':str(exc),'waiting_since':saved.get('waiting_since',utcnow())})
        raise
    except Exception as exc:
        store.set_meta("task:"+job,{"status":"failed","asof":asof,"data_dir":directory,"finished_at":utcnow(),
                                  "duration_seconds":round(time.monotonic()-start,2),**error_evidence(exc)})
        raise
    else:
        saved=store.meta('task:'+job,{})
        if not (saved.get('checkpoint') and saved.get('status')=='completed'):
            store.set_meta("task:"+job,{"status":"completed","asof":asof,"data_dir":store.meta('current_data_dir',store.settings.data_dir),"finished_at":utcnow(),
                                      "duration_seconds":round(time.monotonic()-start,2)})


@contextmanager
def executor_lock(store):
    with (store.home/"executor.lock").open("a") as file:
        try:
            fcntl.flock(file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another executor is already running") from None
        try:
            yield
        finally:
            fcntl.flock(file,fcntl.LOCK_UN)


def normalize_symbol(value):
    value = str(value)
    return value if value.startswith("US.") else "US."+value


def integer_quantity(value):
    number=Decimal(str(value))
    if not number.is_finite() or number<0 or number!=number.to_integral_value():
        raise BrokerError("Fractional or invalid broker quantity")
    return int(number)


def reconcile(store, broker):
    if hasattr(broker,'on_definite_rejection'):broker.on_definite_rejection=store.submission_failed
    remote = broker.verify() if not broker.verified else broker.state()
    bound = store.meta("bound_account")
    identity = digest([store.settings.stock_account,store.settings.stock_market])
    if bound and bound != identity:
        raise AccountIntegrityError("Account changed underneath persistent ledger")
    store.set_meta("bound_account",identity)
    rows = store.orders()
    active = [r for r in rows if r["status"] not in ("filled","cancelled","rejected")]
    history = []
    if active:
        begin = min(int(pd.Timestamp(r["created"]).timestamp()*1e6)-60_000_000 for r in active)
        history = broker.history(begin)
    remote_orders = {str(r["order_id"]):r for r in history+remote["orders"]}
    statuses = {2:"submitted",3:"partial",4:"filled",5:"cancelled",6:"rejected"}
    for row in active:
        match = remote_orders.get(row["broker_id"])
        if match is None:
            found = [r for r in remote_orders.values() if r.get("text") == "qa:"+row["id"]]
            if len(found)>1:
                raise AccountIntegrityError("Duplicate broker client marker")
            match = found[0] if found else None
        if row["status"] in ("submitting","unknown"):
            if match:
                store.submitted(row["id"],str(match["order_id"]))
            else:
                store.unknown(row["id"])
                continue
        if match:
            if (normalize_symbol(match["symbol"]) != row["symbol"] or integer_quantity(match["qty"]) != row["qty"] or
                    int(match["side"]) != (1 if row["side"] == "BUY" else 2)):
                raise AccountIntegrityError("Broker order identity differs from intent")
            if int(match["status"]) not in statuses:
                raise AccountIntegrityError("Unknown broker status; stop new execution")
            store.fill(row["id"],integer_quantity(match.get("cum_qty",0)),float(match.get("avg_fill_price") or 0),statuses[int(match["status"])])
        elif row["status"] in ("submitted","partial"):
            raise AccountIntegrityError("Acknowledged order absent from current and history queries")
    known = {r["broker_id"] for r in store.orders() if r["broker_id"]}
    foreign = [r for r in remote["orders"] if str(r["order_id"]) not in known and int(r["status"]) in (2,3)]
    if foreign:
        raise AccountIntegrityError("Unmanaged working order on simulated account")
    expected = {s:p["qty"] for bucket in ('stock','option') for s,p in store.portfolio(bucket)["positions"].items()}
    actual = {}
    for p in remote["positions"]:
        qty = float(p["qty"])
        if int(p.get("pstn_type",0)) != 0 or qty != int(qty):
            raise AccountIntegrityError("Short or fractional external position unsupported")
        if qty:
            actual[normalize_symbol(p["symbol"])] = int(qty)
    if actual != expected:
        raise AccountIntegrityError("Broker positions differ from allocated ledger; do not adopt external positions")
    from .capital import update_broker_capital
    update_broker_capital(store,remote)
    from .simulation_policy import resume_drawdown_pause
    resume_drawdown_pause(store,remote)
    return remote


def mark_portfolio(store, quotes, remote):
    with store.transaction() as db:
        for symbol,q in quotes.items():
            price = float(q["last_price"])
            if not math.isfinite(price) or price <= 0:
                raise BrokerError("Invalid mark")
            db.execute("UPDATE positions SET mark=? WHERE bucket='stock' AND symbol=?", (price,symbol))
            db.execute("UPDATE positions SET mark=? WHERE bucket='option' AND symbol=?", (price,symbol))
        from .holdings import record_marks
        record_marks(db,quotes,utcnow())
    p=store.portfolio()
    nav=p["cash"]+sum(v["qty"]*v["mark"] for v in p["positions"].values())
    peak=max(store.meta("peak_nav",store.settings.stock_budget),nav)
    store.set_meta("peak_nav",peak)
    session=session_now() or latest_completed_session()
    previous=store.meta("daily_nav",{"session":session,"nav":store.settings.stock_budget})
    if session!=previous['session']:
        previous={'session':session,'nav':store.meta('last_mark_stock_nav',previous['nav'])}
        store.set_meta('daily_nav',previous)
    dd=(1-nav/peak)*100
    loss=(1-nav/previous["nav"])*100
    if dd>=store.settings.max_drawdown_pct or loss>=store.settings.daily_loss_limit_pct:
        from .simulation_policy import risk_breach
        risk_breach(store,f"Risk breach: drawdown={dd:.2f}%, daily_loss={loss:.2f}%")
    store.set_meta('last_mark_stock_nav',nav)
    if float(remote["cash"]["balance"]) < 0:
        store.pause("Broker cash negative; no margin allowed",category='integrity')
    account_risk={}
    if store.settings.option_funding_mode=='shared_ratio':
        options=store.portfolio('option')
        total_nav=nav+options['cash']+sum(v['qty']*v['mark']*v['multiplier'] for v in options['positions'].values())
        total_budget=store.settings.stock_budget+store.settings.option_budget
        account_peak=max(store.meta('account_peak_nav',total_budget),total_nav)
        account_day=store.meta('account_daily_nav',{'session':session,'nav':total_budget})
        if session!=account_day['session']:
            account_day={'session':session,'nav':store.meta('last_mark_account_nav',account_day['nav'])}
            store.set_meta('account_daily_nav',account_day)
        account_dd=(1-total_nav/account_peak)*100
        account_loss=(1-total_nav/account_day['nav'])*100
        store.set_meta('account_peak_nav',account_peak)
        store.set_meta('last_mark_account_nav',total_nav)
        if account_dd>=store.settings.max_drawdown_pct or account_loss>=store.settings.daily_loss_limit_pct:
            from .simulation_policy import risk_breach
            risk_breach(store,f"Shared-account risk breach: drawdown={account_dd:.2f}%, daily_loss={account_loss:.2f}%")
        account_risk={'account_nav':total_nav,'account_drawdown_pct':account_dd,'account_daily_loss_pct':account_loss}
    with store.transaction() as db:
        accounting=store.attributed_portfolio('stock',db)
        realized=accounting['realized_pnl']
        unrealized=sum(v['qty']*v['mark']*v['multiplier']-v['basis'] for v in accounting['positions'].values())
        db.execute('INSERT OR IGNORE INTO paper_nav VALUES (?,?,?,?,?,?)',
                   (utcnow(),session_now() or session,nav,p['cash'],unrealized,realized))
        if store.settings.option_paper_enabled:
            options=store.attributed_portfolio('option',db)
            option_realized=options['realized_pnl']
            option_value=sum(p['qty']*p['mark']*p['multiplier'] for p in options['positions'].values())
            option_unrealized=sum(p['qty']*p['mark']*p['multiplier']-p['basis'] for p in options['positions'].values())
            db.execute('INSERT OR IGNORE INTO option_paper_nav VALUES (?,?,?,?,?,?)',
                (utcnow(),session_now() or session,options['cash']+option_value,options['cash'],option_unrealized,option_realized))
        if store.settings.parallel_paper:
            owners=list(db.execute("SELECT id FROM strategies WHERE stage IN ('paper','live_review')"))
            observed=utcnow()
            for row in owners:
                owner=store.strategy_portfolio(row['id'],db=db)
                db.execute('INSERT OR IGNORE INTO strategy_paper_nav VALUES (?,?,?,?,?,?,?,?)',
                    (row['id'],observed,session_now() or session,owner['nav'],owner['cash'],owner['unrealized_pnl'],owner['realized_pnl'],owner['budget']))
    return {"nav":nav,"drawdown_pct":dd,"daily_loss_pct":loss,**account_risk}


def monitor(store, quotes):
    for symbol,q in quotes.items():
        try:previous=float(q.get("prev_close_price") or 0)
        except (ValueError,TypeError):continue
        if previous<=0:
            continue
        change=(float(q["last_price"])/previous-1)*100
        if abs(change)>=3:
            key="anomaly:"+symbol+":"+str(q.get("data_date"))+":"+("up" if change>0 else "down")
            if store.once(key):
                store.event("price_anomaly",{"symbol":symbol,"change_pct":change,"source":"Futunn REST","data_date":q.get("data_date")})


def execution_view(store,record,source_panel):
    evidence=record['evidence'];panel=None
    try:panel=frozen_view(source_panel,record)
    except ValueError:pass  # A newer Shadow can have additional frozen members.
    if panel is not None and data_prefix_hash(panel,evidence['seed_asof'])==evidence['seed_hash']:return panel
    # New research/Shadow versions may use the revised vendor history while
    # already running paper versions retain their independent observed prefix.
    candidates=[store.meta('execution_cache:'+record['id']),store.meta('current_data_dir')]
    for directory in dict.fromkeys(c for c in candidates if c):
        try:alternative=frozen_view(load_panel(directory),record)
        except ValueError:continue
        if data_prefix_hash(alternative,evidence['seed_asof'])==evidence['seed_hash']:return alternative
    if panel is None:raise BrokerError('No preserved execution dataset covers frozen strategy members')
    return panel  # checked_panel will retain the strict freeze failure.


def checked_panel(store,record,source_panel=None):
    if record['spec'].get('execution_timeframe')=='5m':
        from .intraday import verify
        return verify(record)
    data_dir=store.meta("execution_data_dir",store.meta("current_data_dir",store.settings.data_dir))
    panel=execution_view(store,record,source_panel if source_panel is not None else load_panel(data_dir))
    evidence=record["evidence"]
    if engine_hash(record['spec'])!=record["spec"]["engine_sha256"]:
        raise BrokerError("Frozen strategy engine changed")
    if data_prefix_hash(panel,evidence["seed_asof"])!=evidence["seed_hash"]:
        raise BrokerError("Historical data revised since freeze; corporate-action review required")
    if panel.metadata["end"] != latest_completed_session():
        raise SignalUnavailable("Signal cache not at latest completed exchange session")
    return panel


def quote_prices(quotes,settings,symbols=None,*,check_spread=True):
    prices={}
    for s in sorted(set(quotes) if symbols is None else set(symbols)):
        if s not in quotes:
            raise QuoteUnavailable('Required quote missing: '+s)
        try:
            prices[s]=sum(executable_quote(quotes[s],settings,check_spread=check_spread))/2
        except QuoteUnavailable as exc:
            raise QuoteUnavailable(s+': '+str(exc)) from None
    return prices


def execution_symbols(panel,policy,holdings):
    # rank_buffer retains existing holdings; new entries only use positions ranks.
    return set(holdings)|{panel.symbols[j] for j in rank(panel,len(panel.dates)-1,policy)[:policy.positions]}


def shadow_tick(store,record,quotes,session,source_panel=None):
    key="shadow_pending:"+record["id"]
    pending=store.meta(key)
    if pending and session!=pending["session"]:
        day=pending["session"]
        calendar=us_calendar()
        now=pd.Timestamp.now(tz="UTC")
        if now >= calendar.session_close(day):
            with store.transaction() as db:
                sealed=db.execute('SELECT state,data_hash FROM shadow WHERE strategy=? AND session=?',(record['id'],day)).fetchone()
                if sealed:
                    saved=json.loads(sealed['state'])
                    if sealed['data_hash']!=pending['data_hash'] or saved['holdings']!=pending['state']['holdings'] or saved['cash']!=pending['state']['cash']:
                        raise BrokerError('Persisted Shadow seal conflicts with pending observation')
                    store.set_meta('shadow_state:'+record['id'],saved,db)
                    store.set_meta(key,None,db)
                    store.event('shadow_seal_recovered',{'strategy':record['id'],'session':day},db)
            if sealed:
                pending=None
            elif now>calendar.session_close(day)+pd.Timedelta(days=1):
                # An unobserved close cannot be manufactured from later bars.
                # Archive this failed Shadow experiment, so it cannot keep the
                # whole supervisor polling forever during market closures.
                reason='Shadow closing observation expired; no historical backfill or forward credit'
                with store.transaction() as db:
                    store.set_meta('shadow_expired:'+record['id']+':'+day,pending,db)
                    store.set_meta(key,None,db)
                    db.execute("UPDATE strategies SET stage='rejected',evidence=? WHERE id=? AND stage='shadow'",
                               (canonical({**record['evidence'],'reason':reason}),record['id']))
                    store.event('shadow_observation_expired',{'strategy':record['id'],'session':day,'forward_credit':False},db)
                return {'status':'expired','reason':reason}
            else:
                state=pending["state"]
                held=state["holdings"]
                marks={}
                for s in held:
                    q=quotes.get(s)
                    if not q or str(q.get("data_date"))!=day:
                        raise QuoteUnavailable("Cannot seal shadow without observed same-session closing marks")
                    marks[s]=float(q["last_price"])
                    state["peaks"][s]=max(state["peaks"].get(s,0),marks[s])
                state["nav"]=equity(state,marks)
                state["order_count"]=len(pending["orders"])
                shadow_record(store,record,day,state,pending["data_hash"])
                with store.transaction() as db:
                    store.set_meta("shadow_state:"+record["id"],state,db)
                    store.set_meta(key,None,db)
                pending=None
    if not session or pending:
        return {'status':'closed' if not session else 'observing'}
    calendar=us_calendar()
    if calendar.session_open(session)<=pd.Timestamp(record['evidence']['shadow_started_at']):
        return {'status':'waiting_next_session'}
    if store.meta('shadow_last_plan:'+record['id'])==session:
        return {'status':'observing'}
    # Freeze a causal plan at an observed quote in first 5 minutes, not hindsight open.
    if pd.Timestamp.now(tz="UTC")>calendar.session_open(session)+pd.Timedelta(minutes=5):
        previous=store.meta('shadow_outcome:'+session+':'+record['id'],{})
        outcome={**previous,'status':'missed_window','reason':previous.get('reason','五分钟观察窗口结束，未取得可验证的 Shadow 计划。')}
        store.set_meta('shadow_outcome:'+session+':'+record['id'],outcome)
        if store.once('shadow_window_missed:'+session+':'+record['id']):
            store.event('shadow_window_missed',{'strategy':record['id'],'session':session,**outcome})
        return outcome
    panel=checked_panel(store,record,source_panel)
    panel,policy=compile_spec(panel,record["spec"])
    state=store.meta("shadow_state:"+record["id"],empty())
    required=execution_symbols(panel,policy,state['holdings'])
    prices=quote_prices(quotes,store.settings,required)
    i=len(panel.dates)-1
    # Snapshot uses raw shares, while research cache is autype=1. Do not splice a split/dividend basis.
    for s in required:
        if s not in prices:
            raise BrokerError("Ranked symbol quote missing")
        close=panel.fields["close"][i,panel.symbols.index(s)]
        prev=float(quotes[s].get("prev_close_price",0))
        if not prev or abs(prev/close-1)>.005:
            raise QuoteUnavailable(s+': adjusted/raw share basis discrepancy')
    state=store.meta("shadow_state:"+record["id"],empty())
    orders,controls=plan(panel,i,policy,state,session,prices)
    state=apply(state,orders,controls)
    with store.transaction() as db:
        store.set_meta(key,{"session":session,"state":state,"orders":orders,"data_hash":panel.metadata["input_sha256"],"observed":utcnow()},db)
        store.set_meta("shadow_last_plan:"+record["id"],session,db)
        store.event("shadow_observed_plan",{"strategy":record["id"],"session":session,"orders":orders,"fill_model":"observed quote midpoint +25bps, hypothetical; not broker fills"},db)
    return {'status':'observing'}


def paper_tick(store,broker,record,quotes,session,source_panel=None):
    if not session or store.meta("paused") or not store.settings.paper_enabled:
        return
    from .account_selection import strict as strict_selection
    if store.meta('intraday_enabled',False) and not strict_selection(store) and record['spec'].get('execution_timeframe')!='5m':
        return {'status':'legacy_hold','reason':'日线版本停止新增调仓；持仓按账户换仓流程交接，未重写历史规则。'}
    from .champion import enabled,pending
    if enabled(store) and (pending(store) or store.meta('account_champion')!=record['id']):
        return {'status':'rotating','reason':'账户换仓或策略尚未获得账户交易权限'}
    calendar=us_calendar()
    if pd.Timestamp.now(tz="UTC")>calendar.session_open(session)+pd.Timedelta(minutes=5):
        suffix=':'+record['id'] if store.settings.parallel_paper else ''
        outcome=store.meta('paper_outcome:'+session+suffix)
        if outcome is None or outcome.get('status')=='partial':
            placed=[o for o in store.orders() if o['strategy']==record['id'] and o['session']==session and o['broker_id']]
            outcome={**(outcome or {}),'status':'missed_window','session':session,'strategy':record['id'],
                     'submitted_orders':len(placed),'filled_orders':sum(o['status']=='filled' for o in placed),
                     'detail':'五分钟入场窗口结束，部分或全部订单未执行；本日不追单。','observed_at':utcnow()}
            store.set_meta('paper_outcome:'+session+suffix,outcome)
            store.event('paper_window_missed',outcome)
        return outcome
    panel=checked_panel(store,record,source_panel)
    suffix=':'+record['id'] if store.settings.parallel_paper else ''
    key="paper_plan:"+session+suffix
    pending=store.meta(key)
    if pending is None:
        panel,policy=compile_spec(panel,record["spec"])
        portfolio=store.strategy_portfolio(record['id']) if store.settings.parallel_paper else store.portfolio()
        budget=portfolio.get('budget',store.settings.stock_budget)
        if store.settings.capital_mode=='broker_equity':
            from .capital import stock_cap
            budget=stock_cap(store)
        state=store.meta("paper_controls"+suffix,empty(budget))
        state.update(cash=portfolio["cash"],holdings={s:p["qty"] for s,p in portfolio["positions"].items()})
        required=execution_symbols(panel,policy,state['holdings'])
        # A wide market can price a frozen plan, but cannot authorize an order.
        # Enforce the spread separately for each intent so other names can run.
        prices=quote_prices(quotes,store.settings,required,check_spread=False)
        for s in prices:
            if s in panel.symbols:
                close=panel.fields["close"][-1,panel.symbols.index(s)]
                try:prev=float(quotes[s].get("prev_close_price") or 0)
                except (ValueError,TypeError):raise QuoteUnavailable(s+': previous-close reference invalid') from None
                if not math.isfinite(prev) or prev<=0 or not math.isfinite(close) or abs(prev/close-1)>.005:
                    raise QuoteUnavailable(s+": raw/adjusted basis discrepancy; defer this strategy")
        # A new funding mandate needs one allocation decision, while preserving
        # the incumbent's peaks and remaining cooldown periods.
        force=store.meta('paper_rebalance_required:'+record['id'],False)
        offset=state['sessions'] if force else 0
        if force:
            state['sessions']=0
            state['blocked']={s:max(0,v-offset) for s,v in state.get('blocked',{}).items()}
        orders,controls=plan(panel,len(panel.dates)-1,policy,state,session,prices,store.settings.fee_reserve_bps,capital_cap=budget)
        if force:
            controls['sessions']+=offset
            controls['blocked']={s:v+offset for s,v in controls.get('blocked',{}).items()}
        pending={"strategy":record["id"],"orders":orders,"controls":controls}
        with store.transaction() as db:
            store.set_meta(key,pending,db)
            store.set_meta("paper_controls"+suffix,controls,db)
            if force:store.set_meta('paper_rebalance_required:'+record['id'],False,db)
            store.event("paper_plan",pending,db)
    deferred=[]
    for number,o in enumerate(pending["orders"]):
        # Freeze a price for every intent; never change it on retry.
        order_id=digest([record["id"],session,number,o["symbol"],o["side"],o["qty"]])[:32]
        existing=next((r for r in store.orders() if r["id"]==order_id),None)
        if existing:
            if existing["status"] != "reserved":
                continue
        try:
            if o['symbol'] not in quotes:raise QuoteUnavailable('Required quote missing')
            bid,ask=executable_quote(quotes[o["symbol"]],store.settings)
        except QuoteUnavailable as exc:
            item={'symbol':o['symbol'],'side':o['side'],'reason':str(exc),'id':order_id}
            deferred.append(item)
            store.event('paper_order_quote_deferred',{'strategy':record['id'],'session':session,**item})
            continue
        if not existing:
            price=(math.ceil(ask*100)/100 if o["side"]=="BUY" else math.floor(bid*100)/100)
            if abs(price/o["raw_price"]-1)>.005:
                store.event("order_skipped_price_drift",{"id":order_id})
                deferred.append({'symbol':o['symbol'],'id':order_id,'reason':'Frozen intent price drift exceeds 0.5%'})
                continue
            try:
                store.reserve(order_id,record["id"],"stock",o["symbol"],o["side"],o["qty"],price,session)
            except ValueError:
                # Sales must fill and settle; no anticipated proceeds/margin.
                if store.once("defer:"+order_id):
                    store.event("order_deferred_risk",{"id":order_id,"symbol":o["symbol"]})
                deferred.append({'symbol':o['symbol'],'id':order_id,'reason':'Cash, allocation or settlement gate blocked reservation'})
                continue
            existing=next(r for r in store.orders() if r["id"]==order_id)
        if store.claim(order_id):
            try:
                broker_id=broker.submit(existing)
                store.submitted(order_id,broker_id)
            except Exception:
                store.unknown(order_id)
                raise
    outcome={'status':'partial' if deferred else 'checked','session':session,'strategy':record['id'],
             'planned_orders':len(pending['orders']),'deferred_orders':deferred,'observed_at':utcnow()}
    store.set_meta('paper_outcome:'+session+suffix,outcome)
    return outcome


def expire_orders(store,broker,session):
    calendar=us_calendar()
    for row in store.orders(active=True):
        deadline=(pd.Timestamp(row['created'])+pd.Timedelta(minutes=2) if row['bucket']=='option' or store.meta('rotation_order:'+row['id']) or store.meta('minute_order:'+row['id'])
                  else calendar.session_open(row["session"])+pd.Timedelta(minutes=5))
        if pd.Timestamp.now(tz="UTC")<=deadline:
            continue
        if row["status"]=="reserved":
            store.fill(row["id"],0,0,"cancelled")
        elif row["status"] in ("submitted","partial") and row["broker_id"]:
            key='cancel_attempts:'+row['id'];attempts=store.meta(key,{'count':0})
            last=attempts.get('last_at')
            if attempts['count']>=3 and last and pd.Timestamp.now(tz='UTC')>=pd.Timestamp(last)+pd.Timedelta(minutes=5):
                # tick() has just read back this same working order. Cancellation
                # of its verified ID may resume after cooldown; never resubmit
                # the original order or assume that a cancel acknowledgement filled it.
                attempts={'count':0,'round':attempts.get('round',0)+1}
                last=None
                store.event('cancel_retry_window_reopened',{'id':row['id'],'round':attempts['round']})
            if attempts['count']<3 and (not last or pd.Timestamp.now(tz='UTC')>=pd.Timestamp(last)+pd.Timedelta(seconds=store.settings.poll_seconds)):
                store.set_meta(key,{'count':attempts['count']+1,'round':attempts.get('round',0),'last_at':pd.Timestamp.now(tz='UTC').isoformat()})
                try:
                    broker.cancel(row["broker_id"])
                    store.event("cancel_requested",{"id":row["id"]})
                except Exception as exc:
                    store.event('cancel_uncertain',{'id':row['id'],**error_evidence(exc)})
                    if not store.meta('paused'):store.pause("Cancellation uncertain or rejected; reconcile working order",category="cancellation",recoverable=True,orders=[row["id"]])
            elif attempts['count']>=3 and store.once('cancel_cap:'+row['id']):
                store.event('cancel_retry_cap',{'id':row['id'],'attempts':3})
                if not store.meta('paused'):store.pause('Cancellation retry cap reached; reconcile working order',category='cancellation',recoverable=True,orders=[row['id']])


def daily_jobs(store,quotes):
    from .checkpoints import workflow_lock
    with workflow_lock(store):return _daily_jobs(store,quotes)


def _daily_jobs(store,quotes):
    from .checkpoints import run_step,recover_factor_result,StepDeferred
    def step(job,action,context=None):
        version=research_step_version(store,job)
        if context is not None:version=digest([version,context])
        return run_step(store,job,asof,version,action)
    asof=latest_completed_session()
    from .research_evidence import research_pending
    from .market_data import finalization_due
    finalize=finalization_due(store)
    if store.meta("daily_completed:"+asof) and not research_pending(store,asof) and not finalize:
        return
    retry=store.meta("daily_retry_after:"+asof)
    if retry and pd.Timestamp.now(tz="UTC")<pd.Timestamp(retry):
        return
    jobs_ok=True
    if store.settings.auto_refresh:
        try:
            from .market_data import refresh_cache
            with job_trace(store,"refresh",asof):
                source=store.meta('current_data_dir',store.settings.data_dir)
                refresh_version=digest(['refresh-v2',asof,source,finalize,load_panel(source).metadata['input_sha256']])
                run_step(store,'refresh',asof,refresh_version,lambda:refresh_cache(store,recheck=finalize))
            from .recovery import recovered
            recovered(store,'daily_refresh')
        except Exception as exc:
            jobs_ok=False
            from .recovery import failed
            failed(store,'daily_refresh',exc)
            store.event("refresh_failed",{**error_evidence(exc),"asof":asof})
    if not jobs_ok:
        store.set_meta('task:review',{'status':'blocked','asof':asof,'reason':'历史数据未通过校验，等待刷新重试；尚未调用AI。'})
        store.set_meta('daily_completed:'+asof,False)
        state=store.meta('task:refresh',{})
        store.set_meta('daily_retry_after:'+asof,state.get('retry_after') or (pd.Timestamp.now(tz='UTC')+pd.Timedelta(minutes=1)).isoformat())
        return
    if jobs_ok and not store.meta("universe_scanned:"+asof):
        try:
            from .universe import scan,enrich
            with job_trace(store,"scan",asof):
                found=scan(store)
                enrichment=enrich(store,found)
            store.event("daily_universe_intake",enrichment)
            store.set_meta("universe_scanned:"+asof,True)
        except Exception as exc:
            jobs_ok=False
            store.event("universe_intake_failed",{"error_type":type(exc).__name__})
    from .news import refresh_news,evidence_at
    if not store.meta('news_completed:'+asof):
        try:
            with job_trace(store,'news',asof):refresh_news(store)
            store.set_meta('news_completed:'+asof,True)
        except Exception as exc:
            jobs_ok=False;store.event('news_failed',error_evidence(exc))
    minute_mode=store.meta('intraday_enabled',False)
    daily_lane_ok=True
    if minute_mode:
        try:
            from .intraday import research as minute_research
            with job_trace(store,'minute_research',asof):step('minute_research',lambda:minute_research(store,locked=True))
        except StepDeferred:
            jobs_ok=False
        except Exception as exc:
            jobs_ok=False
            store.event('minute_research_failed',error_evidence(exc))
    if store.settings.daily_research:
        data_hash=load_panel(store.meta("current_data_dir",store.settings.data_dir)).metadata["input_sha256"]
        if not store.meta("research_completed:"+data_hash) or not store.meta('discovery_completed:'+data_hash):
            cycle_trials=sum(r['spec'].get('research_data_sha256')==data_hash for r in store.strategies())
            if cycle_trials>=store.settings.max_trials:
                if store.once("trial_cap:"+data_hash):
                    store.event("research_trial_cap",{"max_trials":store.settings.max_trials,"data_hash":data_hash})
            elif not store.meta('discovery_completed:'+data_hash):
                # A batch can reach the cap halfway through. Keep its registered
                # candidates, evaluate them, and still produce the daily review.
                try:
                    with job_trace(store,"discovery",asof):
                        propose(store,names=["trend_buffer15","momentum_blend20","residual_momentum20"])
                        if sum(r['spec'].get('research_data_sha256')==data_hash for r in store.strategies())<store.settings.max_trials:
                            step('discovery',lambda:propose(store,ai=True))
                        store.set_meta('discovery_completed:'+data_hash,True)
                except StepDeferred:
                    jobs_ok=False;daily_lane_ok=False
                except Exception as exc:
                    jobs_ok=False;daily_lane_ok=False
                    store.event("research_failed",{"error_type":type(exc).__name__})
            # Versioned, fixed control strategies are rechecked on revised data
            # even when AI makes no new proposal or is temporarily unavailable.
            try:
                with job_trace(store,"backtest",asof):
                    step('backtest',lambda:evaluate(store))
                store.set_meta("research_completed:"+data_hash,True)
            except StepDeferred:
                jobs_ok=False;daily_lane_ok=False
            except Exception as exc:
                jobs_ok=False;daily_lane_ok=False
                store.event("validation_failed",{"error_type":type(exc).__name__})
    from .briefing import review_packet, REVIEW_SCHEMA, REVIEW_SYSTEM, validate_brief, persist_brief
    packet=review_packet(store,quotes,panel=load_panel(store.meta("current_data_dir",store.settings.data_dir)))
    if minute_mode:
        from .intraday import view as minute_view
        packet['minute_research']=minute_view(store)
        packet['research_timeframes']=['1d','5m']
        packet['protocol_gates']={'timeframe':'5m','cost_bps':store.settings.fee_reserve_bps,'stress_bps':50,
            'baseline_signal_lag_bars':1,'stress_signal_lag_bars':2,'max_drawdown_pct':store.settings.max_drawdown_pct,
            'min_test_sessions':20,'min_test_orders':10,'benchmark':'QQQ on identical test dates',
            'note':'日线与分钟研究同时运行，各用独立数据、规则和验证。日线指标以交易日计，分钟指标以完整5分钟K计；不得混用收益、信号或因果证据。账户交接仍由统一执行流程管理。'}
    packet=frozen_review_input(store,asof,packet)
    response=None
    # Observation metadata must not make identical evidence miss its cache.
    name="daily-"+asof+"-"+digest({k:v for k,v in packet.items() if k!="created"})[:12]
    saved=store.home/"artifacts"/(name+".json")
    if saved.exists():
        response=json.loads(saved.read_text())["ai"]
    if response is None:
        try:
            def review_action():
                value=ask(store.settings.ai_provider,{"purpose":"daily_review","asof":asof,"evidence":packet},
                          model_name=store.settings.ai_model,reasoning_effort=store.settings.ai_reasoning_effort,
                          output_schema=REVIEW_SCHEMA,system_instruction=REVIEW_SYSTEM)
                validate_brief(value,packet)
                return {'response':value,'packet':packet}
            with job_trace(store,"review",asof):
                completed_review=step('review',review_action,'stable-evidence-v2')
                response=completed_review['response'];packet=completed_review['packet']
                validate_brief(response,packet)
            store.event("ai_review",response)
            store.set_meta('ai_runtime',{'model':response['model'],'reasoning_effort':response.get('reasoning_effort'),'observed_at':utcnow()})
        except StepDeferred:
            response=None
        except Exception as exc:
            response=None
            store.event("ai_unavailable",{"job":"daily_review",**error_evidence(exc)})
    jobs_ok=jobs_ok and response is not None
    directory=store.home/"reports"; directory.mkdir(exist_ok=True,mode=0o700)
    summary=["# AI 量化每日复盘 "+asof,"", f"股票账本本金 ${store.settings.stock_budget:,.0f}；期权账本本金 ${store.settings.option_budget:,.0f}。实际执行额度以最新净资产与现金校验为准。实盘执行：关闭。",
             "", "行情来源 Futunn REST；时间依据 data_date，不把周末缓存叫实时。",
             "", '复盘生成时间：'+pd.Timestamp.now(tz='Asia/Shanghai').isoformat()+'。行情截至上述已收盘交易日，资讯按首次观察时间可用。',
             "", "策略阶段：",*['- '+r['spec'].get('factor_name',r['spec']['policy']['name'])+': '+r['stage'] for r in packet['strategies']],
             "", f"订单累计 {len(packet['orders'])}；暂停={packet['paused']}","", "## AI 复盘",
             response["result"]["review"] if response else "AI 当前不可用；执行和风控仍由确定性程序控制。",
             "", "## 因子待办",*(["- "+s for s in response["result"]["factor_ideas"]] if response else []),
             "", f"资讯：{len(packet['news'])}条带时间戳标题，首次观察后可用于分析；尚不含全文核实或历史新闻因子。期权模拟执行{'已授权，实际状态见账本' if store.settings.option_paper_enabled else '关闭'}。"]
    path=directory/(asof+".md")
    if response is None and path.exists():
        # Retain a successful earlier-data report. Failure evidence belongs to
        # its own attempt and task state, never silently replaces valid analysis.
        path=directory/(name+"-failed.md")
    from .files import atomic_write
    atomic_write(path,"\n".join(summary))
    if response is not None:
        persist_brief(store,response,packet,asof)
    if not saved.exists():
        artifact(store,name,{"packet":packet,"ai":response})
    elif response is not None and json.loads(saved.read_text())["ai"] is None:
        # Preserve the failed attempt while saving the successful retry.
        retry_name=name+"-retry-"+digest(response)[:12]
        if not (store.home/"artifacts"/(retry_name+".json")).exists():
            artifact(store,retry_name,{"packet":packet,"ai":response})
    cycle_trials=sum(r['spec'].get('research_data_sha256')==packet['data']['input_sha256'] for r in store.strategies())
    if store.settings.evolution_enabled and (cycle_trials<store.settings.max_trials or store.meta('evolution_completed:'+packet['data']['input_sha256'])):
        try:
            from .evolution import evolve
            with job_trace(store,'evolution',asof):
                recover_factor_result(store)
                result=step('evolution',lambda:evolve(store,response['result']['factor_ideas'] if response else []))
                step('factor_validation',lambda:evaluate(store))
            record=next(r for r in store.strategies() if r['id']==result['strategy'])
            code_state='已生成独立代码并验证。' if result.get('code_path') else '候选被结构或因果检查拦截。'
            atomic_write(path,path.read_text()+'\n\n## 新因子代码与验证\n'+result['name']+'：'+record['stage']+'。'+code_state+'\n'+result['hypothesis'])
        except StepDeferred:
            jobs_ok=False;daily_lane_ok=False
        except Exception as exc:
            jobs_ok=False;daily_lane_ok=False
            store.event('evolution_failed',{'error_type':type(exc).__name__})
    elif store.settings.evolution_enabled and cycle_trials>=store.settings.max_trials:
        store.set_meta('task:evolution',{'status':'blocked','asof':asof,'reason':'当前数据版本达到试验上限；保留既有候选，不新增公式。'})
    if (store.settings.daily_research or store.settings.evolution_enabled) and daily_lane_ok:
        from .research_evidence import daily_cycle_version
        store.set_meta('daily_research_cycle:'+asof,daily_cycle_version(store))
    if store.settings.evolution_enabled and minute_mode:
        try:
            from .minute_factors import evolve as minute_evolve
            with job_trace(store,'minute_factor_research',asof):
                factor=step('minute_evolution',lambda:minute_evolve(store,response['result']['factor_ideas'] if response else []))
            atomic_write(path,path.read_text()+'\n\n## 新分钟因子：代码与独立验证\n'+factor['name']+'：'+factor['status']+'。\n'+factor['hypothesis']+'\n\n'+canonical(factor.get('stats',{})))
        except StepDeferred:
            jobs_ok=False
        except Exception as exc:
            jobs_ok=False
            store.event('minute_evolution_failed',error_evidence(exc))
    from .account_selection import strict as strict_selection
    if strict_selection(store) and daily_lane_ok:
        try:
            from .champion import screen
            with job_trace(store,'promotion',asof):
                step('promotion',lambda:screen(store,load_panel(store.meta('current_data_dir',store.settings.data_dir))),
                    ['strict-common-v1',store.meta('promotion_policy')])
        except StepDeferred:
            jobs_ok=False
        except Exception as exc:
            jobs_ok=False
            store.event('unified_selection_failed',error_evidence(exc))
    elif jobs_ok and store.settings.daily_research and not minute_mode:
        from .promotion import plan_promotions
        with job_trace(store,'promotion',asof):
            step('promotion',lambda:plan_promotions(store,load_panel(store.meta('current_data_dir',store.settings.data_dir))))
    if jobs_ok:
        store.set_meta('research_cycle_completed:'+asof,store.meta('current_data_dir',store.settings.data_dir))
        from .research_evidence import append_research_review
        append_research_review(store,asof)
    store.set_meta("daily_completed:"+asof,jobs_ok)
    if not jobs_ok:
        # Superseded review checkpoints stay in the audit trail. Only the
        # current task states may schedule another attempt.
        states=[store.meta('task:'+job,{}) for job in ('refresh','discovery','backtest','review','evolution',
            'factor_validation','minute_research','minute_evolution','minute_factor_proposal','minute_factor_validation','promotion')]
        retries=[r['retry_after'] for r in states if r.get('asof')==asof and r.get('status') in ('failed','waiting')
            and r.get('retry_after') and r['retry_after']>utcnow()]
        store.set_meta("daily_retry_after:"+asof,min(retries) if retries else (pd.Timestamp.now(tz="UTC")+pd.Timedelta(minutes=1)).isoformat())
    else:store.set_meta("daily_retry_after:"+asof,None)


def tick(store,broker,run_daily=True):
    remote=reconcile(store,broker)
    # Cancellation/recovery must run even when the quote service is unavailable.
    session=session_now()
    expire_orders(store,broker,session)
    symbols={"US.NVDA","US.QQQ","US.SPY"}|set(store.portfolio()["positions"])|set(store.portfolio('option')['positions'])
    records=store.strategies(["shadow","paper"])
    try:source_panel=load_panel(store.meta("execution_data_dir",store.meta("current_data_dir",store.settings.data_dir)))
    except (ValueError,OSError,EOFError) as exc:
        if not store.settings.continuous_paper and not any(r['stage']=='paper' and r['spec'].get('execution_timeframe')=='5m' for r in records):raise
        source_panel=None
        from .recovery import failed
        failed(store,'legacy_daily_data',exc,scope='daily')
    option_panel=source_panel
    if store.settings.option_paper_enabled:
        try:
            if store.meta('option_data_blocked'):option_panel=None
            elif store.meta('option_data_dir'):option_panel=load_panel(store.meta('option_data_dir'))
        except (ValueError,OSError,EOFError) as exc:
            option_panel=None
            from .recovery import failed
            failed(store,'option_data',exc,scope='option')
    preparation_errors={}
    for r in records:
        if r['spec'].get('execution_timeframe')=='5m':
            symbols.update(r['evidence']['symbols'])
            continue
        if store.meta('intraday_enabled',False) and r['stage']=='shadow' and not store.meta('shadow_pending:'+r['id']):
            continue
        state=store.meta("shadow_state:"+r["id"],empty())
        pending=store.meta("shadow_pending:"+r["id"])
        symbols.update(state["holdings"])
        if pending:
            symbols.update(pending["state"]["holdings"])
        try:
            if source_panel is None:raise SignalUnavailable('Daily cache unavailable; new daily plans blocked, account exits remain independent')
            panel=execution_view(store,r,source_panel)
            panel,policy=compile_spec(panel,r["spec"])
            symbols.update(panel.symbols[j] for j in rank(panel,len(panel.dates)-1,policy)[:r["spec"]["policy"]["rank_buffer"]])
        except (BrokerError,ValueError,KeyError,TypeError) as exc:
            from .recovery import failed
            failed(store,'strategy:'+r['id'],exc,scope='strategy')
            preparation_errors[r['id']]=error_evidence(exc)
    quotes={};invalid=[]
    for q in broker.snapshots(sorted(symbols)):
        try:
            code=q['code'];price=float(q.get('last_price') or 0)
            if code not in symbols or not math.isfinite(price) or price<=0:raise ValueError('Invalid snapshot')
            if code in quotes:
                invalid.append(code);continue
            stamp=q.get('update_time')
            if not isinstance(stamp,(str,int,float)) or isinstance(stamp,(int,float)) and not math.isfinite(stamp):stamp=None
            quotes[code]={**q,'last_price':price,'update_time':stamp,'data_date':q.get('data_date') if isinstance(q.get('data_date'),str) else None}
        except (KeyError,ValueError,TypeError):invalid.append(q.get('code') if isinstance(q,dict) else None)
    for code in invalid:quotes.pop(code,None)
    missing=sorted(symbols-set(quotes))
    if missing or invalid:
        store.event('quote_batch_degraded',{'missing':missing,'invalid':invalid})
    held=set(store.portfolio()['positions'])|set(store.portfolio('option')['positions'])
    held_error=None
    if held-set(quotes):held_error=QuoteUnavailable('Held position quote missing: '+','.join(sorted(held-set(quotes))))
    if session and held_error is None:
        # All held marks must be executable and current before risk/execution.
        for s in held:
            try:executable_quote(quotes[s],store.settings,check_spread=False)
            except QuoteUnavailable as exc:
                q=quotes[s]
                held_error=QuoteUnavailable(s+': '+str(exc)+'; update_time='+str(q.get('update_time'))+'; data_date='+str(q.get('data_date')))
                break
    if held_error:
        # A stale stock mark blocks account-wide additions, but it must not
        # prevent an already-held option's valid, authorized exit monitoring.
        _held_option_exit_during_quote_gap(store,broker,option_panel,quotes,session)
        if not store.settings.continuous_paper:raise held_error
        store.set_meta('held_quote_quality',{'status':'degraded','observed_at':utcnow(),**error_evidence(held_error),
            'action':'account reconciliation succeeded; individual orders use quote or authorized stock market fallback'})
    else:store.set_meta('held_quote_quality',{'status':'ok','observed_at':utcnow()})
    risk=mark_portfolio(store,quotes,remote)
    from .recovery import resume_confirmed_acknowledgement
    resume_confirmed_acknowledgement(store)
    monitor(store,quotes)
    from .champion import enabled as champion_enabled,rotation_tick
    rotating=rotation_tick(store,broker,quotes,session) if champion_enabled(store) else False
    records=store.strategies(['shadow','paper'])
    strategy_checks=[]
    for r in records:
        if r['id'] in preparation_errors:
            strategy_checks.append({'id':r['id'][:12],'stage':r['stage'],'status':'blocked',**preparation_errors[r['id']]})
            continue
        try:
            if r['spec'].get('execution_timeframe')=='5m':
                from .intraday import execute
                outcome=execute(store,broker,r,quotes,session) if r['stage']=='paper' else {'status':'observing','timeframe':'5m'}
            elif r["stage"]=="shadow":
                outcome=shadow_tick(store,r,quotes,session,source_panel)
            elif r["stage"]=="paper":
                outcome=paper_tick(store,broker,r,quotes,session,source_panel)
            strategy_checks.append({'id':r['id'][:12],'stage':r['stage'],
                'status':'paused' if store.meta('paused') else 'closed' if not session else (outcome or {}).get('status','checked'),
                'outcome':outcome,**({'reason':outcome['reason']} if outcome and outcome.get('reason') else {})})
            from .recovery import recovered
            recovered(store,'strategy:'+r['id'])
        except (BrokerError,ValueError,KeyError,TypeError) as exc:
            # An uncertain submission must never be treated as a quote retry.
            if any(o['status'] in ('unknown','submitting') for o in store.orders(active=True)):
                raise
            from .recovery import failed
            failed(store,'strategy:'+r['id'],exc,scope='strategy')
            store.event('strategy_quote_deferred',{'strategy':r['id'],'session':session,**error_evidence(exc)})
            if r['stage']=='shadow':
                store.set_meta('shadow_outcome:'+str(session)+':'+r['id'],{'status':'blocked',**error_evidence(exc)})
            strategy_checks.append({'id':r['id'][:12],'stage':r['stage'],'status':'blocked',**error_evidence(exc)})
    if store.settings.option_paper_enabled and (option_panel is None or session and option_panel.metadata['end']!=latest_completed_session()):
        store.set_meta('option_paper_status',{'status':'blocked','observed_at':utcnow(),'detail':'日线信号过期，等待数据更新；本轮不提交期权订单。'})
    elif store.settings.option_paper_enabled:
        from .option_paper import option_tick
        try:
            option_tick(store,broker,option_panel,quotes,session)
        except Exception as exc:
            store.set_meta('option_paper_status',{'status':'error','observed_at':utcnow(),**error_evidence(exc)})
            uncertain=[o['id'] for o in store.orders(active=True) if o['bucket']=='option' and o['status'] in ('unknown','submitting')]
            if uncertain and not store.meta('paused'):
                store.pause('Option simulated acknowledgement uncertain; reconcile before further execution',
                    category='order_acknowledgement',recoverable=True,orders=uncertain)
    option_status=store.meta('option_paper_status')
    if option_status:
        comparison={k:v for k,v in option_status.items() if k!='observed_at'}
        if comparison!=store.meta('last_option_transition'):
            store.event('option_execution_status',{'session':session,**option_status})
            store.set_meta('last_option_transition',comparison)
    blockers=[]
    if store.meta('paused'):
        blockers.append(store.meta('pause_reason') or 'Execution paused')
    if not session:
        blockers.append("US regular market closed")
    daily_owners=any(r['stage']=='paper' and r['spec'].get('execution_timeframe')!='5m' for r in records)
    if daily_owners and (source_panel is None or source_panel.metadata['end']!=latest_completed_session()):
        blockers.append('Daily strategy signal cache stale')
    if not store.strategies(["paper"]):
        blockers.append("No strategy authorized for paper execution")
    blockers.extend(r.get('reason') or r.get('error_type','Execution conditions unmet') for r in strategy_checks if r['status']=='blocked')
    blockers.extend('策略 '+r['id']+'：入场窗口结束，存在未执行订单' for r in strategy_checks if r['status']=='missed_window')
    if session:
        store.set_meta('paper_execution_session',{'session':session,'observed_at':utcnow(),
            'strategies':strategy_checks,'option':store.meta('option_paper_status')})
    elif not champion_enabled(store) and any(r['status']=='ready' for r in store.meta('promotion_queue',[])):
        from .promotion import apply_promotions
        promoted=apply_promotions(store)
        if promoted:
            from .readiness import execution_readiness
            readiness=execution_readiness(store,remote)
            store.set_meta('execution_readiness',readiness)
    store.set_meta("last_tick",{"time":utcnow(),"session":session,"risk":risk,"account_tail":store.settings.stock_account[-4:],
        "signal_cache_asof":source_panel.metadata["end"] if source_panel is not None else None,"execution_blockers":blockers,'strategy_checks':strategy_checks,
        "broker_positions":sum(float(p['qty'])!=0 for p in remote["positions"]),"broker_position_rows":len(remote["positions"]),"broker_orders":len(remote["orders"]),
        "quotes":{s:{"price":q["last_price"],"data_date":q.get("data_date"),"update_time":q.get("update_time")} for s,q in quotes.items()}})
    if run_daily and not session:
        daily_jobs(store,quotes)
    return remote


def _held_option_exit_during_quote_gap(store,broker,panel,quotes,session):
    if not session or store.meta('paused') or not store.settings.option_paper_enabled or panel is None:return
    if panel.metadata['end']!=latest_completed_session():return
    positions=store.portfolio('option')['positions']
    if not positions or any(o['bucket']=='option' for o in store.orders(active=True)):return
    from .option_paper import option_quote,option_tick
    try:
        for symbol in positions:option_quote(quotes.get(symbol,{}),store.settings)
        # Frozen option_tick returns from its existing-position branch. No
        # opening path is reachable while this nonempty book is being checked.
        option_tick(store,broker,panel,quotes,session)
        store.event('option_exit_monitor_isolated',{'session':session,'reason':'Unrelated held-stock quote unavailable; no new exposure allowed'})
    except Exception as exc:
        store.event('option_exit_monitor_deferred',error_evidence(exc))
        if any(o['status'] in ('unknown','submitting') for o in store.orders(active=True)):raise


def serve(store,stop=None):
    from .observability import configure,emit
    from .recovery import failed,recovered
    configure(store.home)
    emit('service_started',pid=__import__('os').getpid(),mode='simulated_only')
    broker=FutuSim(store.settings)
    worker=None
    boot=True
    radar=None
    news_worker_thread=None
    premarket_worker_thread=None
    minute_worker_thread=None
    local_stop=stop or threading.Event()
    if store.settings.quote_push_enabled:
        from .push import run as push_run
        threading.Thread(target=push_run,args=(store,local_stop),daemon=True,name='futu-push').start()
    def radar_worker():
        try:
            from .universe import scan
            scan(store,refresh_members=False)
            store.set_meta('radar_retry_after',None)
            recovered(store,'radar')
        except Exception as exc:
            store.event('radar_failed',error_evidence(exc));failed(store,'radar',exc)
            store.set_meta('radar_retry_after',(pd.Timestamp.now(tz='UTC')+pd.Timedelta(minutes=10)).isoformat())
    def news_worker():
        try:
            from .news import refresh_news
            refresh_news(store);store.set_meta('news_retry_after',None);recovered(store,'news')
        except Exception as exc:
            failed(store,'news',exc);store.event('news_failed',error_evidence(exc))
            store.set_meta('news_retry_after',(pd.Timestamp.now(tz='UTC')+pd.Timedelta(minutes=5)).isoformat())
    def premarket_worker(session,phase):
        try:
            from .premarket import run
            result=run(store,session,phase)
            if result.get('status')=='completed':recovered(store,'premarket')
        except Exception as exc:
            failed(store,'premarket',exc,scope='information')
            store.event('premarket_failed',error_evidence(exc))
    def minute_worker():
        try:
            from .intraday import observe
            observe(store);recovered(store,'minute_data')
        except Exception as exc:
            failed(store,'minute_data',exc,scope='minute')
            store.event('minute_observation_failed',error_evidence(exc))
    def daily_worker(quotes):
        from .checkpoints import StepDeferred
        try:
            # Optional research reads belong to this worker, never the account
            # reconciliation loop. Research can use dated evidence without it.
            if not quotes:
                try:quotes={q['code']:q for q in FutuSim(store.settings).snapshots(['US.NVDA','US.QQQ','US.SPY'])}
                except Exception as exc:
                    store.event('research_quotes_unavailable',error_evidence(exc))
            daily_jobs(store,quotes)
            if store.meta('daily_completed:'+latest_completed_session()):recovered(store,'research')
        except StepDeferred:
            pass  # Another process owns the durable workflow; no new failure.
        except Exception as exc:
            store.event("daily_job_failed",error_evidence(exc));failed(store,'research',exc)
            store.set_meta("daily_retry_after:"+latest_completed_session(),
                           (pd.Timestamp.now(tz="UTC")+pd.Timedelta(minutes=1)).isoformat())
        finally:
            forecast_worker()
    def forecast_worker():
        # Pure local research evidence must not block or pause the executor.
        try:
            from .forecast_tracking import update_tracking
            update_tracking(store)
        except Exception as exc:
            store.event('forecast_tracking_failed',{'error_type':type(exc).__name__})
    def execution_failed(exc):
        uncertain=any(o['status'] in ('unknown','submitting') for o in store.orders(active=True))
        retryable=isinstance(exc,(ReadUnavailable,QuoteUnavailable,SignalUnavailable)) and not uncertain
        if not retryable and (not uncertain or isinstance(exc,(AccountIntegrityError,ValueError))) and (not store.meta('paused') or store.meta('pause_policy',{}).get('recoverable')):
            store.pause("Supervisor failure: "+type(exc).__name__,category='integrity')
        failed(store,'execution_reads',exc,scope='execution')
        store.event('supervisor_deferred' if retryable else 'supervisor_failed',error_evidence(exc))
        store.set_meta("health",{"time":utcnow(),"status":"degraded" if retryable else "error",
            'mode':(store.meta('schedule') or {}).get('mode'),
            'execution_status':'paused' if store.meta('paused') else 'blocked',**error_evidence(exc)})
    threading.Thread(target=forecast_worker,daemon=True,name='forecast-check').start()
    with executor_lock(store):
        while stop is None or not stop.is_set():
            started=time.monotonic()
            try:
                scheduled_state=activity(store,boot=boot)
                store.set_meta("schedule",scheduled_state)
                execution_error=False
                if scheduled_state["mode"]!="idle" and (scheduled_state["mode"]!="daily_research" or worker is None or not worker.is_alive()):
                    try:
                        remote=tick(store,broker,run_daily=False)
                        recovered(store,'execution_reads')
                        boot=False
                        if scheduled_state["mode"]=="pre_open":
                            from .readiness import execution_readiness
                            readiness=execution_readiness(store,remote)
                            store.set_meta('execution_readiness',readiness)
                            store.event('preflight_ready' if readiness['ready'] else 'preflight_blocked',readiness)
                            if readiness['ready']:store.set_meta("preflight_session",scheduled_state["next_session"])
                    except Exception as exc:
                        # Account/quote failures still fence orders. Read-only
                        # workers and post-close research must keep their own
                        # schedule, even during a persistent stale held quote.
                        execution_error=True
                        execution_failed(exc)
                latest=store.meta('last_tick') or {}
                active_checks=[r for r in latest.get('strategy_checks',[]) if r['stage']=='paper'] if scheduled_state.get('session')==latest.get('session') and latest.get('session') else []
                blocked_count=sum(r['status']=='blocked' or r['status']=='missed_window' and not (r.get('outcome') or {}).get('submitted_orders') for r in active_checks)
                degraded_count=sum(r['status']=='partial' or r['status']=='missed_window' and (r.get('outcome') or {}).get('submitted_orders',0)>0 for r in active_checks)
                execution_status='blocked' if active_checks and blocked_count==len(active_checks) else 'degraded' if blocked_count or degraded_count else 'ready'
                from .champion import enabled as champion_enabled,pending as pending_rotation
                if champion_enabled(store) and pending_rotation(store):execution_status='rotating'
                if not execution_error:
                    previous=store.meta('component:execution_reads',{})
                    unresolved=previous.get('status')=='degraded'
                    health=store.meta('health',{}) if unresolved else {}
                    quote_degraded=store.meta('held_quote_quality',{}).get('status')=='degraded' and bool(scheduled_state.get('session'))
                    store.set_meta("health",{**health,"time":utcnow(),"status":health.get('status','degraded') if unresolved else 'degraded' if quote_degraded else "ok","mode":scheduled_state["mode"],
                        'execution_status':'paused' if store.meta('paused') else execution_status})
                last=store.meta('universe_scan',{}).get('created_at')
                retry=store.meta('radar_retry_after')
                now=pd.Timestamp.now(tz='UTC')
                if scheduled_state.get('session') and (radar is None or not radar.is_alive()) and (not retry or now>=pd.Timestamp(retry)) and (not last or (now-pd.Timestamp(last)).total_seconds()>=store.settings.broad_scan_seconds):
                    radar=threading.Thread(target=radar_worker,daemon=True,name='market-radar');radar.start()
                last_news=store.meta('news_packet',{}).get('observed_at');news_retry=store.meta('news_retry_after')
                if scheduled_state.get('session') and (news_worker_thread is None or not news_worker_thread.is_alive()) and (not news_retry or now>=pd.Timestamp(news_retry)) and (not last_news or (now-pd.Timestamp(last_news)).total_seconds()>=store.settings.news_scan_seconds):
                    news_worker_thread=threading.Thread(target=news_worker,daemon=True,name='market-news');news_worker_thread.start()
                if scheduled_state.get('session') and store.meta('intraday_enabled',False) and (minute_worker_thread is None or not minute_worker_thread.is_alive()):
                    minute_worker_thread=threading.Thread(target=minute_worker,daemon=True,name='minute-data');minute_worker_thread.start()
                # Optional pre-open intelligence has its own worker and failures.
                try:
                    from .premarket import due as premarket_due
                    preopen=premarket_due(store,now)
                    if preopen and (premarket_worker_thread is None or not premarket_worker_thread.is_alive()):
                        premarket_worker_thread=threading.Thread(target=premarket_worker,
                            args=(preopen['session'],preopen['phase']),daemon=True,name='premarket-intelligence')
                        premarket_worker_thread.start()
                except Exception as exc:
                    failed(store,'premarket_scheduler',exc,scope='information')
                # AI/research/history downloads cannot block order reconciliation.
                try:
                    due=daily_due(store);recovered(store,'research_scheduler')
                    if due and (worker is None or not worker.is_alive()):
                        worker=threading.Thread(target=daily_worker,args=({},),daemon=True,name="quant-research")
                        worker.start()
                except Exception as exc:
                    failed(store,'research_scheduler',exc)
                    store.event('research_schedule_failed',error_evidence(exc))
            except Exception as exc:
                execution_failed(exc)
            wait=max(1,store.settings.poll_seconds-(time.monotonic()-started))
            if stop:
                stop.wait(wait)
            else:
                time.sleep(wait)
