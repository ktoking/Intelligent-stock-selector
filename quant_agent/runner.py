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

from research.daily_factor_lab.core import Policy, load_panel, rank
from research.daily_factor_lab.paper import data_prefix_hash
from research.daily_factor_lab.refresh import refresh
from .exchange_clock import us_calendar,latest_completed_session
from .ai import ask
from .broker import BrokerError, QuoteUnavailable, ReadUnavailable, SignalUnavailable, FutuSim, executable_quote, session_now
from .config import canonical, digest
from .engine import apply, empty, equity, plan
from .pipeline import artifact, engine_hash, evaluate, propose, shadow_record
from .store import utcnow
from .evolution import compile_strategy as compile_spec
from .dataset import frozen_view
from .schedule import activity, daily_due


def error_evidence(exc):
    # Frame locations and our own bounded messages, never request bodies/URLs/keys.
    result={'error_type':type(exc).__name__, 'frames':[
        {'file':Path(f.filename).name,'line':f.lineno,'function':f.name}
        for f in traceback.extract_tb(exc.__traceback__)[-5:]]}
    message=str(exc)
    if isinstance(exc,BrokerError) or message.startswith(('Invalid OHLCV:', 'New cache', 'AI evidence packet too large')):
        result['reason']=message[:240]
    return result


@contextmanager
def job_trace(store,job,asof):
    start=time.monotonic()
    store.set_meta("task:"+job,{"status":"running","asof":asof,"started_at":utcnow()})
    try:
        yield
    except Exception as exc:
        store.set_meta("task:"+job,{"status":"failed","asof":asof,"finished_at":utcnow(),
                                  "duration_seconds":round(time.monotonic()-start,2),**error_evidence(exc)})
        raise
    else:
        store.set_meta("task:"+job,{"status":"completed","asof":asof,"finished_at":utcnow(),
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
    remote = broker.verify() if not broker.verified else broker.state()
    bound = store.meta("bound_account")
    identity = digest([store.settings.stock_account,store.settings.stock_market])
    if bound and bound != identity:
        raise BrokerError("Account changed underneath persistent ledger")
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
                raise BrokerError("Duplicate broker client marker")
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
                raise BrokerError("Broker order identity differs from intent")
            if int(match["status"]) not in statuses:
                raise BrokerError("Unknown broker status; stop new execution")
            store.fill(row["id"],integer_quantity(match.get("cum_qty",0)),float(match.get("avg_fill_price") or 0),statuses[int(match["status"])])
        elif row["status"] in ("submitted","partial"):
            raise BrokerError("Acknowledged order absent from current and history queries")
    known = {r["broker_id"] for r in store.orders() if r["broker_id"]}
    foreign = [r for r in remote["orders"] if str(r["order_id"]) not in known and int(r["status"]) in (2,3)]
    if foreign:
        raise BrokerError("Unmanaged working order on simulated account")
    expected = {s:p["qty"] for bucket in ('stock','option') for s,p in store.portfolio(bucket)["positions"].items()}
    actual = {}
    for p in remote["positions"]:
        qty = float(p["qty"])
        if int(p.get("pstn_type",0)) != 0 or qty != int(qty):
            raise BrokerError("Short or fractional external position unsupported")
        if qty:
            actual[normalize_symbol(p["symbol"])] = int(qty)
    if actual != expected:
        raise BrokerError("Broker positions differ from allocated ledger; do not adopt external positions")
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
    peak=max(store.meta("peak_nav",10000),nav)
    store.set_meta("peak_nav",peak)
    session=latest_completed_session()
    previous=store.meta("daily_nav",{"session":session,"nav":10000})
    dd=(1-nav/peak)*100
    loss=(1-nav/previous["nav"])*100
    if dd>=store.settings.max_drawdown_pct or loss>=store.settings.daily_loss_limit_pct:
        store.pause(f"Risk breach: drawdown={dd:.2f}%, daily_loss={loss:.2f}%")
    if session!=previous["session"]:
        store.set_meta("daily_nav",{"session":session,"nav":nav})
    if float(remote["cash"]["balance"]) < 0:
        store.pause("Broker cash negative; no margin allowed")
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
    return {"nav":nav,"drawdown_pct":dd,"daily_loss_pct":loss}


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
    panel=frozen_view(source_panel,record);evidence=record['evidence']
    if data_prefix_hash(panel,evidence['seed_asof'])==evidence['seed_hash']:return panel
    # New research/Shadow versions may use the revised vendor history while
    # already running paper versions retain their independent observed prefix.
    candidates=[store.meta('execution_cache:'+record['id']),store.meta('current_data_dir')]
    for directory in dict.fromkeys(c for c in candidates if c):
        alternative=frozen_view(load_panel(directory),record)
        if data_prefix_hash(alternative,evidence['seed_asof'])==evidence['seed_hash']:return alternative
    return panel  # checked_panel will retain the strict freeze failure.


def checked_panel(store,record,source_panel=None):
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
        return
    calendar=us_calendar()
    # Freeze a causal plan at an observed quote in first 5 minutes, not hindsight open.
    if pd.Timestamp.now(tz="UTC")>calendar.session_open(session)+pd.Timedelta(minutes=5):
        return
    if calendar.session_open(session)<=pd.Timestamp(record["evidence"]["shadow_started_at"]):
        return
    if store.meta("shadow_last_plan:"+record["id"])==session:
        return
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
    store.set_meta(key,{"session":session,"state":state,"orders":orders,"data_hash":panel.metadata["input_sha256"],"observed":utcnow()})
    store.set_meta("shadow_last_plan:"+record["id"],session)
    store.event("shadow_observed_plan",{"strategy":record["id"],"session":session,"orders":orders,"fill_model":"observed quote midpoint +25bps, hypothetical; not broker fills"})


def paper_tick(store,broker,record,quotes,session,source_panel=None):
    if not session or store.meta("paused") or not store.settings.paper_enabled:
        return
    calendar=us_calendar()
    if pd.Timestamp.now(tz="UTC")>calendar.session_open(session)+pd.Timedelta(minutes=5):
        suffix=':'+record['id'] if store.settings.parallel_paper else ''
        outcome=store.meta('paper_outcome:'+session+suffix)
        if outcome is None or outcome.get('status')=='partial':
            outcome={**(outcome or {}),'status':'missed_window','session':session,'strategy':record['id'],
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
        orders,controls=plan(panel,len(panel.dates)-1,policy,state,session,prices,store.settings.fee_reserve_bps,capital_cap=budget)
        pending={"strategy":record["id"],"orders":orders,"controls":controls}
        with store.transaction() as db:
            store.set_meta(key,pending,db)
            store.set_meta("paper_controls"+suffix,controls,db)
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
        deadline=(pd.Timestamp(row['created'])+pd.Timedelta(minutes=2) if row['bucket']=='option'
                  else calendar.session_open(row["session"])+pd.Timedelta(minutes=5))
        if pd.Timestamp.now(tz="UTC")<=deadline:
            continue
        if row["status"]=="reserved":
            store.fill(row["id"],0,0,"cancelled")
        elif row["status"] in ("submitted","partial") and row["broker_id"]:
            key='cancel_attempts:'+row['id'];attempts=store.meta(key,{'count':0})
            last=attempts.get('last_at')
            if attempts['count']<3 and (not last or pd.Timestamp.now(tz='UTC')>=pd.Timestamp(last)+pd.Timedelta(seconds=store.settings.poll_seconds)):
                store.set_meta(key,{'count':attempts['count']+1,'last_at':pd.Timestamp.now(tz='UTC').isoformat()})
                try:
                    broker.cancel(row["broker_id"])
                    store.event("cancel_requested",{"id":row["id"]})
                except Exception as exc:
                    store.event('cancel_uncertain',{'id':row['id'],**error_evidence(exc)})
                    if not store.meta('paused'):store.pause("Cancellation uncertain or rejected; reconcile working order")
            elif attempts['count']>=3 and store.once('cancel_cap:'+row['id']):
                store.event('cancel_retry_cap',{'id':row['id'],'attempts':3})
                if not store.meta('paused'):store.pause('Cancellation retry cap reached; reconcile working order')


def daily_jobs(store,quotes):
    asof=latest_completed_session()
    from .research_evidence import research_pending
    if store.meta("daily_completed:"+asof) and not research_pending(store,asof):
        return
    retry=store.meta("daily_retry_after:"+asof)
    if retry and pd.Timestamp.now(tz="UTC")<pd.Timestamp(retry):
        return
    jobs_ok=True
    if store.settings.auto_refresh:
        try:
            from .market_data import refresh_cache
            with job_trace(store,"refresh",asof):
                refresh_cache(store)
        except Exception as exc:
            jobs_ok=False
            store.event("refresh_failed",{**error_evidence(exc),"asof":asof})
    if not jobs_ok:
        store.set_meta('task:review',{'status':'blocked','asof':asof,'reason':'历史数据未通过校验，等待刷新重试；尚未调用AI。'})
        store.set_meta('daily_completed:'+asof,False)
        store.set_meta('daily_retry_after:'+asof,(pd.Timestamp.now(tz='UTC')+pd.Timedelta(hours=1)).isoformat())
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
    if jobs_ok and not store.meta('news_completed:'+asof):
        try:
            with job_trace(store,'news',asof):refresh_news(store)
            store.set_meta('news_completed:'+asof,True)
        except Exception as exc:store.event('news_failed',{'error_type':type(exc).__name__})
    if jobs_ok and store.settings.daily_research:
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
                            propose(store,ai=True)
                        store.set_meta('discovery_completed:'+data_hash,True)
                except Exception as exc:
                    jobs_ok=False
                    store.event("research_failed",{"error_type":type(exc).__name__})
            # Versioned, fixed control strategies are rechecked on revised data
            # even when AI makes no new proposal or is temporarily unavailable.
            try:
                with job_trace(store,"backtest",asof):
                    evaluate(store)
                store.set_meta("research_completed:"+data_hash,True)
            except Exception as exc:
                jobs_ok=False
                store.event("validation_failed",{"error_type":type(exc).__name__})
    from .briefing import review_packet, REVIEW_SCHEMA, REVIEW_SYSTEM, validate_brief, persist_brief
    packet=review_packet(store,quotes,panel=load_panel(store.meta("current_data_dir",store.settings.data_dir)))
    response=None
    # Observation metadata must not make identical evidence miss its cache.
    name="daily-"+asof+"-"+digest({k:v for k,v in packet.items() if k!="created"})[:12]
    saved=store.home/"artifacts"/(name+".json")
    if saved.exists():
        response=json.loads(saved.read_text())["ai"]
    if response is None:
        try:
            with job_trace(store,"review",asof):
                response=ask(store.settings.ai_provider,{"purpose":"daily_review","asof":asof,"evidence":packet},
                             model_name=store.settings.ai_model,reasoning_effort=store.settings.ai_reasoning_effort,
                             output_schema=REVIEW_SCHEMA,system_instruction=REVIEW_SYSTEM)
                validate_brief(response,packet)
            store.event("ai_review",response)
            store.set_meta('ai_runtime',{'model':response['model'],'reasoning_effort':response.get('reasoning_effort'),'observed_at':utcnow()})
        except Exception as exc:
            response=None
            store.event("ai_unavailable",{"job":"daily_review",**error_evidence(exc)})
    jobs_ok=jobs_ok and response is not None
    directory=store.home/"reports"; directory.mkdir(exist_ok=True,mode=0o700)
    summary=["# AI 量化每日复盘 "+asof,"", "股票预算 $10,000；期权预算 $1,000。实盘执行：关闭。",
             "", "行情来源 Futunn REST；时间依据 data_date，不把周末缓存叫实时。",
             "", '复盘生成时间：'+pd.Timestamp.now(tz='Asia/Shanghai').isoformat()+'。行情截至上述已收盘交易日，资讯按首次观察时间可用。',
             "", "策略阶段：",*['- '+r['spec'].get('factor_name',r['spec']['policy']['name'])+': '+r['stage'] for r in packet['strategies']],
             "", f"订单累计 {len(packet['orders'])}；暂停={packet['paused']}","", "## AI 复盘",
             response["result"]["review"] if response else "AI 当前不可用；执行和风控仍由确定性程序控制。",
             "", "## 因子待办",*(["- "+s for s in response["result"]["factor_ideas"]] if response else []),
             "", f"资讯：{len(packet['news'])}条带时间戳标题，首次观察后可用于分析；尚不含全文核实或历史新闻因子。期权模拟执行{'已授权，实际状态见账本' if store.settings.option_paper_enabled else '关闭'}。"]
    path=directory/(asof+".md"); path.write_text("\n".join(summary)); path.chmod(0o600)
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
    if response and jobs_ok and store.settings.evolution_enabled and (cycle_trials<store.settings.max_trials or store.meta('evolution_completed:'+packet['data']['input_sha256'])):
        try:
            from .evolution import evolve
            with job_trace(store,'evolution',asof):
                result=evolve(store,response['result']['factor_ideas'])
                evaluate(store)
            record=next(r for r in store.strategies() if r['id']==result['strategy'])
            code_state='已生成独立代码并验证。' if result.get('code_path') else '候选被结构或因果检查拦截。'
            path.write_text(path.read_text()+'\n\n## 新因子代码与验证\n'+result['name']+'：'+record['stage']+'。'+code_state+'\n'+result['hypothesis'])
        except Exception as exc:
            jobs_ok=False
            store.event('evolution_failed',{'error_type':type(exc).__name__})
    elif store.settings.evolution_enabled and cycle_trials>=store.settings.max_trials:
        store.set_meta('task:evolution',{'status':'blocked','asof':asof,'reason':'当前数据版本达到试验上限；保留既有候选，不新增公式。'})
    if jobs_ok and store.settings.daily_research:
        from .promotion import plan_promotions
        with job_trace(store,'promotion',asof):
            plan_promotions(store,load_panel(store.meta('current_data_dir',store.settings.data_dir)))
    if jobs_ok:
        store.set_meta('research_cycle_completed:'+asof,store.meta('current_data_dir',store.settings.data_dir))
        from .research_evidence import append_research_review
        append_research_review(store,asof)
    store.set_meta("daily_completed:"+asof,jobs_ok)
    if not jobs_ok:
        store.set_meta("daily_retry_after:"+asof,(pd.Timestamp.now(tz="UTC")+pd.Timedelta(hours=1)).isoformat())
    else:store.set_meta("daily_retry_after:"+asof,None)


def tick(store,broker,run_daily=True):
    remote=reconcile(store,broker)
    # Cancellation/recovery must run even when the quote service is unavailable.
    session=session_now()
    expire_orders(store,broker,session)
    symbols={"US.NVDA","US.QQQ","US.SPY"}|set(store.portfolio()["positions"])|set(store.portfolio('option')['positions'])
    records=store.strategies(["shadow","paper"])
    source_panel=load_panel(store.meta("execution_data_dir",store.meta("current_data_dir",store.settings.data_dir)))
    for r in records:
        state=store.meta("shadow_state:"+r["id"],empty())
        pending=store.meta("shadow_pending:"+r["id"])
        symbols.update(state["holdings"])
        if pending:
            symbols.update(pending["state"]["holdings"])
        panel=execution_view(store,r,source_panel)
        panel,policy=compile_spec(panel,r["spec"])
        symbols.update(panel.symbols[j] for j in rank(panel,len(panel.dates)-1,policy)[:r["spec"]["policy"]["rank_buffer"]])
    quotes={};invalid=[]
    for q in broker.snapshots(sorted(symbols)):
        try:
            code=q['code'];price=float(q.get('last_price') or 0)
            if code not in symbols or not math.isfinite(price) or price<=0:raise ValueError('Invalid snapshot')
            if code in quotes:raise QuoteUnavailable('Duplicate snapshot symbol: '+code)
            stamp=q.get('update_time')
            if not isinstance(stamp,(str,int,float)) or isinstance(stamp,(int,float)) and not math.isfinite(stamp):stamp=None
            quotes[code]={**q,'last_price':price,'update_time':stamp,'data_date':q.get('data_date') if isinstance(q.get('data_date'),str) else None}
        except (KeyError,ValueError,TypeError):invalid.append(q.get('code') if isinstance(q,dict) else None)
    missing=sorted(symbols-set(quotes))
    if missing or invalid:
        store.event('quote_batch_degraded',{'missing':missing,'invalid':invalid})
    held=set(store.portfolio()['positions'])|set(store.portfolio('option')['positions'])
    if held-set(quotes):raise QuoteUnavailable('Held position quote missing: '+','.join(sorted(held-set(quotes))))
    if session:
        # All held marks must be executable and current before risk/execution.
        for s in held:
            executable_quote(quotes[s],store.settings,check_spread=False)
    risk=mark_portfolio(store,quotes,remote)
    monitor(store,quotes)
    strategy_checks=[]
    for r in records:
        try:
            if r["stage"]=="shadow":
                shadow_tick(store,r,quotes,session,source_panel)
            elif r["stage"]=="paper":
                outcome=paper_tick(store,broker,r,quotes,session,source_panel)
            strategy_checks.append({'id':r['id'][:12],'stage':r['stage'],
                'status':'paused' if store.meta('paused') else 'closed' if not session else (outcome or {}).get('status','checked') if r['stage']=='paper' else 'checked',
                **({'outcome':outcome} if r['stage']=='paper' else {})})
        except (QuoteUnavailable,SignalUnavailable) as exc:
            # An uncertain submission must never be treated as a quote retry.
            if any(o['status'] in ('unknown','submitting') for o in store.orders(active=True)):
                raise
            store.event('strategy_quote_deferred',{'strategy':r['id'],'session':session,**error_evidence(exc)})
            strategy_checks.append({'id':r['id'][:12],'stage':r['stage'],'status':'blocked',**error_evidence(exc)})
    if store.settings.option_paper_enabled and session and source_panel.metadata['end']!=latest_completed_session():
        store.set_meta('option_paper_status',{'status':'blocked','observed_at':utcnow(),'detail':'日线信号过期，等待数据更新；本轮不提交期权订单。'})
    elif store.settings.option_paper_enabled:
        from .option_paper import option_tick
        try:
            option_tick(store,broker,source_panel,quotes,session)
        except Exception as exc:
            store.set_meta('option_paper_status',{'status':'error','observed_at':utcnow(),**error_evidence(exc)})
            if any(o['bucket']=='option' and o['status'] in ('unknown','submitting') for o in store.orders(active=True)):
                store.pause('Option simulated acknowledgement uncertain; reconcile before further execution')
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
    if source_panel.metadata["end"]!=latest_completed_session():
        blockers.append("Signal cache stale: "+source_panel.metadata["end"])
    if not store.strategies(["paper"]):
        blockers.append("No strategy authorized for paper execution")
    blockers.extend(r.get('reason') or r.get('error_type','Execution conditions unmet') for r in strategy_checks if r['status']=='blocked')
    blockers.extend('策略 '+r['id']+'：入场窗口结束，存在未执行订单' for r in strategy_checks if r['status']=='missed_window')
    if session:
        store.set_meta('paper_execution_session',{'session':session,'observed_at':utcnow(),
            'strategies':strategy_checks,'option':store.meta('option_paper_status')})
    elif any(r['status']=='ready' for r in store.meta('promotion_queue',[])):
        from .promotion import apply_promotions
        promoted=apply_promotions(store)
        if promoted:
            from .readiness import execution_readiness
            readiness=execution_readiness(store,remote)
            store.set_meta('execution_readiness',readiness)
    store.set_meta("last_tick",{"time":utcnow(),"session":session,"risk":risk,"account_tail":store.settings.stock_account[-4:],
        "signal_cache_asof":source_panel.metadata["end"],"execution_blockers":blockers,'strategy_checks':strategy_checks,
        "broker_positions":len(remote["positions"]),"broker_orders":len(remote["orders"]),
        "quotes":{s:{"price":q["last_price"],"data_date":q.get("data_date"),"update_time":q.get("update_time")} for s,q in quotes.items()}})
    if run_daily and not session:
        daily_jobs(store,quotes)
    return remote


def serve(store,stop=None):
    broker=FutuSim(store.settings)
    worker=None
    boot=True
    radar=None
    local_stop=stop or threading.Event()
    if store.settings.quote_push_enabled:
        from .push import run as push_run
        threading.Thread(target=push_run,args=(store,local_stop),daemon=True,name='futu-push').start()
    def radar_worker():
        try:
            from .universe import scan
            scan(store,refresh_members=False)
            store.set_meta('radar_retry_after',None)
        except Exception as exc:
            store.event('radar_failed',{'error_type':type(exc).__name__})
            store.set_meta('radar_retry_after',(pd.Timestamp.now(tz='UTC')+pd.Timedelta(minutes=10)).isoformat())
        last=store.meta('news_packet',{}).get('observed_at')
        if not last or (pd.Timestamp.now(tz='UTC')-pd.Timestamp(last)).total_seconds()>=store.settings.news_scan_seconds:
            try:
                from .news import refresh_news
                refresh_news(store)
            except Exception as exc:store.event('news_failed',{'error_type':type(exc).__name__})
    def daily_worker(quotes):
        try:
            daily_jobs(store,quotes)
        except Exception as exc:
            store.event("daily_job_failed",{"error_type":type(exc).__name__})
            store.set_meta("daily_retry_after:"+latest_completed_session(),
                           (pd.Timestamp.now(tz="UTC")+pd.Timedelta(hours=1)).isoformat())
        finally:
            forecast_worker()
    def forecast_worker():
        # Pure local research evidence must not block or pause the executor.
        try:
            from .forecast_tracking import update_tracking
            update_tracking(store)
        except Exception as exc:
            store.event('forecast_tracking_failed',{'error_type':type(exc).__name__})
    threading.Thread(target=forecast_worker,daemon=True,name='forecast-check').start()
    with executor_lock(store):
        while stop is None or not stop.is_set():
            started=time.monotonic()
            try:
                scheduled_state=activity(store,boot=boot)
                store.set_meta("schedule",scheduled_state)
                if scheduled_state["mode"]!="idle" and (scheduled_state["mode"]!="daily_research" or worker is None or not worker.is_alive()):
                    remote=tick(store,broker,run_daily=False)
                    boot=False
                    if scheduled_state["mode"]=="pre_open":
                        from .readiness import execution_readiness
                        readiness=execution_readiness(store,remote)
                        store.set_meta('execution_readiness',readiness)
                        store.event('preflight_ready' if readiness['ready'] else 'preflight_blocked',readiness)
                        if readiness['ready']:store.set_meta("preflight_session",scheduled_state["next_session"])
                latest=store.meta('last_tick') or {}
                active_checks=[r for r in latest.get('strategy_checks',[]) if r['stage']=='paper'] if scheduled_state.get('session')==latest.get('session') and latest.get('session') else []
                blocked_count=sum(r['status'] in ('blocked','missed_window') for r in active_checks)
                degraded_count=sum(r['status']=='partial' for r in active_checks)
                execution_status='blocked' if active_checks and blocked_count==len(active_checks) else 'degraded' if blocked_count or degraded_count else 'ready'
                store.set_meta("health",{"time":utcnow(),"status":"ok","mode":scheduled_state["mode"],
                    'execution_status':'paused' if store.meta('paused') else execution_status})
                last=store.meta('universe_scan',{}).get('created_at')
                retry=store.meta('radar_retry_after')
                now=pd.Timestamp.now(tz='UTC')
                if scheduled_state.get('session') and (radar is None or not radar.is_alive()) and (not retry or now>=pd.Timestamp(retry)) and (not last or (now-pd.Timestamp(last)).total_seconds()>=store.settings.broad_scan_seconds):
                    radar=threading.Thread(target=radar_worker,daemon=True,name='market-radar');radar.start()
                # AI/research/history downloads cannot block order reconciliation.
                if daily_due(store) and (worker is None or not worker.is_alive()):
                    quotes={q["code"]:q for q in broker.snapshots(["US.NVDA","US.QQQ","US.SPY"])}
                    worker=threading.Thread(target=daily_worker,args=(quotes,),daemon=True,name="quant-research")
                    worker.start()
            except Exception as exc:
                uncertain=any(o['status'] in ('unknown','submitting') for o in store.orders(active=True))
                retryable=isinstance(exc,(ReadUnavailable,QuoteUnavailable,SignalUnavailable)) and not uncertain
                if not retryable and not store.meta('paused'):
                    store.pause("Supervisor failure: "+type(exc).__name__)
                store.event('supervisor_deferred' if retryable else 'supervisor_failed',error_evidence(exc))
                store.set_meta("health",{"time":utcnow(),"status":"degraded" if retryable else "error",
                    'execution_status':'paused' if store.meta('paused') else 'blocked',**error_evidence(exc)})
            wait=max(1,store.settings.poll_seconds-(time.monotonic()-started))
            if stop:
                stop.wait(wait)
            else:
                time.sleep(wait)
