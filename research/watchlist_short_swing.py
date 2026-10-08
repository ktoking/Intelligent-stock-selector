"""Exploratory short swing research on cached Futunn daily bars; no orders."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from research.watchlist_daily_trend import OUT as DATA_OUT, buy_hold, load

OUT = Path('outputs/watchlist_short_swing_20260923')
VARIANTS = {
    'trend20': {'momentum': 20, 'max_days': 8, 'trail': .08},
    'breakout20': {'momentum': 20, 'max_days': 10, 'trail': .08},
    'trend10': {'momentum': 10, 'max_days': 6, 'trail': .06},
}


def features(data):
    ans = {}
    for symbol, f in data.items():
        c = f.close
        ans[symbol] = pd.DataFrame({
            'close': c,
            'ema10': c.ewm(span=10, adjust=False, min_periods=10).mean(),
            'ema20': c.ewm(span=20, adjust=False, min_periods=20).mean(),
            'ema60': c.ewm(span=60, adjust=False, min_periods=60).mean(),
            'mom10': c / c.shift(10) - 1,
            'mom20': c / c.shift(20) - 1,
            'vol20': c.pct_change().rolling(20, min_periods=20).std(),
            'liquid20': (c * f.volume).rolling(20, min_periods=20).mean(),
            'prior20high': f.high.shift().rolling(20, min_periods=20).max(),
        }, index=f.index)
    return ans


def rank(feats, types, day, variant):
    market = feats['US.QQQ'].loc[day]
    if pd.isna(market.ema60) or market['close'] <= market.ema60:
        return []
    ranked = []
    mom_col = 'mom' + str(VARIANTS[variant]['momentum'])
    for symbol, f in feats.items():
        if types.get(symbol) != 'STOCK' or day not in f.index:
            continue
        r = f.loc[day]
        if any(pd.isna(r[x]) for x in ('ema60', mom_col, 'vol20', 'liquid20', 'prior20high')):
            continue
        if r.vol20 <= 0 or r.liquid20 < 5_000_000 or r['close'] < 5 or r[mom_col] <= 0:
            continue
        eligible = r['close'] > r.ema20 > r.ema60
        if variant == 'trend10':
            eligible = eligible and r['close'] > r.ema10 > r.ema20
        if variant == 'breakout20':
            eligible = eligible and r['close'] > r.prior20high
        if eligible:
            ranked.append((float(r[mom_col] / r.vol20), symbol))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    return [symbol for _, symbol in ranked[:5]]


def simulate(data, feats, types, days, variant, bps=10):
    config = VARIANTS[variant]
    calendar = list(data['US.QQQ'].index)
    first, last = days[0], days[-1]
    cash = 10000.0
    held = {}
    curve, trades, orders = [], [], []
    for i, day in enumerate(calendar):
        if day < first or day > last:
            continue
        prev = calendar[i - 1]
        rebalance = day.weekday() in (0, 3)
        targets = rank(feats, types, prev, variant) if rebalance else []
        exiting = []
        for symbol, pos in held.items():
            p = feats[symbol].loc[prev]
            if (p['close'] < p.ema10 or p['close'] < pos['peak'] * (1 - config['trail'])
                    or i - pos['entry_index'] >= config['max_days']
                    or (rebalance and symbol not in targets)):
                exiting.append(symbol)
        for symbol in exiting:
            pos = held.pop(symbol)
            fill = float(data[symbol].loc[day, 'open']) * (1 - bps / 10000)
            cash += pos['qty'] * fill
            trades.append({'symbol': symbol, 'entry_date': str(pos['entry_day'].date()),
                           'exit_date': str(day.date()), 'pnl': pos['qty'] * (fill - pos['entry'])})
            orders.append({'date': str(day.date()), 'symbol': symbol, 'side': 'SELL',
                           'qty': pos['qty'], 'price': fill})
        if rebalance:
            equity_open = cash + sum(pos['qty'] * float(data[s].loc[day, 'open']) for s, pos in held.items())
            for symbol in targets:
                if symbol in held:
                    continue
                fill = float(data[symbol].loc[day, 'open']) * (1 + bps / 10000)
                prior_vol = float(feats[symbol].loc[prev, 'vol20'])
                target_weight = min(.20, .01 / prior_vol)
                qty = int(min(cash, equity_open * target_weight) / fill)
                if qty < 1:
                    continue
                cash -= qty * fill
                held[symbol] = {'qty': qty, 'entry': fill, 'entry_day': day,
                                'entry_index': i, 'peak': float(feats[symbol].loc[prev, 'close'])}
                orders.append({'date': str(day.date()), 'symbol': symbol, 'side': 'BUY',
                               'qty': qty, 'price': fill})
        equity = cash
        for symbol, pos in held.items():
            close = float(data[symbol].loc[day, 'close'])
            equity += pos['qty'] * close
            pos['peak'] = max(pos['peak'], close)
        curve.append({'date': str(day.date()), 'equity': equity, 'cash': cash,
                      'holdings': len(held)})
    frame = pd.DataFrame(curve)
    values = np.r_[10000., frame.equity.to_numpy()]
    stats = {'return_pct': float((values[-1] / values[0] - 1) * 100),
             'max_drawdown_pct': float((1 - values / np.maximum.accumulate(values)).max() * 100),
             'closed_trades': len(trades), 'orders': len(orders),
             'end_holdings': len(held), 'trading_days': len(frame),
             'win_rate': sum(t['pnl'] > 0 for t in trades) / len(trades) if trades else None}
    if len(frame) > 42:
        rolling = (frame.equity / frame.equity.shift(42) - 1).dropna() * 100
        stats['rolling_42d_pct'] = {'min': float(rolling.min()),
                                    'median': float(rolling.median()), 'max': float(rolling.max())}
    return stats, frame, trades, orders


def research():
    OUT.mkdir(parents=True, exist_ok=True)
    data = load()
    feats = features(data)
    types = json.loads((DATA_OUT / 'security_types.json').read_text())
    calendar = list(data['US.QQQ'].index)
    periods = {'train': ('2024-05-01', '2025-03-31'),
               'validation': ('2025-04-01', '2025-12-31'),
               'test': ('2026-01-01', '2026-09-22')}
    days = {label: [d for d in calendar if pd.Timestamp(start) <= d <= pd.Timestamp(end)]
            for label, (start, end) in periods.items()}
    report = {'status': 'retrospective exploratory test; current watchlist survivor bias and analyst has previously viewed 2026 results',
              'source': 'Futunn REST split-adjusted daily bars; current US watchlist, ordinary stocks only',
              'universe': len(data), 'ordinary_stock_count': sum(types.get(s) == 'STOCK' for s in data),
              'periods': periods, 'cost_each_side_bps': [10, 25], 'variants': {}, 'benchmarks': {}}
    for label, ds in days.items():
        report['benchmarks'][label] = {s: buy_hold(data, s, ds) for s in ('US.QQQ', 'US.SPY')}
    for name in VARIANTS:
        report['variants'][name] = {}
        for label, ds in days.items():
            report['variants'][name][label] = {}
            for cost in (10, 25):
                stats, curve, trades, orders = simulate(data, feats, types, ds, name, cost)
                report['variants'][name][label][str(cost)] = stats
                if label == 'test':
                    curve.to_csv(OUT / f'{name}_test_{cost}bps_curve.csv', index=False)
                    pd.DataFrame(trades).to_csv(OUT / f'{name}_test_{cost}bps_trades.csv', index=False)
                    pd.DataFrame(orders).to_csv(OUT / f'{name}_test_{cost}bps_orders.csv', index=False)
    (OUT / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    research()
