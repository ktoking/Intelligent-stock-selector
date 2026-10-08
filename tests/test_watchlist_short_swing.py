import json

import pandas as pd

from research.watchlist_daily_trend import OUT as DATA_OUT, load
from research.watchlist_short_swing import OUT, features, simulate


def test_saved_short_swing_ledger_reconciles():
    orders = pd.read_csv(OUT / 'trend20_test_10bps_orders.csv')
    curve = pd.read_csv(OUT / 'trend20_test_10bps_curve.csv')
    closes = {s: pd.read_pickle(DATA_OUT / (s[3:] + '.pkl')).set_index('date')['close']
              for s in orders.symbol.unique()}
    cash, held = 10000.0, {}
    for row in curve.itertuples(index=False):
        for order in orders[orders.date == row.date].itertuples(index=False):
            if order.side == 'BUY':
                cash -= order.qty * order.price
                held[order.symbol] = held.get(order.symbol, 0) + order.qty
            else:
                cash += order.qty * order.price
                held[order.symbol] -= order.qty
                if not held[order.symbol]:
                    del held[order.symbol]
        key = int(row.date.replace('-', ''))
        equity = cash + sum(qty * float(closes[s].loc[key]) for s, qty in held.items())
        assert abs(cash - row.cash) < 1e-6
        assert abs(equity - row.equity) < 1e-6
        assert len(held) == row.holdings


def test_short_swing_first_month_orders_do_not_use_later_bars():
    full = load()
    subset = {s: f for s, f in full.items() if s in ('US.QQQ', 'US.NVDA', 'US.AAPL')}
    short = {s: f.loc[:'2026-06-30'] for s, f in subset.items()}
    days = [d for d in short['US.QQQ'].index
            if pd.Timestamp('2026-06-01') <= d <= pd.Timestamp('2026-06-30')]
    types = json.loads((DATA_OUT / 'security_types.json').read_text())
    a = simulate(subset, features(subset), types, days, 'trend20')[3]
    b = simulate(short, features(short), types, days, 'trend20')[3]
    assert a
    assert a == b
