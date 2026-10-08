#!/usr/bin/env python3
"""Verify frozen SOXL orders in Backtesting.py's independent broker engine.

This is ex-post ledger reconciliation, NOT a fresh causal strategy test:
quantities come from the frozen tape. A synthetic settlement bar represents
the predeclared terminal closing auction; it adds no investment return.
"""
import argparse
from pathlib import Path
import json
import hashlib
import warnings
import sys

import numpy as np
import pandas as pd
from backtesting import Strategy
from backtesting.lib import FractionalBacktest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.equity_factor_lab import daily_data


def run(source,folder,name):
    daily_path=folder/f'{name}_primary_daily.csv'
    orders_path=folder/f'{name}_primary_orders.csv'
    daily=pd.read_csv(daily_path);orders=pd.read_csv(orders_path)
    p,_=daily_data(source)
    start=p['close'].index.get_loc(daily.date.iloc[0]);end=p['close'].index.get_loc(daily.date.iloc[-1])+1
    frame=pd.DataFrame({field.title():p[field].SOXL.iloc[start-2:end].to_numpy() for field in ('open','high','low','close','volume')},
                       index=pd.to_datetime(p['close'].index[start-2:end]))
    final_day=frame.index[-1]
    settle=final_day+pd.Timedelta(days=1)
    price=float(frame.Close.iloc[-1])
    frame.loc[settle]=[price,price,price,price,0.]
    schedule={}
    for row in orders.to_dict('records'):
        signal=str(row['signal_date']) if row['fill']=='next_open' else str(final_day.date())
        schedule.setdefault(signal,[]).append(row)
    unit=1e-8
    # Fixed one-cent cushion avoids all-or-nothing rejection due solely to
    # fractional-unit rounding of a fully invested frozen tape. It is removed
    # from every equity comparison and never changes the submitted quantities.
    rounding_cushion=.01
    class Tape(Strategy):
        def init(self):
            self.orders_submitted=0
        def next(self):
            day=str(self.data.index[-1].date())
            for order in schedule.get(day,[]):
                if order['fill']=='predeclared_final_close':
                    self.position.close()
                    continue
                quantity=int(np.floor(order['quantity']/unit))
                if quantity<=0:continue
                if order['side']=='SELL':
                    quantity=min(quantity,max(0,int(self.position.size)))
                    if quantity:self.sell(size=quantity)
                else:
                    self.buy(size=quantity)
                self.orders_submitted+=1
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter('always')
        result=FractionalBacktest(frame,Tape,fractional_unit=unit,cash=100_000.+rounding_cushion,commission=.0007,
                                  trade_on_close=False,hedging=False,exclusive_orders=False,finalize_trades=False).run()
    curve=result['_equity_curve']['Equity']-rounding_cushion
    dates=pd.to_datetime(daily.date.iloc[:-1])
    errors=curve.reindex(dates).to_numpy()-daily.equity.iloc[:-1].to_numpy()
    if not np.isfinite(errors).all():raise ValueError('framework equity dates are incomplete')
    expected=float(daily.equity.iloc[-1]);actual=float(result['Equity Final [$]'])-rounding_cushion
    messages=[str(w.message) for w in captured]
    passed=abs(expected-actual)<1 and float(np.max(np.abs(errors)))<1 and not messages
    report={'scope':'independent frozen-order ledger/mark-to-market/cost reconciliation, not independent signal validation',
            'symbol':'SOXL','fractional_unit':unit,'cash_tolerance_usd':1,
            'rounding_cash_cushion_subtracted_from_equity':rounding_cushion,
            'frozen_orders_sha256':hashlib.sha256(orders_path.read_bytes()).hexdigest(),
            'frozen_daily_sha256':hashlib.sha256(daily_path.read_bytes()).hexdigest(),
            'expected_final_equity':expected,'backtesting_final_equity':actual,'final_difference_usd':actual-expected,
            'max_intermediate_equity_difference_usd':float(np.max(np.abs(errors))),
            'frozen_order_count':len(orders),'submitted_nonterminal_orders':result['_strategy'].orders_submitted,
            'synthetic_settlement':'one no-return bar at last real close solely to execute terminal closing order',
            'warnings':messages,'passed':passed}
    (folder/'independent_ledger_check.json').write_text(json.dumps(report,indent=2,allow_nan=False))
    (folder/Path(__file__).name).write_bytes(Path(__file__).read_bytes())
    print(json.dumps(report,indent=2))
    if not passed:raise RuntimeError('independent order-tape check failed; inspect saved report')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,required=True);parser.add_argument('--folder',type=Path,required=True)
    parser.add_argument('--name',default='SOXL_ema20_100_vol60');args=parser.parse_args();run(args.source,args.folder,args.name)
