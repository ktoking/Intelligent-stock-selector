"""Causal closed-5m decisions. Next-bar replay, cash settlement and auditable fills."""
from collections import defaultdict
from copy import deepcopy
import math

import pandas as pd

FAMILIES=('opening_range','vwap_pullback','trend_follow')


def decision(history,family,held=False):
    if family not in FAMILIES:raise ValueError('Unknown five-minute strategy')
    if len(history)<4:return 'WAIT'
    bars=history;last=bars[-1];close=last['close'];day=last['session']
    if any(r['session']!=day for r in bars):raise ValueError('Intraday indicators must reset at session open')
    closes=[r['close'] for r in bars];ema=[closes[0]]
    for value in closes[1:]:ema.append(.2*value+.8*ema[-1])
    volume=sum(r['volume'] for r in bars)
    vwap=sum((r['high']+r['low']+r['close'])/3*r['volume'] for r in bars)/volume if volume else close
    # Fifteen-minute trend confirmation uses only completed 3x5m groups.
    completed15=closes[2:len(closes)//3*3:3]
    trend15=len(completed15)>=2 and completed15[-1]>completed15[-2]
    if held:return 'SELL' if close<ema[-1] or close<vwap else 'HOLD'
    relative_volume=last['volume']/max(1,sum(r['volume'] for r in bars[-4:-1])/3)
    if family=='opening_range':entry=close>max(r['high'] for r in bars[:3]) and relative_volume>=1.2 and trend15
    elif family=='vwap_pullback':entry=bars[-2]['close']<=vwap<close and close>ema[-1] and trend15
    else:entry=len(closes)>=10 and closes[-2]<=ema[-2] and close>ema[-1] and close>vwap and trend15
    return 'BUY' if entry else 'WAIT'


def metrics(curve,trades,minute_curve=None):
    nav=[r['equity'] for r in curve];peak=10000.;dd=0.
    for point in minute_curve or curve:
        v=point['equity'];peak=max(peak,v);dd=max(dd,(1-v/peak)*100)
    return {'return_pct':(nav[-1]/10000-1)*100 if nav else 0.,'max_drawdown_pct':dd,
            'orders':len(trades),'trading_days':len(curve),'sharpe':0.}


def replay(data,family,days,cost_bps=30,delay_bars=0,signal_fn=None):
    """Signal close t -> actual open t+1 (+delay); no same-bar/overnight fills."""
    by_day=defaultdict(dict)
    for symbol,rows in data['bars'].items():
        for r in rows:
            if r['session'] in days:by_day[r['session']].setdefault(symbol,[]).append(r)
    cash=10000.;trades=[];curve=[];minute_curve=[];fee=cost_bps/10000
    for day in sorted(days):
        series=by_day[day];timeline=sorted({r['end_ms'] for bars in series.values() for r in bars})
        held={};pending=[];locked=0.;history={s:[] for s in series};marks={};attempted=set()
        index={s:{r['end_ms']:r for r in bars} for s,bars in series.items()}
        for i,t in enumerate(timeline):
            current={s:bars[t] for s,bars in index.items() if t in bars}
            for intent in list(pending):
                due,symbol,side=intent
                if due!=i:continue
                pending.remove(intent);bar=current.get(symbol)
                if not bar:continue
                price=bar['open']
                if side=='SELL' and symbol in held:
                    qty,basis=held.pop(symbol);value=qty*price;cash+=value*(1-fee);locked+=value*(1-fee)
                    trades.append({'session':day,'time_ms':t-300000,'symbol':symbol,'side':side,'qty':qty,'price':price,'fee':value*fee,'pnl':value*(1-fee)-basis})
                elif side=='BUY' and symbol not in held and len(held)<2:
                    # At most 40% gross, one attempt/name/day; sale proceeds
                    # remain unavailable until the following session.
                    nav=cash+sum(q*marks.get(s,series[s][0]['open']) for s,(q,_) in held.items())
                    budget=min(max(0,cash-locked),nav*.2);qty=int(budget/(price*(1+fee)))
                    if qty:
                        value=qty*price;cash-=value*(1+fee);held[symbol]=(qty,value*(1+fee))
                        trades.append({'session':day,'time_ms':t-300000,'symbol':symbol,'side':side,'qty':qty,'price':price,'fee':value*fee,'pnl':0.})
            for symbol,bar in current.items():history[symbol].append(bar);marks[symbol]=bar['close']
            for symbol in sorted(current):
                own=held.get(symbol);signal=(signal_fn(symbol,history[symbol],bool(own)) if signal_fn else decision(history[symbol],family,bool(own)));bar=current[symbol]
                # At 15:50 close request exit, next 15:50-15:55 bar open fills.
                exit_due=i>=len(timeline)-3-delay_bars
                if own and (exit_due or signal=='SELL' or bar['close']<=own[1]/own[0]*.985 or bar['close']>=own[1]/own[0]*1.03):
                    if not any(p[1]==symbol for p in pending):pending.append((i+1+delay_bars,symbol,'SELL'))
                elif not own and not exit_due and i<len(timeline)-6 and signal=='BUY' and symbol not in attempted:
                    attempted.add(symbol);pending.append((i+1+delay_bars,symbol,'BUY'))
            if cash < -1e-7:raise ValueError('Minute replay cash negative')
            nav=cash+sum(q*marks[s] for s,(q,_) in held.items())
            minute_curve.append({'date':pd.Timestamp(t,unit='ms',tz='UTC').isoformat(),'session':day,'equity':nav})
        if held:raise ValueError('Minute replay has an unclosed position or missing exit bars')
        curve.append({'date':day,'equity':cash})
    # Independent cash ledger recomputation, using raw trade prices/fees.
    verified=10000.
    positions=defaultdict(int)
    for t in trades:
        sign=1 if t['side']=='BUY' else -1
        verified-=sign*t['qty']*t['price']+t['fee'];positions[t['symbol']]+=sign*t['qty']
        if verified< -1e-7 or positions[t['symbol']]<0:raise ValueError('Independent minute ledger mismatch')
    audited=math.isclose(verified,cash,abs_tol=1e-7) and not any(positions.values())
    if not audited:raise ValueError('Independent minute cash mismatch')
    return {'metrics':metrics(curve,trades,minute_curve),'curve':curve,'minute_curve':minute_curve,'trades':trades,'independent_ledger':audited,
            'execution':'closed 5m signal; next bar open; T+1 sale proceeds; max 2 positions x20%; no overnight'}
