"""One causal daily decision function shared by research, shadow and paper.

Prices supplied to a decision are observed opens/quotes. Signals use completed
daily bars only. Corporate-action adjusted caches remain research-only online.
"""
from __future__ import annotations

from copy import deepcopy
import math

import numpy as np
import pandas as pd

from research.daily_factor_lab.core import market_exposure, metrics, rank, scheduled, weights_for


def empty(capital=10000):
    return {"cash":float(capital),"holdings":{},"peaks":{},"blocked":{},"sessions":0}


def equity(state, prices):
    return state["cash"] + sum(qty*prices[s] for s,qty in state["holdings"].items())


def plan(panel, signal_index, policy, state, session, prices, fee_bps=25, capital_cap=10000):
    if str(panel.dates[signal_index].date()) >= session:
        raise ValueError("Signal must precede execution session")
    for symbol in state["holdings"]:
        if symbol not in prices or not math.isfinite(prices[symbol]) or prices[symbol] <= 0:
            raise ValueError("Missing held-asset execution price")
    result = deepcopy(state)
    holdings, peaks, blocked = result["holdings"], result["peaks"], result["blocked"]
    f, i, tick = panel.fields, signal_index, state["sessions"]
    jmap = {s:j for j,s in enumerate(panel.symbols)}
    stop = set()
    for symbol in holdings:
        j = jmap[symbol]
        close = f["close"][i,j]
        if not np.isfinite(close):
            raise ValueError("Missing signal mark")
        peak = max(peaks.get(symbol,close), close)
        peaks[symbol] = float(peak)
        if close < f["ema20" if policy.trend == "fast" else "ema50"][i,j] or close < peak*(1-policy.trail):
            stop.add(symbol)
            blocked[symbol] = tick + policy.cooldown
    dates = panel.dates[:i+1].append(pd.DatetimeIndex([session]))
    rebalance = tick == 0 or scheduled(dates,len(dates)-1,policy.schedule)
    gross = market_exposure(panel,i,policy)
    desired = dict(holdings)
    for symbol in stop:
        desired[symbol] = 0
    nav = equity(state, prices)
    investable = min(nav,capital_cap)
    selected = []
    if rebalance or (policy.regime == "reduce" and gross < 1):
        ranked = rank(panel,i,policy)
        selected = [j for j in ranked[:policy.rank_buffer] if panel.symbols[j] in holdings and panel.symbols[j] not in stop][:policy.positions]
        if rebalance and gross > 0:
            selected += [j for j in ranked[:policy.positions] if j not in selected and panel.symbols[j] not in stop
                         and blocked.get(panel.symbols[j],0) <= tick][:policy.positions-len(selected)]
        if policy.regime == "gate" and gross == 0:
            selected = [j for j in selected if panel.symbols[j] in holdings]
        weights = weights_for(panel,i,selected,policy,gross if policy.regime == "reduce" else 1)
        desired = {}
        for j in selected:
            s = panel.symbols[j]
            price = prices.get(s)
            if price is None or not math.isfinite(price) or price <= 0:
                raise ValueError("Missing ranked-asset execution price")
            qty = int(investable*weights[j]/(price*(1+fee_bps/10000)))
            if gross == 0 or (not rebalance and gross < 1):
                qty = min(qty,holdings.get(s,0))
            previous = holdings.get(s,0)
            cap_breach = previous*price/max(nav,1) > policy.weight_cap + .001
            deviation = abs(qty-previous)*price/max(nav,1)
            desired[s] = qty if deviation >= policy.turnover_band or qty == 0 or previous == 0 or cap_breach else previous
        for s in stop:
            desired[s] = 0
    orders, cash = [], state["cash"]
    for s in sorted(holdings):
        qty = max(0,holdings[s]-desired.get(s,0))
        if qty:
            raw = float(prices[s]); fee = qty*raw*fee_bps/10000
            # Do not recycle sale proceeds into buys in the same session (T+1).
            orders.append({"date":session,"symbol":s,"side":"SELL","qty":qty,"raw_price":raw,"price":raw*(1-fee_bps/10000),"cost":fee,"policy":policy.name,"reason":"exit_or_rebalance"})
    for j in selected:
        s = panel.symbols[j]
        qty = max(0,desired.get(s,0)-holdings.get(s,0))
        if qty:
            raw = float(prices[s]); fill = raw*(1+fee_bps/10000)
            qty = min(qty,int(max(0,cash)/fill))
            if qty:
                cash -= qty*fill
                orders.append({"date":session,"symbol":s,"side":"BUY","qty":qty,"raw_price":raw,"price":fill,"cost":qty*raw*fee_bps/10000,"policy":policy.name,"reason":"rank_and_risk"})
    return orders, {"peaks":peaks,"blocked":blocked,"sessions":tick+1}


def apply(state, orders, controls):
    result = deepcopy(state)
    for o in orders:
        sign = 1 if o["side"] == "BUY" else -1
        result["cash"] -= sign*o["qty"]*o["raw_price"]+o["cost"]
        q = result["holdings"].get(o["symbol"],0)+sign*o["qty"]
        if q < 0 or result["cash"] < -1e-7:
            raise ValueError("Cash-only accounting violated")
        if q:
            if not result["holdings"].get(o["symbol"]):
                controls["peaks"][o["symbol"]] = o["raw_price"]
            result["holdings"][o["symbol"]] = q
        else:
            result["holdings"].pop(o["symbol"],None)
            controls["peaks"].pop(o["symbol"],None)
    result.update(controls)
    return result


def simulate(panel, policy, start, end, bps=25, signal_lag=1):
    state, orders, curve = empty(), [], []
    for i, day in enumerate(panel.dates):
        session = str(day.date())
        if not start <= session <= end:
            continue
        if i < max(200, signal_lag):
            raise ValueError("Need 200-session warmup")
        prices = {s:float(panel.fields["open"][i,j]) for j,s in enumerate(panel.symbols) if np.isfinite(panel.fields["open"][i,j])}
        daily, controls = plan(panel,i-signal_lag,policy,state,session,prices,bps)
        state = apply(state,daily,controls)
        close = {s:float(panel.fields["close"][i,j]) for j,s in enumerate(panel.symbols)}
        for s in state["holdings"]:
            if not math.isfinite(close[s]):
                raise ValueError("Missing held close or delisting treatment")
            state["peaks"][s] = max(state["peaks"].get(s,0),close[s])
        nav = equity(state,close)
        curve.append({"date":session,"equity":nav,"cash":state["cash"],"holdings":len(state["holdings"])})
        orders += daily
    if not curve:
        raise ValueError("Empty research interval")
    frame = pd.DataFrame(curve)
    return {"metrics":metrics(frame,orders),"curve":frame,"orders":orders,"state":state}
