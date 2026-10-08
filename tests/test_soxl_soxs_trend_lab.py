import numpy as np
import pandas as pd

from scripts.soxl_soxs_trend_lab import Config, make_signal, moomoo_canvas_action, moomoo_quant, simulate
from scripts.moomoo_quant_binary import _strategy_class, package_canvas_strategy


def _bars() -> pd.DataFrame:
    index = pd.date_range("2026-06-22 09:30", periods=10, freq="h", tz="America/New_York")
    qqq = np.array([100, 101, 102, 103, 102, 101, 100, 99, 98, 97], dtype=float)
    soxl = np.array([20, 21, 22, 23, 22, 21, 20, 19, 18, 17], dtype=float)
    soxs = np.array([10, 9.8, 9.6, 9.4, 9.6, 9.8, 10, 10.2, 10.4, 10.6], dtype=float)
    values = {}
    for symbol, prices in (("qqq", qqq), ("soxl", soxl), ("soxs", soxs)):
        values[f"{symbol}_open"] = prices
        values[f"{symbol}_high"] = prices
        values[f"{symbol}_low"] = prices
        values[f"{symbol}_close"] = prices
        values[f"{symbol}_volume"] = np.full(len(prices), 1000.0)
    return pd.DataFrame(values, index=index)


def test_simulation_never_holds_both_and_switches_causally():
    bars = _bars()
    signal = pd.Series([1, 1, 1, 1, -1, -1, -1, -1, -1, -1], index=bars.index)
    result = simulate(bars, signal, ["2026-06-22"], cost_bps=0)
    assert result["metrics"]["switches"] == 1
    assert [row["symbol"] for row in result["trades"]] == ["SOXL", "SOXS"]
    assert {row["held"] for row in result["equity"]} <= {"SOXL", "SOXS", "CASH"}


def test_persistence_delays_signal_change():
    close = pd.Series([100, 101, 102, 101, 100, 99, 98, 97], dtype=float)
    signal = make_signal(close, Config("momentum", horizon=1, persistence=2))
    assert signal.iloc[1] == 0
    assert signal.iloc[2] == 1
    assert signal.iloc[3] == 1
    assert signal.iloc[4] == -1


def test_quant_source_has_safety_switch_and_packages_as_native_canvas():
    text = moomoo_quant(Config("ema", fast=8, slow=30, buffer=0.002, persistence=2), shadow_only=True)
    assert "class Strategy(StrategyBase):" in text
    assert 'Contract("US.SOXL")' in text
    assert 'Contract("US.SOXS")' in text
    assert "execution_enabled = show_variable(False" in text
    compile(text, "generated.quant", "exec")

    daily = moomoo_quant(
        Config("momentum", "D1", horizon=2, buffer=0.02, persistence=1, neutral_cash=True),
        shadow_only=True,
    )
    assert "select_now = 1 if False else 2" in daily
    assert "self.last_daily_signal_date" in daily
    compile(daily, "generated_daily.quant", "exec")

    action = moomoo_canvas_action(
        Config("momentum", "D1", horizon=2, buffer=0.02, persistence=1, neutral_cash=True),
        shadow_only=True,
    )
    compile("def _canvas_action(self):\n" + "".join(f"    {line}\n" for line in action.splitlines()), "canvas_action", "exec")
    template_path = "/Users/kaiyi.wang/Downloads/SOXL_ADAPTIVE_RECOVERY_V5_BETA_OPTIMIZED.quant"
    with open(template_path, "rb") as template_file:
        packaged = package_canvas_strategy(template_file.read(), "TEST_CANVAS_STRATEGY", action)
    Strategy = _strategy_class()
    message = Strategy.FromString(packaged)
    assert message.strategyType == 1
    assert message.startCardId == 1
    assert len(message.canvasCardList) == 2
    assert len(message.canvasLineList) == 1
    assert len(message.userCode.userCode) == 0
    assert message.canvasLineList[0].startCardId == 1
    assert message.canvasLineList[0].endCardId == 1006
    action_card = next(card for card in message.canvasCardList if card.id == 1006)
    assert action_card.isUserCode is True
    assert 'Contract("US.SOXS")' in "".join(action_card.userCode.userCode)
    assert "self.EXECUTION_ENABLED >= 1" in "".join(action_card.userCode.userCode)
