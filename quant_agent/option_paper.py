"""Small long-option forward paper experiment; no historical option PASS claim."""
from types import SimpleNamespace
from datetime import date, timedelta
from hashlib import sha256
import math
from pathlib import Path

import pandas as pd

from .broker import BrokerError, executable_quote
from .config import digest
from .store import utcnow

NAME='option-trend-long-v1'


def strategy_spec(settings):
    spec={'name':NAME,'underlyings':['US.NVDA','US.QQQ'],'long_only':True,'max_contracts':1,
          'budget':settings.option_budget,'dte':[14,28],'delta':[.35,.60],'minimum_oi':500,
          'max_spread_bps':settings.option_max_spread_bps,'stop_loss_pct':30,'take_profit_pct':50,
          'exit_before_expiry_days':7,'historical_option_backtest_verified':False,
          'engine_sha256':sha256(Path(__file__).read_bytes()).hexdigest()}
    if settings.option_funding_mode=='shared_ratio':
        spec.update(funding_mode='shared_ratio',equity_fraction=settings.option_equity_fraction)
    return spec


def activate_option_agent(store, authorization):
    if not store.settings.option_paper_enabled or not authorization:
        raise ValueError('Explicit option paper authorization required')
    spec=strategy_spec(store.settings)
    ident=digest(spec)
    if store.portfolio('option')['positions'] or any(o['bucket']=='option' for o in store.orders()):
        raise ValueError('Cannot reset an option strategy with trading activity')
    allocation=store.meta('paper_allocations',{});allocation['option']={ident:store.settings.option_budget}
    with store.transaction() as db:
        store.set_meta('paper_allocations',allocation,db)
        store.set_meta('option_paper_strategy',{'id':ident,'spec':spec,'authorized':True,'source':authorization,'observed_at':utcnow()},db)
        store.event('option_paper_authorized',{'id':ident,'name':NAME,'budget':store.settings.option_budget,'historical_backtest_verified':False},db)
    return ident


def direction(panel, symbol):
    if symbol not in panel.symbols or len(panel.dates)<51:return None
    j=panel.symbols.index(symbol);f=panel.fields
    close=float(f['close'][-1,j]);past=float(f['close'][-6,j])
    fast=float(f['ema20'][-1,j]);slow=float(f['ema50'][-1,j])
    if not all(math.isfinite(x) and x>0 for x in (close,past,fast,slow)):return None
    change=close/past-1
    typ='CALL' if close>fast>slow and change>0 else 'PUT' if close<fast<slow and change<0 else None
    return {'symbol':symbol,'type':typ,'strength':abs(change),'signal_date':str(panel.dates[-1].date())} if typ else None


def option_quote(q, settings):
    if not q.get('option_valid') or q.get('option_contract_multiplier')!=100 or q.get('option_contract_size')!=100 or not math.isfinite(float(q.get('last_price') or 0)) or float(q.get('last_price') or 0)<=0:
        raise BrokerError('Only standard 100-share option contracts are authorized')
    return executable_quote(q,SimpleNamespace(max_spread_bps=settings.option_max_spread_bps,
        max_quote_age_seconds=settings.max_quote_age_seconds))


def candidates(chain, quotes, signal, session, settings, capital_cap=None):
    """Actual chain metadata and fresh executable quotes; no fabricated premiums."""
    selected=[]
    for c in chain:
        q=quotes.get(c['code'])
        dte=(date.fromisoformat(c['strike_time'])-date.fromisoformat(session)).days
        if not q or c.get('stock_owner')!=signal['symbol'] or c.get('option_type')!=signal['type'] or c.get('lot_size')!=100 or c.get('option_standard_type')!='STANDARD' or not 14<=dte<=28:
            continue
        try:bid,ask=option_quote(q,settings)
        except (BrokerError,ValueError,KeyError,TypeError):continue
        delta=abs(float(q.get('delta') or 0));oi=float(q.get('option_open_interest') or 0)
        if not .35<=delta<=.60 or oi<500 or ask*100*(1+settings.fee_reserve_bps/10000)>(settings.option_budget if capital_cap is None else capital_cap):
            continue
        selected.append({'code':c['code'],'expiry':c['strike_time'],'type':c['option_type'],
            'underlying':signal['symbol'],'bid':bid,'ask':ask,'delta':delta,'open_interest':oi,'dte':dte,
            'score':abs(delta-.45)+(ask-bid)/ask})
    return sorted(selected,key=lambda r:(r['score'],r['code']))


def submit(store,broker,owner,code,side,qty,price,session):
    capacity=broker.option_capacity(code,price)
    maximum=float(capacity['max_cash_buy_qty_round_lot' if side=='BUY' else 'max_sell_qty_round_lot'])
    if qty>maximum:raise BrokerError('Simulated option capacity insufficient')
    attempts=[r for r in store.orders() if r['bucket']=='option' and r['strategy']==owner and r['session']==session and r['symbol']==code and r['side']==side]
    if len(attempts)>=3:raise BrokerError('Option order retry cap reached; manual review needed')
    key=digest([NAME,owner,session,code,side,len(attempts)])[:32]
    existing=next((r for r in store.orders() if r['id']==key),None)
    if existing:
        if existing['status']!='reserved':return
    else:
        store.reserve(key,owner,'option',code,side,qty,price,session,multiplier=100)
        existing=next(r for r in store.orders() if r['id']==key)
    if store.claim(key):
        try:store.submitted(key,broker.submit(existing))
        except Exception:
            store.unknown(key);raise


def option_tick(store,broker,panel,quotes,session):
    allocations={k:v for k,v in store.meta('paper_allocations',{}).get('option',{}).items() if v>0}
    if not allocations:return
    owner=next(iter(allocations));book=store.portfolio('option')
    agent=store.meta('option_paper_strategy',{})
    if owner!=agent.get('id') or sha256(Path(__file__).read_bytes()).hexdigest()!=agent.get('spec',{}).get('engine_sha256'):
        raise BrokerError('Frozen option experiment changed; review before execution')
    base={'name':NAME,'observed_at':utcnow(),'historical_option_backtest_verified':False,
          'budget':store.settings.option_budget,'long_only':True,'status':'closed' if not session else 'monitoring',
          'rules':'NVDA/QQQ 已完成日线趋势与5日方向；14–28天标准期权，Delta 0.35–0.60，OI≥500，价差≤3%，最多1张；-30%/+50%或到期前7天平仓。'}
    from .capital import option_cap
    base['capital_cap']=option_cap(store)
    if not session:
        store.set_meta('option_paper_status',base);return
    if store.meta('paused'):return
    if str(panel.dates[-1].date())>=session:
        raise BrokerError('Option signal is not from a completed prior session')
    working=[o for o in store.orders(active=True) if o['bucket']=='option']
    if working:
        for o in working:
            if o['status']=='reserved':
                broker.option_capacity(o['symbol'],o['price'])
                if store.claim(o['id']):
                    try:store.submitted(o['id'],broker.submit(o))
                    except Exception:
                        store.unknown(o['id']);raise
        store.set_meta('option_paper_status',base|{'status':'working','detail':'等待已有期权委托核对'});return
    contracts=store.meta('option_contracts',{})
    for code,p in book['positions'].items():
        meta=contracts.get(code)
        if not meta:raise BrokerError('Missing held option contract metadata')
        q=quotes.get(code)
        if not q:raise BrokerError('Missing option position quote')
        bid,ask=option_quote(q,store.settings)
        average=p['basis']/p['qty']/100
        sig=direction(panel,meta['underlying'])
        dte=(date.fromisoformat(meta['expiry'])-date.fromisoformat(session)).days
        if bid<=average*.70 or bid>=average*1.50 or dte<=7 or not sig or sig['type']!=meta['type']:
            submit(store,broker,owner,code,'SELL',p['qty'],math.floor(bid*100)/100,session)
            base.update(status='exit_submitted',detail='满足退出条件，已提交模拟限价平仓')
        else:base.update(status='holding',detail='持有期权，监控退出条件')
        store.set_meta('option_paper_status',base);return
    if store.meta('option_entry_session')==session:
        store.set_meta('option_paper_status',base|{'status':'waiting','detail':'本交易日已尝试一次开仓'});return
    # Do not chase a signal all day; entry follows the daily stock decision window.
    from .broker import session_now
    import exchange_calendars as xc
    if pd.Timestamp.now(tz='UTC')>xc.get_calendar('XNYS').session_open(session)+pd.Timedelta(minutes=5):
        store.set_meta('option_paper_status',base|{'status':'waiting','detail':'每日开仓窗口已结束；继续监控持仓'});return
    signals=[s for sym in ['US.NVDA','US.QQQ'] if (s:=direction(panel,sym))]
    if not signals:
        store.set_meta('option_paper_status',base|{'status':'waiting','detail':'没有满足日线趋势的期权观察方向'});return
    signal=max(signals,key=lambda s:s['strength'])
    if quotes.get(signal['symbol'],{}).get('data_date')!=session:
        raise BrokerError('Underlying quote is not current-session')
    start=str(date.fromisoformat(session)+timedelta(days=14));end=str(date.fromisoformat(session)+timedelta(days=28))
    chain=broker.request('GET','/api/v1.0/quote/'+signal['symbol']+'/option-chain',{'start':start,'end':end,'filter_standard':'STANDARD'})['data']['option_chain']
    near=[r for r in chain if r.get('option_type')==signal['type'] and r.get('lot_size')==100]
    spot=float(quotes[signal['symbol']]['last_price'])
    near=sorted(near,key=lambda r:abs(r['strike_price']/spot-1))[:30]
    snapshot={q['code']:q for q in broker.snapshots([r['code'] for r in near])} if near else {}
    choices=candidates(near,snapshot,signal,session,store.settings,capital_cap=base['capital_cap'])
    if not choices:
        store.set_meta('option_paper_status',base|{'status':'waiting','detail':'无合约通过真实报价、流动性或当前期权资金上限检查'});return
    selected=choices[0];code=selected['code'];price=math.ceil(selected['ask']*100)/100
    contracts[code]=selected;store.set_meta('option_contracts',contracts)
    # One paid premium position at a time; cash/fees are reserved by the shared ledger.
    submit(store,broker,owner,code,'BUY',1,price,session)
    store.set_meta('option_entry_session',session)
    store.set_meta('option_paper_status',base|{'status':'entry_submitted','detail':'已提交1张模拟限价买入','contract':code,
                    'signal_date':signal['signal_date'],'expiry':selected['expiry']})
