import pandas as pd

from research.soxl_regime.alternative_wave_lab import Settings, prepare, simulate


def _daily() -> pd.DataFrame:
    index = pd.date_range("2026-01-02", periods=90, freq="B")
    close = pd.Series([100 + i * .2 for i in range(len(index))], index=index)
    return pd.DataFrame({"open": close, "high": close + 1,
                         "low": close - 1, "close": close, "volume": 1000}, index=index)


def _hourly() -> pd.DataFrame:
    index = pd.date_range("2026-04-20 09:30", periods=72, freq="h",
                          tz="America/New_York")
    close = pd.Series([110 + i * .01 for i in range(len(index))], index=index)
    return pd.DataFrame({"open": close, "high": close + 1,
                         "low": close - 1, "close": close, "volume": 1000}, index=index)


def test_current_daily_close_is_not_available_to_intraday_signals():
    target = _daily()
    original = prepare(_hourly(), target, target)
    changed = target.copy()
    changed.loc[pd.Timestamp("2026-04-20"), "close"] = 1
    rerun = prepare(_hourly(), changed, target)
    day = original.day == "2026-04-20"
    pd.testing.assert_series_equal(original.loc[day, "broad"], rerun.loc[day, "broad"])
    pd.testing.assert_series_equal(original.loc[day, "strict"], rerun.loc[day, "strict"])


def test_no_trade_profit_factor_is_not_misreported_as_zero():
    target = _daily()
    features = prepare(_hourly(), target, target)
    result = simulate(features, Settings("channel"), "2026-04-20", "2026-04-20")
    assert result["trades"] == 0
    assert result["profit_factor"] is None
