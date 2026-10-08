import json

import numpy as np
import pandas as pd

from research.watchlist_daily_review import OUT, simulate_review
from research.watchlist_daily_trend import OUT as DATA_DIR, features, load


def test_saved_review_order_ledger_reconciles():
    curve = pd.read_csv(OUT / "daily_review_proxy_25bps_curve.csv")
    orders = pd.read_csv(OUT / "daily_review_proxy_25bps_orders.csv")
    closes = {
        symbol: pd.read_pickle(DATA_DIR / (symbol[3:] + ".pkl")).set_index("date")["close"]
        for symbol in orders.symbol.unique()
    }
    cash, qty = 10000.0, {}
    for row in curve.itertuples(index=False):
        day = str(row.date)[:10]
        for order in orders[orders.date == day].itertuples(index=False):
            direction = 1 if order.side == "BUY" else -1
            cash -= direction * order.qty * order.price
            qty[order.symbol] = qty.get(order.symbol, 0) + direction * order.qty
            if qty[order.symbol] == 0:
                del qty[order.symbol]
        key = int(day.replace("-", ""))
        equity = cash + sum(n * float(closes[symbol].loc[key]) for symbol, n in qty.items())
        assert np.isclose(equity, row.equity, atol=1e-6)
        assert np.isclose(cash, row.cash, atol=1e-6)
        assert len(qty) == row.holdings


def test_review_decisions_do_not_depend_on_later_prices():
    data = load("2024-09-01")
    cutoff = pd.Timestamp("2026-06-30")
    truncated = {symbol: frame.loc[:cutoff] for symbol, frame in data.items()}
    stock_types = json.loads((DATA_DIR / "security_types.json").read_text())
    days = [day for day in data["US.QQQ"].index if pd.Timestamp("2026-05-01") <= day <= cutoff]
    full = simulate_review(data, features(data), days, stock_types, 25, True)
    partial = simulate_review(truncated, features(truncated), days, stock_types, 25, True)
    assert full[3]
    assert full[3] == partial[3]
    np.testing.assert_allclose(full[1].equity, partial[1].equity, rtol=0, atol=1e-8)
