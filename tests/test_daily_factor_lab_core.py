"""Economic invariants for the local daily portfolio simulator."""
from dataclasses import replace
import json

import numpy as np
import pandas as pd
import pytest

from research.daily_factor_lab.core import (
    Panel, Policy, benchmark, load_panel, scheduled, simulate, weights_for,
)


def synthetic_panel():
    dates = pd.bdate_range("2025-01-01", periods=95)
    phase = np.arange(len(dates), dtype=float)
    closes = np.column_stack([
        100 + phase * .15 + np.sin(phase) * .4,
        70 + phase * .08 + np.cos(phase) * .2,
        200 + phase * .10 + np.sin(phase * .7) * .3,
    ])
    returns = np.vstack([np.zeros(3), closes[1:] / closes[:-1] - 1])
    fields = {
        "open": closes.copy(), "close": closes.copy(),
        "returns": returns, "vol20": np.full_like(closes, .02),
        "liquid20": np.full_like(closes, 20_000_000.),
        "mom63": np.full_like(closes, .2),
        "momentum_risk": np.tile([3., 2., 1.], (len(dates), 1)),
        "ema20": closes * .96, "ema50": closes * .92,
        "ema60": closes * .90, "ema100": closes * .88,
        "ema150": closes * .80, "ema200": closes * .75,
    }
    return Panel(dates, ["US.AAA", "US.BBB", "US.QQQ"],
                 np.array([True, True, False]), fields, {})


def simple_policy(**changes):
    # A very high risk budget isolates execution and accounting from risk sizing.
    return replace(Policy("test", positions=2, weight_cap=.4,
                          annual_vol_target=10., rank_buffer=2,
                          schedule="daily", turnover_band=0.), **changes)


def test_order_cash_shares_and_mark_to_market_reconcile():
    panel = synthetic_panel()
    result = simulate(panel, simple_policy(), panel.dates[65], panel.dates[90], bps=25)
    assert result["orders"]
    cash, shares, total_cost = 10000., np.zeros(3, dtype=int), 0.
    for row in result["curve"].itertuples(index=False):
        day_orders = [o for o in result["orders"] if o["date"] == row.date]
        for order in day_orders:
            j = panel.symbols.index(order["symbol"])
            direction = 1 if order["side"] == "BUY" else -1
            expected_cost = order["qty"] * order["raw_price"] * .0025
            assert order["cost"] == pytest.approx(expected_cost)
            assert order["price"] == pytest.approx(order["raw_price"] * (1 + direction * .0025))
            cash -= direction * order["qty"] * order["raw_price"] + expected_cost
            shares[j] += direction * order["qty"]
            total_cost += expected_cost
            assert cash >= -1e-8
            assert (shares >= 0).all()
        i = panel.dates.get_loc(pd.Timestamp(row.date))
        equity = cash + shares @ panel.fields["close"][i]
        assert row.cash == pytest.approx(cash, abs=1e-8)
        assert row.equity == pytest.approx(equity, abs=1e-8)
        assert row.holdings == np.count_nonzero(shares)
    assert result["metrics"]["cost_dollars"] == pytest.approx(total_cost)
    assert result["metrics"]["return_pct"] == pytest.approx((equity / 10000 - 1) * 100)
    actual_final = {p["symbol"]: p["qty"] for p in result["positions"]}
    expected_final = {s: int(q) for s, q in zip(panel.symbols, shares) if q}
    assert actual_final == expected_final


def test_trailing_stop_blocks_same_open_reentry_and_cooldown():
    panel = synthetic_panel()
    # AAA remains highly ranked and above EMA50 while falling from its held peak.
    panel.fields["open"][72:, 0] *= .75
    panel.fields["close"][72:, 0] *= .75
    for key in ("ema20", "ema50", "ema60", "ema100", "ema150", "ema200"):
        panel.fields[key][72:, 0] *= .75
    result = simulate(panel, simple_policy(cooldown=2), panel.dates[70], panel.dates[78])
    orders = [o for o in result["orders"] if o["symbol"] == "US.AAA"]
    stop_day = str(panel.dates[73].date())
    stopped = [o for o in orders if o["date"] == stop_day and o["side"] == "SELL"]
    assert len(stopped) == 1
    assert stopped[0]["reason"] == "trend_or_trailing_exit"
    forbidden = {str(panel.dates[i].date()) for i in (73, 74)}
    assert not [o for o in orders if o["side"] == "BUY" and o["date"] in forbidden]
    assert any(o["side"] == "BUY" and o["date"] == str(panel.dates[75].date()) for o in orders)


def test_holidays_move_weekly_and_thursday_rebalances_to_next_session():
    # Monday Jan 20 and Thursday Jan 23 are omitted to exercise both holidays.
    dates = pd.DatetimeIndex(["2025-01-17", "2025-01-21", "2025-01-22",
                              "2025-01-24", "2025-01-27"])
    weekly = [scheduled(dates, i, "weekly") for i in range(1, len(dates))]
    twice = [scheduled(dates, i, "twice") for i in range(1, len(dates))]
    assert weekly == [True, False, False, True]
    assert twice == [True, False, True, True]


def test_reduce_mode_defensive_nonscheduled_day_only_sells():
    panel = synthetic_panel()
    start = next(i for i in range(60, 70) if panel.dates[i].weekday() == 0)
    # At Monday's close QQQ turns below EMA100 but remains above EMA200.
    panel.fields["ema100"][start + 1:, 2] = panel.fields["close"][start + 1:, 2] * 1.1
    policy = simple_policy(schedule="weekly", regime="reduce")
    result = simulate(panel, policy, panel.dates[start], panel.dates[start + 4])
    assert any(o["side"] == "BUY" for o in result["orders"])
    decisions = {d["date"]: d for d in result["decisions"]}
    defensive_orders = [o for o in result["orders"]
                        if not decisions[o["date"]]["rebalance"]
                        and decisions[o["date"]]["regime_exposure"] < 1]
    assert defensive_orders, "Fixture must exercise an actual defensive trim"
    assert all(o["side"] == "SELL" for o in defensive_orders)


def write_cache(directory):
    dates = pd.bdate_range("2024-01-02", periods=300)
    symbols = ["US.AAA", "US.BBB", "US.QQQ"]
    (directory / "universe.json").write_text(json.dumps({"symbols": symbols}))
    (directory / "security_types.json").write_text(json.dumps(
        {"US.AAA": "STOCK", "US.BBB": "STOCK", "US.QQQ": "ETF"}))
    for j, symbol in enumerate(symbols):
        t = np.arange(len(dates), dtype=float)
        close = (30 + j * 80) * np.exp(.0015 * t + .012 * np.sin(t * (.13 + j * .01)))
        frame = pd.DataFrame({"date": dates.strftime("%Y%m%d").astype(int),
                              "open": close * .999, "high": close * 1.02,
                              "low": close * .98, "close": close,
                              "volume": 1_000_000., "turnover": 30_000_000.})
        frame.to_pickle(directory / (symbol[3:] + ".pkl"))
    return dates


def test_future_data_and_missing_future_bar_do_not_change_historical_trades(tmp_path):
    dates = write_cache(tmp_path)
    start, cutoff = dates[190], dates[240]
    original = load_panel(tmp_path)
    policy = simple_policy(schedule="twice")
    reference = simulate(original, policy, start, cutoff)
    assert reference["orders"], "Fixture needs trades to establish causal invariance"
    truncated = load_panel(tmp_path, asof=cutoff)
    prefix = simulate(truncated, policy, start, cutoff)
    assert reference["orders"] == prefix["orders"]
    np.testing.assert_allclose(reference["curve"].equity, prefix["curve"].equity, atol=1e-8, rtol=0)
    # A missing future bar must not retroactively remove AAA from the universe.
    path = tmp_path / "AAA.pkl"
    frame = pd.read_pickle(path).drop(index=270)
    future = frame.date > int(cutoff.strftime("%Y%m%d"))
    frame.loc[future, ["open", "high", "low", "close"]] *= 10
    frame.to_pickle(path)
    perturbed = load_panel(tmp_path)
    assert "US.AAA" in perturbed.symbols
    rerun = simulate(perturbed, policy, start, cutoff)
    assert reference["orders"] == rerun["orders"]
    np.testing.assert_allclose(reference["curve"].equity, rerun["curve"].equity, atol=1e-8, rtol=0)


def test_qqq_benchmark_uses_same_cash_integer_shares_cost_and_end_mark():
    panel = synthetic_panel()
    start, end, bps = panel.dates[65], panel.dates[90], 25
    result = benchmark(panel, start, end, bps)
    raw = panel.fields["open"][65, 2]
    quantity = int(10000 / (raw * 1.0025))
    expected_cash = 10000 - quantity * raw * 1.0025
    expected_equity = expected_cash + quantity * panel.fields["close"][65:91, 2]
    np.testing.assert_allclose(result["curve"].equity, expected_equity, atol=1e-8, rtol=0)
    assert result["metrics"]["cost_dollars"] == pytest.approx(quantity * raw * .0025)
    assert result["metrics"]["orders"] == 1
    values = np.r_[10000., expected_equity]
    expected_dd = (1 - values / np.maximum.accumulate(values)).max() * 100
    assert result["metrics"]["max_drawdown_pct"] == pytest.approx(expected_dd)


def test_covariance_budget_caps_weights_without_leverage():
    panel = synthetic_panel()
    policy = simple_policy(annual_vol_target=.003, weight_cap=.30)
    chosen = [0, 1]
    weights = weights_for(panel, 70, chosen, policy)
    covariance = np.cov(panel.fields["returns"][11:71, chosen], rowvar=False) * 252
    estimated_risk = np.sqrt(weights[chosen] @ covariance @ weights[chosen])
    assert 0 < weights.sum() < .60
    assert (weights >= 0).all()
    assert weights.max() <= .30
    assert estimated_risk == pytest.approx(policy.annual_vol_target)


def test_same_day_close_cannot_affect_that_days_open_orders():
    panel = synthetic_panel()
    policy = simple_policy()
    day = 70
    expected = simulate(panel, policy, panel.dates[65], panel.dates[day])
    panel.fields["close"][day, 0] *= .5
    panel.fields["momentum_risk"][day, 0] = -1000
    panel.fields["ema50"][day, 0] *= 10
    altered = simulate(panel, policy, panel.dates[65], panel.dates[day])
    assert expected["orders"] == altered["orders"]
    assert altered["curve"].equity.iloc[-1] != expected["curve"].equity.iloc[-1]


def test_extra_signal_lag_uses_two_sessions_old_ranks():
    panel = synthetic_panel()
    day = 70
    policy = simple_policy(positions=1, rank_buffer=1)
    panel.fields["momentum_risk"][day - 1, :2] = [-1., 10.]
    normal = simulate(panel, policy, panel.dates[day], panel.dates[day], signal_lag=1)
    lagged = simulate(panel, policy, panel.dates[day], panel.dates[day], signal_lag=2)
    assert normal["orders"][0]["symbol"] == "US.BBB"
    assert lagged["orders"][0]["symbol"] == "US.AAA"
    assert lagged["decisions"][0]["signal_date"] == str(panel.dates[day - 2].date())


def test_gate_blocks_new_buys_without_forcing_liquidation():
    panel = synthetic_panel()
    panel.fields["ema100"][70:, 2] = panel.fields["close"][70:, 2] * 1.1
    result = simulate(panel, simple_policy(regime="gate"), panel.dates[69], panel.dates[75])
    cutoff = str(panel.dates[71].date())
    assert result["positions"]
    assert not [o for o in result["orders"] if o["date"] >= cutoff and o["side"] == "BUY"]


def test_cash_policy_has_zero_trades_and_defined_return_drawdown():
    panel = synthetic_panel()
    result = simulate(panel, simple_policy(positions=0), panel.dates[65], panel.dates[75])
    assert result["orders"] == []
    assert result["metrics"]["return_pct"] == 0
    assert result["metrics"]["max_drawdown_pct"] == 0
    assert result["metrics"]["sharpe_zero_rf"] is None
