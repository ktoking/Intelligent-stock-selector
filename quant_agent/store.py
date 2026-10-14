"""SQLite transactions protect stages, reservations and cumulative fills."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .config import canonical, digest


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, home, settings):
        self.home, self.settings = Path(home), settings
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.home / "state.sqlite"
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS strategies (
                    id TEXT PRIMARY KEY, spec TEXT NOT NULL, stage TEXT NOT NULL,
                    created TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY, created TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY, strategy TEXT NOT NULL, bucket TEXT NOT NULL,
                    symbol TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL,
                    price REAL NOT NULL, multiplier INTEGER NOT NULL, fee REAL NOT NULL,
                    status TEXT NOT NULL, broker_id TEXT, filled INTEGER NOT NULL DEFAULT 0,
                    fill_value REAL NOT NULL DEFAULT 0, created TEXT NOT NULL, session TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS balances (bucket TEXT PRIMARY KEY, cash REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS positions (
                    bucket TEXT NOT NULL, symbol TEXT NOT NULL, qty INTEGER NOT NULL,
                    basis REAL NOT NULL, mark REAL NOT NULL, multiplier INTEGER NOT NULL,
                    PRIMARY KEY(bucket,symbol));
                CREATE TABLE IF NOT EXISTS shadow (
                    strategy TEXT NOT NULL, session TEXT NOT NULL, observed TEXT NOT NULL,
                    state TEXT NOT NULL, data_hash TEXT NOT NULL, PRIMARY KEY(strategy,session));
                CREATE TABLE IF NOT EXISTS jobs (key TEXT PRIMARY KEY, created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS news (id TEXT PRIMARY KEY,available_at TEXT NOT NULL,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fills (seq INTEGER PRIMARY KEY,order_id TEXT NOT NULL,observed TEXT NOT NULL,
                    qty INTEGER NOT NULL,price REAL NOT NULL,fee REAL NOT NULL,realized_pnl REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS paper_nav (observed TEXT PRIMARY KEY,session TEXT NOT NULL,
                    nav REAL NOT NULL,cash REAL NOT NULL,unrealized REAL NOT NULL,realized REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS strategy_paper_nav (strategy TEXT NOT NULL,observed TEXT NOT NULL,
                    session TEXT NOT NULL,nav REAL NOT NULL,cash REAL NOT NULL,unrealized REAL NOT NULL,
                    realized REAL NOT NULL,budget REAL NOT NULL,PRIMARY KEY(strategy,observed));
                CREATE TABLE IF NOT EXISTS option_paper_nav (observed TEXT PRIMARY KEY,session TEXT NOT NULL,
                    nav REAL NOT NULL,cash REAL NOT NULL,unrealized REAL NOT NULL,realized REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS position_marks (bucket TEXT NOT NULL,symbol TEXT NOT NULL,
                    quote_time TEXT NOT NULL,observed TEXT NOT NULL,data_date TEXT,price REAL NOT NULL,
                    PRIMARY KEY(bucket,symbol,quote_time));
                CREATE TABLE IF NOT EXISTS strategy_funding (id TEXT PRIMARY KEY,strategy TEXT NOT NULL,
                    bucket TEXT NOT NULL,amount REAL NOT NULL,observed TEXT NOT NULL,kind TEXT NOT NULL);
            """)
            if settings.parallel_paper:
                db.execute('DROP INDEX IF EXISTS one_paper_executor')
            else:
                db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_paper_executor ON strategies((1)) WHERE stage IN ('paper','live_review')")
            for bucket, budget in (("stock", settings.stock_budget), ("option", settings.option_budget)):
                db.execute("INSERT OR IGNORE INTO balances VALUES (?,?)", (bucket, budget))
            db.execute("INSERT OR IGNORE INTO meta VALUES ('paused','false')")
            old = db.execute("SELECT value FROM meta WHERE key='budget_hash'").fetchone()
            budget_hash = digest([settings.stock_budget, settings.option_budget])
            if old and old[0] != budget_hash:
                raise ValueError("Persisted budgets cannot be reset by changing config")
            db.execute("INSERT OR IGNORE INTO meta VALUES ('budget_hash',?)", (budget_hash,))
        self.path.chmod(0o600)

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def event(self, kind, payload, db=None):
        if db is None:
            with self.transaction() as db:
                self.event(kind, payload, db)
        else:
            db.execute("INSERT INTO events(created,kind,payload) VALUES (?,?,?)", (utcnow(), kind, canonical(payload)))

    def meta(self, key, default=None):
        with self.transaction() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_meta(self, key, value, db=None):
        if db is None:
            with self.transaction() as db:
                self.set_meta(key, value, db)
        else:
            db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, canonical(value)))

    def pause(self, reason):
        with self.transaction() as db:
            self.set_meta("paused", True, db)
            self.set_meta("pause_reason", reason, db)
            self.event("pause", {"reason": reason}, db)

    def propose(self, spec):
        key = digest(spec)
        with self.transaction() as db:
            executable={k:v for k,v in spec.items() if k not in ("hypothesis","origin")}
            for previous in db.execute("SELECT id,spec FROM strategies"):
                old=json.loads(previous["spec"])
                if {k:v for k,v in old.items() if k not in ("hypothesis","origin")}==executable:
                    self.event("duplicate_proposal",{"id":previous["id"],"origin":spec.get("origin")},db)
                    return previous["id"]
            if not db.execute("SELECT 1 FROM strategies WHERE id=?", (key,)).fetchone():
                count = db.execute("SELECT COUNT(*) FROM strategies").fetchone()[0]
                cycle=sum(json.loads(r[0]).get('research_data_sha256')==spec.get('research_data_sha256') for r in db.execute('SELECT spec FROM strategies'))
                if cycle >= self.settings.max_trials:
                    raise ValueError("Trial cap reached; no silent parameter search")
                db.execute("INSERT INTO strategies(id,spec,stage,created) VALUES (?,?,?,?)", (key, canonical(spec), "proposed", utcnow()))
                self.event("strategy_proposed", {"id": key, "trial": count + 1}, db)
        return key

    def strategies(self, stages=None):
        with self.transaction() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM strategies ORDER BY created,id")]
        for row in rows:
            row["spec"], row["evidence"] = json.loads(row["spec"]), json.loads(row["evidence"])
        return [r for r in rows if not stages or r["stage"] in stages]

    def transition(self, key, expected, target, evidence):
        allowed = {"proposed": {"backtested", "rejected"}, "backtested": {"validated", "rejected"},
                   "validated": {"shadow"}, "shadow": {"paper", "rejected"}, "paper": {"live_review", "rejected"}}
        if target not in allowed.get(expected, set()):
            raise ValueError("Forbidden stage transition; live execution is not implemented")
        with self.transaction() as db:
            if target=="paper" and not self.settings.parallel_paper and db.execute("SELECT 1 FROM strategies WHERE stage IN ('paper','live_review') AND id!=?",(key,)).fetchone():
                raise ValueError("Another strategy owns the paper executor")
            updated = db.execute("UPDATE strategies SET stage=?,evidence=? WHERE id=? AND stage=?",
                                 (target, canonical(evidence), key, expected)).rowcount
            if updated != 1:
                raise ValueError("Concurrent or invalid stage change")
            self.event("stage", {"id": key, "from": expected, "to": target, "evidence": evidence}, db)

    def once(self, key):
        with self.transaction() as db:
            return db.execute("INSERT OR IGNORE INTO jobs VALUES (?,?)", (key, utcnow())).rowcount == 1

    def orders(self, active=False):
        with self.transaction() as db:
            return [dict(r) for r in db.execute("SELECT * FROM orders" +
                     (" WHERE status NOT IN ('filled','cancelled','rejected')" if active else "") + " ORDER BY created,id")]

    def portfolio(self, bucket="stock", db=None):
        if db is None:
            with self.transaction() as db:
                return self.portfolio(bucket, db)
        return {"cash": db.execute("SELECT cash FROM balances WHERE bucket=?", (bucket,)).fetchone()[0],
                "positions": {r["symbol"]: dict(r) for r in db.execute("SELECT * FROM positions WHERE bucket=? AND qty>0", (bucket,))}}

    def reserve(self, key, strategy, bucket, symbol, side, qty, price, session, multiplier=1):
        import math
        if bucket not in ("stock", "option") or side not in ("BUY", "SELL"):
            raise ValueError("Cash-only long positions")
        if (type(qty) is not int or qty <= 0 or not math.isfinite(price) or price <= 0
                or multiplier != (100 if bucket == "option" else 1)):
            raise ValueError("Invalid quantity, price or contract multiplier")
        fee = qty * price * multiplier * self.settings.fee_reserve_bps / 10000
        with self.transaction() as db:
            existing = db.execute("SELECT * FROM orders WHERE id=?", (key,)).fetchone()
            if existing:
                if (existing["strategy"], existing["bucket"], existing["symbol"], existing["side"], existing["qty"], existing["price"], existing["session"]) != (strategy,bucket,symbol,side,qty,price,session):
                    raise ValueError("Idempotency key reused for another intent")
                return False
            paused = json.loads(db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0])
            if paused:
                raise ValueError("Executor paused; reconcile before resuming")
            record = db.execute("SELECT stage FROM strategies WHERE id=?", (strategy,)).fetchone()
            option_row=db.execute("SELECT value FROM meta WHERE key='option_paper_strategy'").fetchone()
            option_agent=json.loads(option_row[0]) if option_row else {}
            authorized_option=(bucket=='option' and self.settings.option_paper_enabled and option_agent.get('authorized')
                and option_agent.get('id')==strategy and digest(option_agent.get('spec'))==strategy)
            if not authorized_option and (not record or record[0] != "paper"):
                raise ValueError("Strategy has not passed paper gates")
            portfolio = self.portfolio(bucket, db)
            active = [dict(r) for r in db.execute("SELECT * FROM orders WHERE bucket=? AND status NOT IN ('filled','cancelled','rejected')", (bucket,))]
            if self.settings.parallel_paper:
                own = self.strategy_portfolio(strategy,bucket,db)
                own_active = [r for r in active if r['strategy']==strategy]
                if side == 'BUY':
                    pending = sum((r['qty']-r['filled'])*r['price']*r['multiplier']+r['fee'] for r in own_active if r['side']=='BUY')
                    marked = sum(p['qty']*p['mark']*p['multiplier'] for p in own['positions'].values())
                    if qty*price*multiplier+fee > min(own['cash']-pending,own['budget']-marked-pending)+1e-8:
                        raise ValueError('Strategy allocation exhausted')
                elif qty > own['positions'].get(symbol,{}).get('qty',0)-sum(r['qty']-r['filled'] for r in own_active if r['side']=='SELL' and r['symbol']==symbol):
                    raise ValueError('Strategy cannot sell another strategy position')
            if side == "BUY":
                reservations = sum((r["qty"]-r["filled"]) * r["price"] * r["multiplier"] + r["fee"] for r in active if r["side"] == "BUY")
                marked = sum(p["qty"] * p["mark"] * p["multiplier"] for p in portfolio["positions"].values())
                budget = self.settings.stock_budget if bucket == "stock" else self.settings.option_budget
                unsettled_row = db.execute("SELECT value FROM meta WHERE key='unsettled'").fetchone()
                unsettled = json.loads(unsettled_row[0]) if unsettled_row else []
                locked = sum(x["amount"] for x in unsettled if x["bucket"] == bucket and x["settles"] > utcnow())
                available = min(portfolio["cash"] - reservations - locked, budget - marked - reservations)
                if qty * price * multiplier + fee > available + 1e-8:
                    raise ValueError("Bucket cash/capital cap exceeded, including pending orders and fees")
            else:
                held = portfolio["positions"].get(symbol, {}).get("qty", 0)
                reserved = sum(r["qty"]-r["filled"] for r in active if r["side"] == "SELL" and r["symbol"] == symbol)
                if qty > held - reserved:
                    raise ValueError("Uncovered sell/duplicate sell reservation")
            db.execute("INSERT INTO orders(id,strategy,bucket,symbol,side,qty,price,multiplier,fee,status,created,session) VALUES (?,?,?,?,?,?,?,?,?,'reserved',?,?)",
                       (key,strategy,bucket,symbol,side,qty,price,multiplier,fee,utcnow(),session))
            self.event("reserved", {"id": key, "symbol": symbol, "side": side, "qty": qty}, db)
        return True

    def strategy_portfolio(self, strategy, bucket='stock', db=None):
        """Replay broker fill increments by owner; never use hypothetical fills."""
        if db is None:
            with self.transaction() as db:
                return self.strategy_portfolio(strategy,bucket,db)
        row=db.execute("SELECT value FROM meta WHERE key='paper_allocations'").fetchone()
        allocations=json.loads(row[0]) if row else {}
        budget=allocations.get(bucket,{}).get(strategy)
        if budget is None:
            raise ValueError('Strategy has no explicit allocation')
        funding_count=db.execute('SELECT COUNT(*) FROM strategy_funding WHERE bucket=?',(bucket,)).fetchone()[0]
        funded=float(db.execute('SELECT COALESCE(SUM(amount),0) FROM strategy_funding WHERE strategy=? AND bucket=?',(strategy,bucket)).fetchone()[0]) if funding_count else float(budget)
        initial_row=db.execute("SELECT value FROM meta WHERE key='strategy_initial_capital'").fetchone()
        initial=json.loads(initial_row[0]).get(strategy,float(budget)) if initial_row else float(budget)
        cash=funded;positions={};realized=fees=0.
        marks=self.portfolio(bucket,db)['positions']
        fills=db.execute('SELECT f.*,o.symbol,o.side,o.multiplier FROM fills f JOIN orders o ON o.id=f.order_id WHERE o.strategy=? AND o.bucket=? ORDER BY f.seq',(strategy,bucket))
        for f in fills:
            p=positions.setdefault(f['symbol'],{'qty':0,'basis':0.,'multiplier':f['multiplier'],'mark':f['price']})
            amount=f['qty']*f['price']*f['multiplier'];fees+=f['fee']
            if f['side']=='BUY':
                cash-=amount+f['fee'];p['qty']+=f['qty'];p['basis']+=amount+f['fee']
            else:
                if f['qty']>p['qty']:raise ValueError('Owner ledger contains uncovered sale')
                removed=p['basis']*f['qty']/p['qty'];p['basis']-=removed;p['qty']-=f['qty']
                cash+=amount-f['fee'];realized+=amount-f['fee']-removed
            p['mark']=marks.get(f['symbol'],{}).get('mark',f['price'])
        positions={s:p for s,p in positions.items() if p['qty']}
        held=sum(p['qty']*p['mark']*p['multiplier'] for p in positions.values())
        unrealized=sum(p['qty']*p['mark']*p['multiplier']-p['basis'] for p in positions.values())
        return {'cash':cash,'budget':float(budget),'initial_capital':initial,'net_funding':funded,
                'performance_nav':cash+held+initial-funded,'positions':positions,'nav':cash+held,
                'realized_pnl':realized,'unrealized_pnl':unrealized,'fees':fees}

    def attributed_portfolio(self,bucket='stock',db=None):
        """Presentation accounting; preserve each owner's remaining average basis.

        The physical bucket ledger and execution cash/reservations are unchanged.
        A pooled sale average can differ from the selling strategy's average.
        """
        if db is None:
            with self.transaction() as db:return self.attributed_portfolio(bucket,db)
        portfolio=self.portfolio(bucket,db)
        portfolio['realized_pnl']=db.execute('SELECT COALESCE(SUM(f.realized_pnl),0) FROM fills f JOIN orders o ON o.id=f.order_id WHERE o.bucket=?',(bucket,)).fetchone()[0]
        portfolio['cost_attribution']='pooled'
        if not self.settings.parallel_paper:return portfolio
        row=db.execute("SELECT value FROM meta WHERE key='paper_allocations'").fetchone()
        ids=json.loads(row[0]).get(bucket,{}) if row else {}
        owners={r[0] for r in db.execute('SELECT DISTINCT o.strategy FROM fills f JOIN orders o ON o.id=f.order_id WHERE o.bucket=?',(bucket,))}
        if owners-set(ids):
            portfolio['attribution_unavailable']=True
            return portfolio
        positions={};realized=0.
        for identity in ids:
            owner=self.strategy_portfolio(identity,bucket,db);realized+=owner['realized_pnl']
            for symbol,p in owner['positions'].items():
                combined=positions.setdefault(symbol,{**p,'qty':0,'basis':0.})
                combined['qty']+=p['qty'];combined['basis']+=p['basis']
        if {s:p['qty'] for s,p in positions.items()}!={s:p['qty'] for s,p in portfolio['positions'].items()}:
            portfolio['attribution_unavailable']=True
            return portfolio
        portfolio.update(positions=positions,realized_pnl=realized,cost_attribution='strategy')
        return portfolio

    def claim(self, key):
        with self.transaction() as db:
            return db.execute("UPDATE orders SET status='submitting' WHERE id=? AND status='reserved'", (key,)).rowcount == 1

    def submitted(self, key, broker_id):
        with self.transaction() as db:
            if db.execute("UPDATE orders SET status='submitted',broker_id=? WHERE id=? AND status IN ('submitting','unknown')", (str(broker_id),key)).rowcount != 1:
                raise ValueError("Order not awaiting acknowledgement")

    def unknown(self, key):
        with self.transaction() as db:
            db.execute("UPDATE orders SET status='unknown' WHERE id=? AND status='submitting'", (key,))
        self.pause("Order submission outcome unknown; never retry blindly")

    def fill(self, key, cumulative, average_price, status):
        """Book only the increment from cumulative broker fills; reservations survive partials."""
        import math
        if status not in ("submitted", "partial", "filled", "cancelled", "rejected") or type(cumulative) is not int:
            raise ValueError("Invalid broker state")
        with self.transaction() as db:
            row = dict(db.execute("SELECT * FROM orders WHERE id=?", (key,)).fetchone())
            if cumulative < row["filled"] or cumulative > row["qty"]:
                raise ValueError("Nonmonotonic or excessive cumulative fill")
            if status == "filled" and cumulative != row["qty"]:
                raise ValueError("Incomplete filled order")
            if cumulative and (not math.isfinite(average_price) or average_price <= 0 or
                               (row["side"] == "BUY" and average_price > row["price"] + 1e-8)):
                raise ValueError("Invalid fill or buy limit violated")
            value = cumulative * average_price * row["multiplier"] if cumulative else 0
            delta, amount = cumulative-row["filled"], value-row["fill_value"]
            if not delta and abs(amount) > 1e-7:
                raise ValueError("Fill correction requires explicit reconciliation")
            if delta:
                if amount <= 0:
                    raise ValueError("Invalid cumulative fill value")
                fee = amount * self.settings.fee_reserve_bps / 10000
                portfolio = self.portfolio(row["bucket"], db)
                p = portfolio["positions"].get(row["symbol"], {"qty":0,"basis":0})
                sign = 1 if row["side"] == "BUY" else -1
                cash = portfolio["cash"] - sign * amount - fee
                shares = p["qty"] + sign * delta
                if cash < -1e-7 or shares < 0:
                    raise ValueError("Broker fills violate cash-only portfolio")
                basis = p["basis"] + amount + fee if sign == 1 else p["basis"] * shares / p["qty"]
                realized=0 if sign==1 else amount-fee-p['basis']*delta/p['qty']
                db.execute('INSERT INTO fills(order_id,observed,qty,price,fee,realized_pnl) VALUES (?,?,?,?,?,?)',
                           (key,utcnow(),delta,amount/delta/row['multiplier'],fee,realized))
                db.execute("UPDATE balances SET cash=? WHERE bucket=?", (cash,row["bucket"]))
                db.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?)", (row["bucket"],row["symbol"],shares,basis,average_price,row["multiplier"]))
                if sign == -1:
                    from .exchange_clock import us_calendar
                    import pandas as pd
                    calendar = us_calendar()
                    day = pd.Timestamp(utcnow()).tz_convert("America/New_York").date()
                    session = calendar.date_to_session(str(day),direction="previous")
                    settles = calendar.session_open(calendar.next_session(session)).isoformat()
                    old = db.execute("SELECT value FROM meta WHERE key='unsettled'").fetchone()
                    unsettled = json.loads(old[0]) if old else []
                    unsettled = [x for x in unsettled if x["settles"] > utcnow()]
                    unsettled.append({"bucket":row["bucket"],"amount":amount-fee,"settles":settles})
                    self.set_meta("unsettled",unsettled,db)
                self.event("fill", {"id":key,"delta_qty":delta,"fee_reserve":fee,'realized_pnl':realized}, db)
            if row["status"] in ("filled","cancelled","rejected") and status != row["status"]:
                raise ValueError("Terminal order changed")
            db.execute("UPDATE orders SET status=?,filled=?,fill_value=? WHERE id=?", (status,cumulative,value,key))

    def snapshot(self):
        return {"created":utcnow(),"paused":self.meta("paused"),"pause_reason":self.meta("pause_reason"),
                "strategies": self.strategies(),"portfolios":{b:self.portfolio(b) for b in ("stock","option")},
                "orders":self.orders(),"live_enabled":False}

    def paper_activity(self):
        with self.transaction() as db:
            fills=[dict(r) for r in db.execute('SELECT f.*,o.symbol,o.side,o.bucket,o.session FROM fills f JOIN orders o ON o.id=f.order_id ORDER BY f.seq DESC LIMIT 100')]
            totals=dict(db.execute("SELECT COUNT(*) fill_count,COALESCE(SUM(f.fee),0) estimated_fees,COALESCE(SUM(f.realized_pnl),0) realized_pnl FROM fills f JOIN orders o ON o.id=f.order_id WHERE o.bucket='stock'").fetchone())
            # Keep chart size bounded while preserving first and last observations.
            rows=[dict(r) for r in db.execute('SELECT * FROM paper_nav ORDER BY observed')]
        stride=max(1,len(rows)//1000)
        curve=rows[::stride]
        if rows and (not curve or curve[-1]!=rows[-1]):curve.append(rows[-1])
        p=self.attributed_portfolio()
        totals['realized_pnl']=p['realized_pnl']
        nav=p['cash']+sum(r['qty']*r['mark']*r['multiplier'] for r in p['positions'].values())
        unrealized=sum(r['qty']*r['mark']*r['multiplier']-r['basis'] for r in p['positions'].values())
        return {**totals,'nav':nav,'cash':p['cash'],'unrealized_pnl':unrealized,
                'total_pnl':nav-self.settings.stock_budget,'return_pct':(nav/self.settings.stock_budget-1)*100,
                'fills':fills,'curve':curve,
                'cost_basis':'Average cost including estimated fees; 30bps default, not broker settlement costs',
                'time_basis':'observed is fill reconciliation time, not exact exchange execution time',
                'source':'Futunn simulated broker cumulative fills and REST valuation',
                'option':self.option_activity()}

    def option_activity(self):
        p=self.attributed_portfolio('option')
        with self.transaction() as db:
            totals=dict(db.execute("SELECT COUNT(*) fill_count,COALESCE(SUM(f.fee),0) estimated_fees,COALESCE(SUM(f.realized_pnl),0) realized_pnl FROM fills f JOIN orders o ON o.id=f.order_id WHERE o.bucket='option'").fetchone())
            curve=[dict(r) for r in db.execute('SELECT * FROM option_paper_nav ORDER BY observed')]
        totals['realized_pnl']=p['realized_pnl']
        nav=p['cash']+sum(r['qty']*r['mark']*100 for r in p['positions'].values())
        unrealized=sum(r['qty']*r['mark']*100-r['basis'] for r in p['positions'].values())
        return {**totals,'nav':nav,'cash':p['cash'],'unrealized_pnl':unrealized,
            'total_pnl':nav-self.settings.option_budget,'return_pct':(nav/self.settings.option_budget-1)*100,
            'positions':p['positions'],'curve':curve,'budget':self.settings.option_budget,
            'status':self.meta('option_paper_status'),'historical_option_backtest_verified':False}
