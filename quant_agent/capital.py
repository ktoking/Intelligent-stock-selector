"""Explicit simulated-account capital migration and cash-flow neutral performance."""
from dataclasses import asdict, replace
import json
import math

import pandas as pd

from .config import canonical, digest
from .store import utcnow

TREASURY='__account_treasury__'


def adjusted_curve(rows, flows, initial):
    """Time-weighted returns: a deposit/withdrawal is never investment profit."""
    flows=sorted(flows,key=lambda f:f['observed']);index=0;prior=float(initial);growth=1.;result=[]
    for row in rows:
        change=0.
        while index<len(flows) and flows[index]['observed']<=row['observed']:
            change+=flows[index]['amount'];index+=1
        nav=float(row['nav']);base=prior+change
        if base>1e-8:growth*=nav/base
        elif abs(nav)>1e-7:raise ValueError('NAV without attributable capital')
        result.append({**row,'performance_equity':initial*growth,'return_pct':(growth-1)*100,'external_flow':change})
        prior=nav
    return result


def strategy_performance(store,identity,owner):
    with store.transaction() as db:
        rows=[dict(r) for r in db.execute('SELECT * FROM strategy_paper_nav WHERE strategy=? ORDER BY observed',(identity,))]
        flows=[dict(r) for r in db.execute('SELECT observed,amount,kind FROM strategy_funding WHERE strategy=? ORDER BY observed,rowid',(identity,))]
    # The first funding establishes this owner's initial capital.
    if flows:
        first=flows[0]['observed'];seed=[f for f in flows if f['observed']==first]
        if math.isclose(sum(f['amount'] for f in seed),owner['initial_capital'],abs_tol=.001):
            flows=[f for f in flows if f['observed']!=first]
    rows.append({'observed':utcnow(),'nav':owner['nav'],'cash':owner['cash'],'session':'current'})
    curve=adjusted_curve(rows,flows,owner['initial_capital'])
    return curve[-1]['return_pct'],curve[:-1]


def update_broker_capital(store,remote):
    if store.settings.capital_mode!='broker_equity':return
    cash=float(remote['cash']['balance']);equity=float(remote['cash']['total_asset'])
    option=store.portfolio('option');reserved=max(store.settings.option_budget,
        option['cash']+sum(p['qty']*p['mark']*p['multiplier'] for p in option['positions'].values()))
    if not all(math.isfinite(v) and v>=0 for v in (cash,equity,reserved)):raise ValueError('Invalid broker capital')
    values={'stock_equity_cap':max(0,equity-reserved),'stock_cash_cap':max(0,cash-option['cash']),
        'option_reserve':reserved}
    if store.settings.option_funding_mode=='shared_ratio':
        stock=store.portfolio('stock');option_nav=option['cash']+sum(p['qty']*p['mark']*p['multiplier'] for p in option['positions'].values())
        target=equity*store.settings.option_equity_fraction
        values.update(option_equity_fraction=store.settings.option_equity_fraction,option_equity_cap=target,
            option_cash_cap=max(0,cash-stock['cash']),option_reserve=target,
            stock_equity_cap=max(0,min(equity-target,equity-option_nav)))
    store.set_meta('broker_capital',{'observed_at':utcnow(),'cash':cash,'total_asset':equity,**values,
        'source':'Futunn sim-trade cash-info; no margin buying power'})


def stock_cap(store):
    if store.settings.capital_mode=='fixed':return store.settings.stock_budget
    book=store.portfolio();nav=book['cash']+sum(p['qty']*p['mark'] for p in book['positions'].values())
    capital=store.meta('broker_capital') or {}
    # A persisted broker observation cannot authorize a new trading session.
    stamp=capital.get('observed_at')
    if not stamp or not 0<=(pd.Timestamp(utcnow())-pd.Timestamp(stamp)).total_seconds()<=120:return 0.
    return min(nav,capital.get('stock_equity_cap',0.))


def migrate_account_capital(store,broker,total_capital,authorization):
    """Operator-authorized expansion; broker and old fills/bases stay untouched.

    Caller holds executor_lock and stops the resident service. A new settings
    hash is committed with funding; recovery can write config from persisted
    capital_settings if interrupted before the atomic config-file replacement.
    """
    from .runner import reconcile
    if not authorization or not math.isfinite(total_capital) or total_capital<=store.settings.option_budget:
        raise ValueError('Explicit simulated capital authorization required')
    if store.settings.capital_mode=='broker_equity':return store.settings
    remote=reconcile(store,broker)
    if store.orders(active=True):raise ValueError('Resolve working orders before capital migration')
    new=replace(store.settings,stock_budget=total_capital-store.settings.option_budget,capital_mode='broker_equity')
    delta=new.stock_budget-store.settings.stock_budget
    if delta<=0:raise ValueError('Expansion must add capital')
    cash=store.portfolio()['cash']+store.portfolio('option')['cash']
    if cash+delta>float(remote['cash']['balance'])+1.:raise ValueError('Broker cash cannot fund expansion')
    stamp=utcnow();alloc=store.meta('paper_allocations',{})
    with store.transaction() as db:
        if db.execute("SELECT value FROM meta WHERE key='budget_hash'").fetchone()[0]!=digest([store.settings.stock_budget,store.settings.option_budget]):
            raise ValueError('Capital configuration changed')
        if db.execute("SELECT 1 FROM orders WHERE status NOT IN ('filled','cancelled','rejected')").fetchone():raise ValueError('Concurrent order')
        if not db.execute("SELECT 1 FROM strategy_funding WHERE bucket='stock'").fetchone():
            for identity,budget in alloc['stock'].items():
                db.execute('INSERT INTO strategy_funding VALUES (?,?,?,?,?,?)',('seed-'+identity,identity,'stock',budget,stamp,'initial'))
        alloc['stock'][TREASURY]=delta
        db.execute('INSERT INTO strategy_funding VALUES (?,?,?,?,?,?)',('account-expansion',TREASURY,'stock',delta,stamp,'account_expansion'))
        db.execute("UPDATE balances SET cash=cash+? WHERE bucket='stock'",(delta,))
        store.set_meta('capital_initial_budget',store.settings.stock_budget,db)
        store.set_meta('capital_flows',[{'observed':stamp,'amount':delta,'reason':authorization}],db)
        store.set_meta('paper_allocations',alloc,db)
        db.execute("UPDATE meta SET value=? WHERE key='budget_hash'",(digest([new.stock_budget,new.option_budget]),))
        store.set_meta('capital_settings',asdict(new),db)
        # Risk baselines adjust by the deposit, preserving the existing loss.
        for key in ('peak_nav','daily_nav'):
            row=db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
            if row:
                value=json.loads(row[0])
                if key=='peak_nav':value+=delta
                else:value['nav']+=delta
                store.set_meta(key,value,db)
        store.set_meta('preflight_session',None,db)
        store.event('account_capital_expanded',{'from_stock':store.settings.stock_budget,'to_stock':new.stock_budget,
            'option_reserve':new.option_budget,'external_funding':delta,'authorization':authorization,'live_enabled':False},db)
    store.settings=new
    temporary=store.home/'config.json.tmp';temporary.write_text(canonical(asdict(new)));temporary.chmod(0o600)
    temporary.replace(store.home/'config.json')
    update_broker_capital(store,remote)
    return new


def detach_unbound_option_capital(store,authorization):
    """Return an unused shared reserve; never invent a second broker balance.

    Caller stops the service and holds executor_lock. Binding a real independent
    account is a separate operation requiring its observed identity/assets.
    """
    if not authorization or store.settings.capital_mode!='broker_equity':raise ValueError('Account authorization required')
    if store.settings.option_funding_mode=='independent_pending':return store.settings
    if any(o['bucket']=='option' for o in store.orders()) or store.portfolio('option')['positions']:
        raise ValueError('Option trading history requires reconciliation before detaching')
    amount=store.portfolio('option')['cash'];stamp=utcnow()
    new=replace(store.settings,stock_budget=store.settings.stock_budget+amount,option_budget=0,
        option_funding_mode='independent_pending',option_paper_enabled=False)
    alloc=store.meta('paper_allocations',{});flows=store.meta('capital_flows',[])
    policy=store.meta('paper_authorization',{})
    # Reserve return can happen while a champion rotation is queued. Treasury
    # receives it until that workflow hands all cash to the winner.
    destination=store.meta('account_champion') or TREASURY
    from .champion import pending
    if pending(store):destination=TREASURY
    with store.transaction() as db:
        if db.execute("SELECT 1 FROM orders WHERE bucket='option'").fetchone():raise ValueError('Concurrent option activity')
        count=db.execute("SELECT COUNT(*) FROM strategy_funding WHERE bucket='option'").fetchone()[0]
        total=0.
        for identity,budget in alloc.get('option',{}).items():
            own=store.strategy_portfolio(identity,'option',db)
            if own['positions']:raise ValueError('Option positions remain')
            if not count:
                db.execute('INSERT INTO strategy_funding VALUES (?,?,?,?,?,?)',('seed-option-'+identity,identity,'option',budget,stamp,'initial'))
            total+=own['cash']
            db.execute('INSERT INTO strategy_funding VALUES (?,?,?,?,?,?)',('detach-option-'+identity,identity,'option',-own['cash'],stamp,'reserve_return'))
            alloc['option'][identity]=0.
        if not math.isclose(total,amount,abs_tol=.001):raise ValueError('Shared option funding does not reconcile')
        db.execute('INSERT INTO strategy_funding VALUES (?,?,?,?,?,?)',('returned-option-reserve',destination,'stock',amount,stamp,'reserve_return'))
        alloc['stock'][destination]=alloc['stock'].get(destination,0)+amount
        db.execute("UPDATE balances SET cash=0 WHERE bucket='option'")
        db.execute("UPDATE balances SET cash=cash+? WHERE bucket='stock'",(amount,))
        store.set_meta('paper_allocations',alloc,db)
        store.set_meta('capital_flows',flows+[{'observed':stamp,'amount':amount,'reason':'共享期权预留退回股票账户；非盈利'}],db)
        for key in ('peak_nav','daily_nav'):
            row=db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
            if row:
                value=json.loads(row[0])
                if key=='peak_nav':value+=amount
                else:value['nav']+=amount
                store.set_meta(key,value,db)
        db.execute("UPDATE meta SET value=? WHERE key='budget_hash'",(digest([new.stock_budget,new.option_budget]),))
        store.set_meta('capital_settings',asdict(new),db)
        store.set_meta('paper_authorization',{**policy,'stock_budget':new.stock_budget,'option_budget':None,'option_funding_mode':'independent_pending'},db)
        store.set_meta('option_account_status',{'status':'awaiting_binding','market':'US','observed_at':stamp,
            'account_id_verified':False,'balance_verified':False,'authorized':False,'source':authorization,
            'detail':'AppKey账户列表未列出独立美股期权模拟账户；等待账户身份。不会使用股票账户或虚构1000美元余额。'},db)
        store.set_meta('option_paper_status',{'status':'awaiting_binding','detail':'独立美股期权账户尚未绑定，执行关闭。行情研究仍可进行。'},db)
        store.set_meta('preflight_session',None,db)
        store.event('option_account_detached',{'returned_stock_cash':amount,'stock_budget':new.stock_budget,
            'independent_option_account_verified':False,'authorization':authorization,'orders_sent':0},db)
    store.settings=new
    temporary=store.home/'config.json.tmp';temporary.write_text(canonical(asdict(new)));temporary.chmod(0o600);temporary.replace(store.home/'config.json')
    return new


def option_cap(store):
    if store.settings.option_funding_mode!='shared_ratio':return store.settings.option_budget
    capital=store.meta('broker_capital') or {};stamp=capital.get('observed_at')
    if not stamp or not 0<=(pd.Timestamp(utcnow())-pd.Timestamp(stamp)).total_seconds()<=120:return 0.
    book=store.portfolio('option');nav=book['cash']+sum(p['qty']*p['mark']*p['multiplier'] for p in book['positions'].values())
    return min(nav,capital.get('option_equity_cap',0.))


def enable_shared_option_ratio(store,broker,authorization):
    """Explicit 90/10 funding mandate on the already bound simulated account.

    Stop the resident service and hold executor_lock. This migration does not
    submit orders, invent deposits, or modify the frozen stock strategy specs.
    """
    if not authorization or store.settings.capital_mode!='broker_equity':raise ValueError('Account authorization required')
    if store.settings.option_funding_mode=='shared_ratio':return store.settings
    if store.orders(active=True) or any(o['bucket']=='option' for o in store.orders()):
        raise ValueError('Existing option activity or working orders require reconciliation')
    total=store.settings.stock_budget+store.settings.option_budget
    option_budget=round(total*.1,2);stock_budget=total-option_budget
    new=replace(store.settings,stock_budget=stock_budget,option_budget=option_budget,
        option_funding_mode='shared_ratio',option_equity_fraction=.1,option_paper_enabled=True)
    if broker.settings!=new:raise ValueError('Verify capacity using the new simulated authorization')
    from .runner import reconcile
    remote=reconcile(store,broker)
    capability=store.meta('option_capability',{});code=capability.get('probe_contract')
    if not code:
        evidence=store.meta('option_data_status') or {};code=evidence.get('daily_contract')
    if not code:raise ValueError('No previously observed option contract for capacity verification')
    rows=broker.snapshots([code]);quote=next((r for r in rows if r.get('code')==code),None)
    if not quote or not quote.get('option_valid') or quote.get('option_contract_multiplier')!=100:
        raise ValueError('No verified standard option quote')
    price=float(quote.get('ask_price') or quote.get('last_price') or 0)
    if not math.isfinite(price) or price<=0:raise ValueError('No valid option reference price')
    capacity=broker.option_capacity(code,price)
    if float(capacity['max_cash_buy_qty_round_lot'])<1:raise ValueError('Simulated cash option capacity unavailable')
    from .option_paper import strategy_spec
    from .pipeline import artifact
    from .champion import pending
    spec=strategy_spec(new);identity=digest(spec);stamp=utcnow()
    alloc=store.meta('paper_allocations',{});old=store.meta('option_paper_strategy',{})
    if old:
        path=store.home/'artifacts'/('option-agent-'+old['id']+'.json')
        if not path.exists():artifact(store,'option-agent-'+old['id'],old)
    source=TREASURY if pending(store) else store.meta('account_champion') or TREASURY
    before=store.strategy_portfolio(source);needed=option_budget-store.portfolio('option')['cash']
    if needed<0 or before['cash']<needed or alloc['stock'].get(source,0)<needed:
        raise ValueError('Stock cash cannot fund 10 percent option allocation')
    flows=store.meta('capital_flows',[]);archives=store.meta('option_paper_archive',[])
    initial=store.meta('strategy_initial_capital',{});initial[identity]=option_budget
    policy=store.meta('paper_authorization',{})
    with store.transaction() as db:
        if db.execute("SELECT 1 FROM orders WHERE bucket='option' OR status NOT IN ('filled','cancelled','rejected')").fetchone():
            raise ValueError('Concurrent order activity')
        if store.portfolio('option',db)['positions']:raise ValueError('Option positions require reconciliation')
        if old and old.get('id') not in {a.get('id') for a in archives}:archives.append(old)
        # Old option attribution is already zero in independent_pending. An
        # unused legacy shared reserve is moved to the new owner explicitly.
        count=db.execute("SELECT COUNT(*) FROM strategy_funding WHERE bucket='option'").fetchone()[0]
        for owner,cap in alloc.get('option',{}).items():
            own=store.strategy_portfolio(owner,'option',db)
            if not count:db.execute('INSERT INTO strategy_funding VALUES (?,?,?,?,?,?)',('seed-option-'+owner,owner,'option',cap,stamp,'initial'))
            if own['cash']:
                db.execute('INSERT INTO strategy_funding VALUES (?,?,?,?,?,?)',('ratio-old-out-'+identity+owner,owner,'option',-own['cash'],stamp,'rotation'))
                db.execute('INSERT INTO strategy_funding VALUES (?,?,?,?,?,?)',('ratio-old-in-'+identity+owner,identity,'option',own['cash'],stamp,'rotation'))
            alloc['option'][owner]=0.
        for suffix,owner,bucket,value in [('out',source,'stock',-needed),('in',identity,'option',needed)]:
            db.execute('INSERT INTO strategy_funding VALUES (?,?,?,?,?,?)',('shared-ratio-'+identity+'-'+suffix,owner,bucket,value,stamp,'shared_ratio_allocation'))
        alloc['stock'][source]-=needed;alloc.setdefault('option',{})[identity]=option_budget
        db.execute("UPDATE balances SET cash=cash-? WHERE bucket='stock'",(needed,))
        db.execute("UPDATE balances SET cash=cash+? WHERE bucket='option'",(needed,))
        store.set_meta('paper_allocations',alloc,db);store.set_meta('strategy_initial_capital',initial,db)
        store.set_meta('capital_flows',flows+[{'observed':stamp,'amount':-needed,'reason':'股票资金划至同账户期权；非亏损'}],db)
        for key in ('peak_nav','daily_nav'):
            row=db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
            if row:
                value=json.loads(row[0])
                if key=='peak_nav':value=max(1.,value-needed)
                else:value['nav']=max(1.,value['nav']-needed)
                store.set_meta(key,value,db)
        db.execute("UPDATE meta SET value=? WHERE key='budget_hash'",(digest([new.stock_budget,new.option_budget]),))
        store.set_meta('capital_settings',asdict(new),db);store.set_meta('option_paper_archive',archives,db)
        store.set_meta('option_paper_strategy',{'id':identity,'spec':spec,'authorized':True,'source':authorization,'observed_at':stamp},db)
        store.set_meta('paper_authorization',{**policy,'stock_budget':stock_budget,'option_budget':option_budget,'option_funding_mode':'shared_ratio','option_equity_fraction':.1},db)
        store.set_meta('option_account_status',{'status':'shared_verified','market':'US','observed_at':stamp,
            'account_id_verified':True,'balance_verified':True,'authorized':True,'equity_fraction':.1,
            'detail':'与股票共用已核实的美股模拟账户；期权上限为最新净资产10%，独立核账，不使用融资。'},db)
        store.set_meta('option_capability',{**capability,'probe_contract':code,'reference_price':price,
            'observed_at':stamp,'capacity_verified':True,'order_endpoint_tested':False},db)
        store.set_meta('option_paper_status',{'status':'closed','budget':option_budget,'observed_at':stamp,
            'detail':'共用模拟账户10%额度已授权，正常开盘后检查信号和可执行报价。'},db)
        store.set_meta('preflight_session',None,db)
        store.event('shared_option_ratio_authorized',{'stock_budget':stock_budget,'option_budget':option_budget,
            'equity_fraction':.1,'same_simulated_account':True,'old_agent':old.get('id'),'new_agent':identity,
            'authorization':authorization,'orders_sent':0,'live_enabled':False},db)
    store.settings=new
    temporary=store.home/'config.json.tmp';temporary.write_text(canonical(asdict(new)));temporary.chmod(0o600);temporary.replace(store.home/'config.json')
    update_broker_capital(store,remote)
    return new
