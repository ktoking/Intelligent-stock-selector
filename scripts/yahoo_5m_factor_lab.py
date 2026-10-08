#!/usr/bin/env python3
"""Chronological, shared-account five-minute multi-factor research pilot."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import exchange_calendars as xcals
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.equity_factor_lab import metrics


def load_data(path, symbols=None):
    raw = pd.read_pickle(path)
    calendar = xcals.get_calendar('XNYS')
    result = {}
    expected = None
    for symbol in (symbols if symbols is not None else ('SOXL', 'TQQQ', 'SMH', 'QQQ')):
        frame = raw[symbol].copy().sort_index()
        if frame.index.tz is None or frame.index.has_duplicates:
            raise ValueError('minute timestamps require timezone and uniqueness')
        frame.index = frame.index.tz_convert('UTC')
        sessions = calendar.sessions_in_range(frame.index[0].date(), frame.index[-1].date())
        index = pd.DatetimeIndex([t for d in sessions for t in pd.date_range(
            calendar.session_open(d), calendar.session_close(d), freq='5min', inclusive='left')])
        if frame.index.tolist() != index.tolist():
            raise ValueError(f'{symbol}: missing/extra regular-session bars; do not fill missing prices')
        if expected is not None and index.tolist() != expected.tolist():
            raise ValueError('symbols have different coverage')
        expected = index
        cols = ['open', 'high', 'low', 'close', 'volume']
        frame = frame[cols].astype(float)
        if not np.isfinite(frame.to_numpy()).all() or (frame[cols[:4]] <= 0).any().any() or (frame.volume < 0).any():
            raise ValueError('invalid minute prices or volumes')
        if (frame.high < frame[['open','close','low']].max(axis=1)).any() or (frame.low > frame[['open','close','high']].min(axis=1)).any():
            raise ValueError('invalid OHLC range')
        result[symbol] = frame
    return result


def closed_15m_trend(frame):
    # A 09:30--09:45 bar is unavailable until 09:45. Never backward-fill it.
    grouped = frame['close'].resample('15min', closed='left', label='right').agg(['last','count'])
    close = grouped.loc[grouped['count'] == 3, 'last']
    active = (close.ewm(span=9, adjust=False).mean() > close.ewm(span=21, adjust=False).mean()).astype(float)
    aligned = active.reindex(frame.index+pd.Timedelta(minutes=5), method='ffill').fillna(0.)
    aligned.index = frame.index
    return aligned


def features(data, pairs=None):
    result = {}
    for symbol, reference in (pairs if pairs is not None else (('SOXL','SMH'), ('TQQQ','QQQ'))):
        f = data[symbol]
        local = f.index.tz_convert('America/New_York')
        day, slot = local.strftime('%Y-%m-%d'), local.strftime('%H:%M')
        typical = (f.high+f.low+f.close)/3
        vwap = (typical*f.volume).groupby(day).cumsum()/f.volume.groupby(day).cumsum().replace(0,np.nan)
        normal_volume = f.volume.groupby(slot).transform(lambda x:x.shift(1).rolling(20,min_periods=3).median())
        relative_volume = f.volume/normal_volume.replace(0,np.nan)
        change = f.close.diff()
        up, down = change.clip(lower=0).rolling(14).mean(), -change.clip(upper=0).rolling(14).mean()
        rsi = 100*up/(up+down).replace(0,np.nan)
        atr = pd.concat([f.high-f.low, (f.high-f.close.shift()).abs(), (f.low-f.close.shift()).abs()],axis=1).max(axis=1).rolling(14).mean()
        ema = f.close.ewm(span=9,adjust=False).mean()
        factors = pd.DataFrame({'trend15':closed_15m_trend(f), 'vwap':(f.close>vwap).astype(float),
            'relative_volume':(relative_volume>=1.2).astype(float),
            'momentum':((rsi>=50)&(rsi<=75)).astype(float),
            'reference_trend':closed_15m_trend(data[reference])},index=f.index)
        out = factors.copy()
        out['score'] = factors.sum(axis=1)
        out['stop_fraction'] = (1.5*atr/f.close).clip(.003,.03)
        out['breakout'] = f.close > f.high.groupby(day).transform(lambda x:x.shift(1).rolling(3).max())
        out['pullback'] = (f.low <= ema)&(f.close > ema)&(f.close > f.close.shift())
        out['previous_score'] = out.score.groupby(day).shift(1).fillna(0)
        out['entry_window'] = (slot >= '09:50')&(slot <= '14:55')
        result[symbol] = out
    return result


def signal(frame, config):
    active = (frame.score >= config['threshold']) & frame.entry_window & frame.stop_fraction.notna()
    family = config['family']
    if family == 'score_rise':
        return active & (frame.previous_score < config['threshold'])
    return active & frame[family]


def stop_fill(open_, high, low, stop, target):
    if open_ <= stop:
        return open_, 'gap_stop'
    if open_ >= target:
        return target, 'target'  # No favorable gap improvement assumed.
    if low <= stop:
        return stop, 'stop_first' if high >= target else 'stop'
    if high >= target:
        return target, 'target'
    return None, None


def simulate(data, feats, config, days, *, cost_bps=7., slippage_bps=2., initial_equity=100_000.,
             max_holding_bars=None, reward_multiple=2., symbols=None, minimum_fee_usd=0., max_daily_entries=3, daily_profit_pause=.02):
    if daily_profit_pause is not None and (not np.isfinite(daily_profit_pause) or daily_profit_pause<=0):
        raise ValueError('profit pause must be positive or None')
    if not isinstance(max_daily_entries,int) or max_daily_entries < 1:
        raise ValueError('daily entry limit must be a positive integer')
    if minimum_fee_usd < 0 or initial_equity <= 0 or cost_bps < 0 or slippage_bps < 0:
        raise ValueError('invalid capital or cost assumptions')
    if max_holding_bars is not None and max_holding_bars < 1:
        raise ValueError('holding period must be positive')
    symbols = tuple(symbols) if symbols is not None else ('SOXL','TQQQ')
    index = data[symbols[0]].index
    date_labels = index.tz_convert('America/New_York').strftime('%Y-%m-%d')
    signals = {s:signal(feats[s],config).to_numpy() for s in symbols}
    arrays = {s:data[s][['open','high','low','close']].to_numpy() for s in symbols}
    cash = initial_equity
    fee, slip = cost_bps/10_000, slippage_bps/10_000
    daily, trades, bars = [], [], []
    for day in days:
        ix = np.flatnonzero(date_labels == day)
        start_cash = cash
        position = None
        pending_exit = False
        halted = False
        entered = 0
        costs = traded = exposure = 0.
        for n,i in enumerate(ix):
            exited = False
            if position is None and not halted and n > 0 and entered < max_daily_entries:
                candidates = [s for s in symbols if signals[s][i-1]]
                if candidates:
                    s = max(candidates, key=lambda s:(feats[s].score.iloc[i-1],s))
                    stop_pct = float(feats[s].stop_fraction.iloc[i-1])
                    # Quantity is determined from the completed signal bar, then
                    # reserved at a 3% higher limit before the next bar opens.
                    limit = arrays[s][i-1,3]*1.03
                    qty = min(cash/(limit*(1+fee)), max(0.,cash-minimum_fee_usd)/limit,
                              cash*config['risk']/ (limit*stop_pct))
                    price = arrays[s][i,0]*(1+slip)
                    if price <= limit and qty > 1e-8:
                        value = qty*price
                        entry_cost = max(value*fee,minimum_fee_usd)
                        cash -= value+entry_cost
                        costs += entry_cost; traded += value
                        position = dict(symbol=s,qty=qty,entry=price,entry_cost=entry_cost,
                            stop=price*(1-stop_pct),target=price*(1+reward_multiple*stop_pct) if reward_multiple is not None else None,
                            entry_index=i,
                            entry_time=str(index[i]),signal_time=str(index[i-1]+pd.Timedelta(minutes=5)))
                        entered += 1
            if position is not None:
                s = position['symbol']
                op,hi,lo,cl = arrays[s][i]
                if pending_exit:
                    price,reason = op,'daily_limit_next_open'
                elif max_holding_bars is not None and i-position['entry_index'] >= max_holding_bars:
                    price,reason = op,'holding_limit_next_open'
                else:
                    price,reason = stop_fill(op,hi,lo,position['stop'],position['target'] if position['target'] is not None else np.inf)
                if price is None and n == len(ix)-1:
                    price,reason = cl,'session_close'
                if price is not None:
                    price *= (1-slip)
                    value = position['qty']*price
                    exit_cost = max(value*fee,minimum_fee_usd)
                    cash += value-exit_cost
                    costs += exit_cost; traded += value
                    pnl = position['qty']*(price-position['entry'])-position['entry_cost']-exit_cost
                    trades.append({**position,'exit_time':str(index[i]),'exit_price':price,'exit_cost':exit_cost,'pnl':pnl,'reason':reason,'day':day})
                    position = None; pending_exit = False; exited = True
            equity = cash + (position['qty']*arrays[position['symbol']][i,3] if position else 0.)
            weight = (equity-cash)/equity
            exposure += weight
            if (daily_profit_pause is not None and equity/start_cash-1 >= daily_profit_pause) or equity/start_cash-1 <= -.02:
                halted = True
                if position is not None:pending_exit = True
            if cash < -1e-6 or equity <= 0:
                raise ValueError('account constraint violated')
            bars.append({'time':str(index[i]+pd.Timedelta(minutes=5)), 'equity':float(equity),'cash':float(cash)})
        daily.append({'date':day,'return':cash/start_cash-1,'equity':float(cash),'cash':float(cash),
            'gross_weight':exposure/len(ix),'turnover':traded/start_cash,'cost':costs})
    summary = metrics(daily)
    curve = np.r_[initial_equity,[b['equity'] for b in bars]]
    summary['max_drawdown_5m_close_pct'] = float((1-curve/np.maximum.accumulate(curve)).max()*100)
    summary['trades'] = len(trades)
    summary['win_rate'] = sum(t['pnl']>0 for t in trades)/len(trades) if trades else None
    return dict(metrics=summary,daily=daily,trades=trades,bars=bars)


def configurations():
    return [dict(name=f'{family}_score{threshold}_risk{int(risk*10000)}',family=family,threshold=threshold,risk=risk)
        for family in ('breakout','pullback','score_rise') for threshold in (3,4) for risk in (.005,.01)]


def run(source,output):
    if output.exists():raise FileExistsError('use a new research output')
    data = load_data(source)
    days = list(dict.fromkeys(data['SOXL'].index.tz_convert('America/New_York').strftime('%Y-%m-%d')))
    if len(days)<35:raise ValueError('insufficient sessions even for a small pilot')
    # Five warmup days; chronological 20/8/8 for the present 41-session data.
    usable = days[5:]
    final_n = max(7,len(usable)//5)
    val_n = max(7,len(usable)//5)
    partitions = {'train':usable[:-final_n-val_n], 'validation':usable[-final_n-val_n:-final_n], 'final':usable[-final_n:]}
    output.mkdir(parents=True)
    configs = configurations()
    manifest = {'created_at':datetime.now(timezone.utc).isoformat(),'source':str(source),
        'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'configurations':configs,'partitions':partitions,
        'bar_interval':'5m','factor_intervals':['5m','15m'],'symbols':['SOXL','TQQQ'],'references':['SMH','QQQ'],
        'cost_bps_per_side':7,'slippage_bps_per_side':2,'max_trades_per_day':3,'max_positions':1,
        'daily_profit_pause':.02,'daily_loss_pause':-.02,'cash_reuse':'immediate sale proceeds; buying-power assumption, not strict settled-cash simulation',
        'selection':'at least15 training trades and5 validation trades; maximize lower train/validation daily Sharpe; no final-data selection',
        'history_status':'Short pilot, not one year. End-period held out for this minute experiment; daily market history previously inspected.'}
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2))
    (output/Path(__file__).name).write_bytes(Path(__file__).read_bytes())
    feats = features(data)
    board = []
    for config in configs:
        row = {'config':config}
        for label in ('train','validation'):
            r=simulate(data,feats,config,partitions[label]);row[label]=r['metrics']
            pd.DataFrame(r['daily']).to_csv(output/f"{config['name']}_{label}_daily.csv",index=False)
        board.append(row)
        print(config['name'],f"train={row['train']['return_pct']:.2f}% val={row['validation']['return_pct']:.2f}%",flush=True)
    eligible = [r for r in board if r['train']['trades']>=15 and r['validation']['trades']>=5]
    selected = max(eligible,key=lambda r:(min(r[x]['sharpe_zero_rf'] for x in ('train','validation')),r['config']['name'])) if eligible else None
    frozen = {'selected':selected,'board':board,'final_evaluated':False}
    (output/'selection_before_final.json').write_text(json.dumps(frozen,indent=2))
    final = None
    if selected:
        final = {}
        for cost in (7,15):
            r=simulate(data,feats,selected['config'],partitions['final'],cost_bps=cost)
            final[f'cost{cost}']=r['metrics']
            for item in ('daily','trades','bars'):
                pd.DataFrame(r[item]).to_csv(output/f'selected_final_cost{cost}_{item}.csv',index=False)
    report = {'manifest':manifest,'board':board,'selected':selected,'final':final,
        'code_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'promotion_passed':False,'execution_enabled':False,
        'limitations':['Annualization of a few days is a mathematical extrapolation, not a validated annual return.',
                       'Opening-bar full fills and fixed slippage idealized; no bid-ask depth or strict settled-cash modeling.',
                       'Stops first when stop and target both occur in one candle; 5m-close drawdown excludes intrabar account troughs.',
                       'Long-only shared account, fractional units, no external leverage; leveraged ETF risks remain.']}
    (output/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False))
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();run(args.source,args.output)
