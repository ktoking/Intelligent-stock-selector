import numpy as np
import pandas as pd
import pytest

from scripts.preopen_cash_engine import plan_orders, settlement_sessions, simulate


def panels(values, columns=("A",)):
    close = pd.DataFrame(values, index=pd.bdate_range("2024-06-03", periods=len(values)).strftime("%Y-%m-%d"), columns=columns, dtype=float)
    return {"open": close.copy(), "close": close.copy()}


def test_cash_is_reserved_at_limit_price_including_cost():
    sell, buy, limits = plan_orders([100, 100], [1, 0], 100, 0, [.25, .75], .05, 20)
    assert sell[0] == .5
    assert np.dot(buy, limits)*1.002 == pytest.approx(100)
    assert buy[1] < 1  # Proceeds from the planned sell are not included.


def test_gap_changes_fill_but_not_preplanned_quantity():
    p = panels([[100], [100], [100]])
    w = pd.DataFrame(1., index=p['close'].index, columns=['A'])
    div = w*0
    a = simulate(p, w, div, 1, 3, initial_equity=1000)
    p['open'].iloc[1, 0] = 110
    b = simulate(p, w, div, 1, 3, initial_equity=1000)
    assert a['orders'][0]['quantity'] == b['orders'][0]['quantity']
    assert a['orders'][0]['status'] == 'filled'
    assert b['orders'][0]['status'] == 'expired_open_above_limit'
    assert b['daily'][0]['cash'] == 1000


def test_rotation_sale_cannot_fund_same_auction_buy():
    p = panels([[100, 100]]*5, ('A', 'B'))
    w = p['close']*0
    w.iloc[0,0] = 1
    w.iloc[1:,1] = 1
    r = simulate(p, w, w*0, 1, 5, gap_limit=0, cost_bps=0, initial_equity=1000)
    orders = [o for o in r['orders'] if o['side']=='BUY' and o['symbol']=='B']
    assert orders[0]['date'] == p['close'].index[3]
    assert r['daily'][1]['cash'] == 0
    assert r['daily'][1]['receivables'] == 1000


def test_dividend_belongs_to_prior_holder_and_remains_receivable():
    p = panels([[100], [100], [99], [99]])
    w = p['close']*0+1
    d = w*0; d.iloc[2,0] = 1
    r = simulate(p, w, d, 1, 4, gap_limit=0, cost_bps=0, initial_equity=1000, distribution_delay=20)
    assert r['dividend_entitlement_usd'] == 10
    assert r['daily'][1]['equity'] == 1000
    assert r['daily'][1]['cash'] == 0
    assert r['daily'][1]['receivables'] == 10
    assert r['daily'][-1]['equity'] == 1000


def test_historical_settlement_regimes():
    assert settlement_sessions('2017-09-01') == 3
    assert settlement_sessions('2017-09-05') == 2
    assert settlement_sessions('2024-05-24') == 2
    assert settlement_sessions('2024-05-28') == 1
