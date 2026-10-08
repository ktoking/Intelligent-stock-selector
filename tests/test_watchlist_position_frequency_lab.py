import json

import numpy as np
import pandas as pd

from research.watchlist_daily_trend import OUT as DATA_DIR, features, load, simulate
from research.watchlist_position_frequency_lab import OUT, simulate_variant


def test_saved_heavy_twice_weekly_ledger_reconciles():
    curve = pd.read_csv(OUT / "recent_2026_heavy_twice_weekly_25bps_curve.csv")
    orders = pd.read_csv(OUT / "recent_2026_heavy_twice_weekly_25bps_orders.csv")
    closes = {
        symbol: pd.read_pickle(DATA_DIR / (symbol[3:] + ".pkl")).set_index("date")["close"]
        for symbol in orders.symbol.unique()
    }
    cash, held = 10000.0, {}
    for row in curve.itertuples(index=False):
        day = str(row.date)[:10]
        for order in orders[orders.date == day].itertuples(index=False):
            direction = 1 if order.side == "BUY" else -1
            cash -= direction * order.qty * order.price
            held[order.symbol] = held.get(order.symbol, 0) + direction * order.qty
            if held[order.symbol] == 0:
                del held[order.symbol]
        assert cash >= -1e-8
        key = int(day.replace("-", ""))
        equity = cash + sum(qty * float(closes[symbol].loc[key])
                            for symbol, qty in held.items())
        assert np.isclose(equity, row.equity, atol=1e-6)
        assert np.isclose(cash, row.cash, atol=1e-6)
        assert len(held) == row.holdings


def test_weekly_baseline_matches_frozen_engine_and_no_future_data():
    data = load("2024-09-01")
    stock_types = json.loads((DATA_DIR / "security_types.json").read_text())
    cutoff = pd.Timestamp("2026-06-30")
    days = [day for day in data["US.QQQ"].index
            if pd.Timestamp("2026-05-01") <= day <= cutoff]
    feats = features(data)
    reference = simulate(data, feats, days, "dual_ema", 25, stock_types, .01)
    reproduced = simulate_variant(data, feats, days, stock_types, 25, .2, .01, (0,))
    assert reproduced[3] == reference[3]
    np.testing.assert_allclose(reproduced[1].equity, reference[1].equity, atol=1e-8, rtol=0)

    truncated = {symbol: frame.loc[:cutoff] for symbol, frame in data.items()}
    future_free = simulate_variant(truncated, features(truncated), days,
                                   stock_types, 25, .3, .015, (0, 3))
    full = simulate_variant(data, feats, days, stock_types, 25, .3, .015, (0, 3))
    assert full[3] == future_free[3]
    np.testing.assert_allclose(full[1].equity, future_free[1].equity, atol=1e-8, rtol=0)
