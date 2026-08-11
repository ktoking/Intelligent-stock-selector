#!/usr/bin/env python3
"""Cost-aware opening-gap continuation and fade research."""
from __future__ import annotations

import json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT=Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from scripts.okx_intraday_agent import OKX, settings  # noqa:E402
from scripts.okx_multitimeframe_backtest import CACHE_DIR, DEFAULT_SYMBOLS, aggregate_bars, load_market_data, weekday_sessions  # noqa:E402
from scripts.okx_research_universe import load_symbols  # noqa:E402
from scripts.okx_return_model_research import metric  # noqa:E402

NY=ZoneInfo('America/New_York'); OUTPUT=ROOT/'data'/'okx_gap_research_90d.json'
TRADE_SYMBOLS=[x for x in DEFAULT_SYMBOLS if not x.startswith(('SPY-','QQQ-'))]


def opportunities(
    raw,
    excluded_symbols: tuple[str, ...] = ("SPY-USDT-SWAP", "QQQ-USDT-SWAP"),
):
    """Build opening paths while retaining optional benchmark instruments."""
    records=[]
    for symbol,rows in raw.items():
        bars=aggregate_bars(rows,5)
        if not bars: continue
        frame=pd.DataFrame(bars,columns=['ts','open','high','low','close','volume','v1','v2','confirm'])
        frame['stamp']=pd.to_datetime(frame.ts.astype('int64'),unit='ms',utc=True)
        frame['local']=frame.stamp.dt.tz_convert(NY); frame['date']=frame.local.dt.date.astype(str)
        frame['clock']=frame.local.dt.strftime('%H:%M')
        for c in ('open','close'): frame[c]=pd.to_numeric(frame[c])
        by_date={d:g.set_index('clock') for d,g in frame.groupby('date')}
        dates=sorted(by_date)
        for i,date in enumerate(dates[1:],1):
            prev,day=by_date[dates[i-1]],by_date[date]
            if '15:55' not in prev.index or '09:30' not in day.index or '09:35' not in day.index: continue
            if '09:30' not in prev.index: continue
            previous_close=float(prev.loc['15:55'].close); previous_open=float(prev.loc['09:30'].open); open_price=float(day.loc['09:30'].open)
            entry=float(day.loc['09:35'].open); entry_time=int(day.loc['09:35'].ts)
            gap=(open_price/previous_close-1)*10000
            rec={'symbol':symbol,'date':date,'entry_time':entry_time,'entry':entry,'gap_bps':gap,
                 'first5_bps':(float(day.loc['09:30'].close)/open_price-1)*10000,
                 'previous_day_bps':(previous_close/previous_open-1)*10000,
                 'path_150':[(float(item.high),float(item.low),float(item.close))
                             for _,item in day.loc[(day.index>='09:35')&(day.index<='12:00')].iterrows()]}
            for minutes,clock in ((30,'10:00'),(60,'10:30'),(90,'11:00'),(120,'11:30'),(150,'12:00'),(385,'15:55')):
                if clock in day.index: rec[f'exit_{minutes}']=float(day.loc[clock].close)
            records.append(rec)
    result=pd.DataFrame(records)
    spy=result[result.symbol=='SPY-USDT-SWAP'][['date','gap_bps','first5_bps','previous_day_bps']].rename(
        columns={'gap_bps':'spy_gap','first5_bps':'spy_first5','previous_day_bps':'spy_previous_day'})
    return result[~result.symbol.isin(excluded_symbols)].merge(spy,on='date',how='left')


def portfolio(rows, score, style, horizon, threshold):
    data=rows.copy(); data['score']=score; data=data[abs(data.score)>=threshold]
    trades=[]
    for _,group in data.groupby('entry_time',sort=True):
        for _,row in group.reindex(group.score.abs().sort_values(ascending=False).index).head(5).iterrows():
            direction=(1 if row.score>0 else -1)*(1 if style=='continuation' else -1)
            ret=(float(row[f'exit_{horizon}'])/float(row.entry)-1)*10000
            trades.append({'entry_time':int(row.entry_time),'exit_time':int(row.entry_time+horizon*60000),
                           'side':'LONG' if direction>0 else 'SHORT','net_r':(direction*ret-14)/100})
    return trades


def confirmed_relative_fade(rows, horizon=150, threshold=100):
    score=rows.gap_bps-rows.spy_gap
    first5=rows.first5_bps-rows.spy_first5
    previous=rows.previous_day_bps-rows.spy_previous_day
    confirmed=(score*first5<0)|(score*previous<=0)
    return portfolio(rows[confirmed],score.loc[confirmed],'fade',horizon,threshold)


def protected_outcome(row, direction, stop_r, breakeven):
    """Conservative 5m replay: a same-bar hard stop precedes a new BE trigger."""
    activated=False
    for high,low,_ in row.path_150:
        favorable=((high/row.entry-1)*100 if direction>0 else (1-low/row.entry)*100)
        adverse=((low/row.entry-1)*100 if direction>0 else (1-high/row.entry)*100)
        if not activated and adverse<=-stop_r:
            return -stop_r-.14
        if activated and adverse<=0:
            return -.14
        if breakeven and favorable>=1:
            activated=True
    return direction*(row.path_150[-1][2]/row.entry-1)*100-.14


def protected_confirmed_trades(rows, stop_r, breakeven):
    score=rows.gap_bps-rows.spy_gap
    first5=rows.first5_bps-rows.spy_first5
    previous=rows.previous_day_bps-rows.spy_previous_day
    selected=rows[(score.abs()>=100)&((score*first5<0)|(score*previous<=0))].copy()
    selected['score']=score.loc[selected.index]
    trades=[]
    for _,group in selected.groupby('entry_time',sort=True):
        for _,row in group.reindex(group.score.abs().sort_values(ascending=False).index).head(5).iterrows():
            direction=-1 if row.score>0 else 1
            trades.append({'date':row.date,'symbol':row.symbol,'entry_time':int(row.entry_time),
                           'exit_time':int(row.entry_time+150*60000),
                           'side':'LONG' if direction>0 else 'SHORT',
                           'net_r':protected_outcome(row,direction,stop_r,breakeven)})
    return trades


def trailing_state_filter(trades, days, lookback):
    """Causal switch driven by prior paper outcomes, including disabled days."""
    daily={day:[] for day in days}
    for trade in trades: daily.setdefault(trade['date'],[]).append(trade)
    result=[]
    for index,day in enumerate(days):
        history=[trade['net_r'] for old in days[max(0,index-lookback):index] for trade in daily.get(old,[])]
        if index>=lookback and history and sum(history)/len(history)>0:
            result.extend(daily.get(day,[]))
    return result


def main():
    end=datetime.now(NY).date()-timedelta(days=1);sessions=weekday_sessions(end,90);days=[x.isoformat() for x in sessions]
    requested=load_symbols("historical_90d")
    symbols=tuple(symbol for symbol in requested if len(list(CACHE_DIR.glob(f"{symbol}_*_1m.json"))) >= len(sessions))
    raw=load_market_data(OKX(settings()),symbols,sessions); rows=opportunities(raw)
    specs=[]
    for signal in ('absolute','relative'):
      for style in ('continuation','fade'):
       for horizon in (30,60,90,120,150,385):
        for threshold in (50,75,100,125,150): specs.append((signal,style,horizon,threshold))
    results=[]
    for split,target in (('validation',days[40:50]),('development',days[50:60]),('final',days[60:90])):
      sub=rows[rows.date.isin(target)].dropna()
      for signal,style,horizon,threshold in specs:
        score=sub.gap_bps if signal=='absolute' else sub.gap_bps-sub.spy_gap
        available=sub[sub[f'exit_{horizon}'].notna()]; score=score.loc[available.index]
        results.append({'split':split,'signal':signal,'style':style,'horizon':horizon,'threshold':threshold,
                        **metric(portfolio(available,score,style,horizon,threshold))})
    grouped={}
    for row in results: grouped.setdefault((row['signal'],row['style'],row['horizon'],row['threshold']),{})[row['split']]=row
    eligible=[]
    for key,p in grouped.items():
      if all(x in p for x in ('validation','development','final')) and p['validation']['trades']>=30 and p['development']['trades']>=30 and p['validation']['net_r']>0 and p['development']['net_r']>0:
       eligible.append({'config':key,'validation':p['validation'],'development':p['development'],'final':p['final']})
    eligible.sort(key=lambda x:min(x['validation']['profit_factor'] or 0,x['development']['profit_factor'] or 0),reverse=True)
    # Freeze the broad-plateau configuration with more forward opportunities,
    # rather than the neighboring threshold that won by a few PF decimals.
    selected=next((row for row in eligible if tuple(row['config'])==('relative','fade',150,100)),None)
    forward=[]
    for split,target in (('validation',days[40:50]),('development',days[50:60]),('final_diagnostic',days[60:90])):
        sub=rows[rows.date.isin(target)].dropna()
        forward.append({'split':split,**metric(confirmed_relative_fade(sub))})
    protected=[]
    for stop_r,breakeven in ((1,False),(1,True),(1.5,False),(2,False),(3,False)):
        paper=protected_confirmed_trades(rows,stop_r,breakeven)
        for lookback in (5,10,15,20):
            enabled=trailing_state_filter(paper,days,lookback)
            split_metrics={name:metric([x for x in enabled if x['date'] in target]) for name,target in (
                ('train',days[:40]),('validation',days[40:50]),('development',days[50:60]),('final_diagnostic',days[60:90]))}
            protected.append({'stop_r':stop_r,'breakeven_at_1r':breakeven,'state_lookback_days':lookback,**split_metrics})
    protected.sort(key=lambda x:min(x['validation']['profit_factor'] or 0,x['development']['profit_factor'] or 0),reverse=True)
    out={'generated_at':datetime.now(timezone.utc).isoformat(),'symbols':list(symbols),'opportunities':len(rows),'tested':len(specs),'eligible':eligible[:20],
         'selected_diagnostic':selected,'final_diagnostic':selected['final'] if selected else None,'passed':False,
         'forward_candidate':{'experiment_id':'gap_relative_fade_confirm_e0936_quote_h150_t100_cost14_v3',
                              'rule':'relative gap >=100bp and (relative first5 reverses or previous-day relative return opposes gap)',
                              'historical_proxy':forward,'passed':False},
         'protected_state_diagnostics':protected,
         'warning':'diagnostic only; final dates were previously inspected'}
    OUTPUT.write_text(json.dumps(out,ensure_ascii=False,indent=2));print(json.dumps(out,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
