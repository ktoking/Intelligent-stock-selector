import numpy as np
import pandas as pd
import pytest
from types import SimpleNamespace

from research.soxl_regime.main_wave_hourly import Config, features
from research.soxl_regime.quant_main_wave_runner import runner_risk_source


def fixture():
    days = pd.bdate_range("2025-01-02", periods=140)
    daily = pd.DataFrame({"s_close": np.linspace(100, 130, len(days)),
                          "q_close": np.linspace(100, 140, len(days))}, index=days)
    times = pd.DatetimeIndex([day + pd.Timedelta(hours=10) for day in days]).tz_localize("America/New_York")
    prices = np.linspace(100, 130, len(times))
    hourly = pd.DataFrame({"open": prices, "high": prices + 1,
                           "low": prices - 1, "close": prices}, index=times)
    daily.loc[days[-3:-1], "s_close"] = 90
    return hourly, daily


def test_hold_requires_two_completed_daily_closes_below_ema():
    hourly, daily = fixture()
    result = features(hourly, daily, Config(exit_mode="two_daily_closes"))
    assert bool(result.hold_regime.iloc[-2])  # Only the first down day is known.
    assert not bool(result.hold_regime.iloc[-1])  # Both down days are now known.


def test_current_day_cannot_change_its_hold_signal():
    hourly, daily = fixture()
    before = features(hourly, daily, Config(exit_mode="two_daily_closes"))
    daily.iloc[-1, daily.columns.get_loc("s_close")] = 10000
    after = features(hourly, daily, Config(exit_mode="two_daily_closes"))
    pd.testing.assert_series_equal(before.hold_regime, after.hold_regime)


def test_default_exit_signal_remains_identical_to_entry_regime():
    hourly, daily = fixture()
    result = features(hourly, daily, Config())
    assert result.hold_regime.equals(result.regime)


@pytest.mark.parametrize("peak,price,daily_close,older_close,sells", [
    (130, 112, 125, 125, 0),  # Winner tolerates more than the old 12% retracement.
    (130, 108, 125, 125, 1),  # Winner still exits beyond the 16% trail.
    (100, 91, 125, 125, 1),   # Initial hard stop still exits immediately.
    (110, 110, 99, 101, 0),   # One daily dip does not force an exit.
    (110, 110, 99, 99, 1),    # Two completed daily dips do.
])
def test_generated_canvas_risk_logic(peak, price, daily_close, older_close, sells):
    state = SimpleNamespace(trading_symbol="target", ref_symbol="reference",
                            WAVE_EXIT_PENDING=0, WAVE_PEAK_CLOSE=peak,
                            WAVE_ENTRY_PRICE=100, WAVE_STOP=.08, WAVE_TRAIL=.12,
                            WAVE_RUNNER_TRIGGER=.2, WAVE_RUNNER_TRAIL=.16,
                            WAVE_EXECUTION_ENABLED=1, WAVE_COOLDOWN_SESSIONS=7)
    orders = []

    def close(symbol, bar_type, select, **kwargs):
        if bar_type == "hour":
            return price
        if symbol == "reference":
            return 130
        return older_close if select == 3 else daily_close

    def ema(symbol, period, select, **kwargs):
        if symbol == "reference":
            return 120 if period == 20 else 100
        return 100 if period == 20 else 95

    env = {"self": state, "position_holding_qty": lambda **kw: 100,
           "bar_close": close, "ema": ema,
           "BarType": SimpleNamespace(K_DAY="day", K_60M="hour"),
           "THType": SimpleNamespace(RTH="rth"),
           "OrderSide": SimpleNamespace(SELL="sell"),
           "place_market": lambda **kw: orders.append(kw), "alert": lambda **kw: None}
    exec(compile(runner_risk_source(), "canvas_risk", "exec"), env)
    assert len(orders) == sells
