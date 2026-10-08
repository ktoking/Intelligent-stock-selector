from pathlib import Path

import numpy as np
import pandas as pd

from research.soxl_regime.features import add_features, rsi
from research.soxl_regime.quant_builder import VARIABLES, action_source
from research.soxl_regime.quant_graph_builder import (
    RESEARCH_BALANCED_OVERRIDES,
    build_graph_variant,
)
from research.soxl_regime.quant_aggressive_v2 import VARIABLES as V2_VARIABLES
from research.soxl_regime.quant_aggressive_v2 import action_source as v2_action_source
from research.soxl_regime.quant_aggressive_v2 import build as build_aggressive_v2
from research.soxl_regime.regime import RegimeConfig, classify


def _bars(rows=140, freq="5min"):
    index = pd.date_range("2026-01-05 09:30", periods=rows, freq=freq, tz="America/New_York")
    close = pd.Series(np.linspace(100, 125, rows), index=index)
    return pd.DataFrame({"open": close.shift(1).fillna(close.iloc[0]), "high": close + 1, "low": close - 1, "close": close, "volume": 1000}, index=index)


def test_wilder_rsi_is_bounded_and_period_is_14():
    value = rsi(_bars().close, 14)
    assert value.between(0, 100).all()
    assert value.iloc[-1] > 90


def test_features_use_only_current_and_past_rows():
    bars = _bars()
    first = add_features(bars).iloc[:100]
    changed = bars.copy(); changed.iloc[110:, changed.columns.get_loc("close")] *= 5
    second = add_features(changed).iloc[:100]
    pd.testing.assert_series_equal(first.ema20, second.ema20)


def test_regime_has_no_backward_mutation():
    features = add_features(_bars())
    original = classify(features, RegimeConfig())
    changed = features.copy(); changed.iloc[-10:, changed.columns.get_loc("close")] *= 0.5
    rerun = classify(changed, RegimeConfig())
    pd.testing.assert_series_equal(original.iloc[:-10], rerun.iloc[:-10])


def test_quant_reads_completed_5m_and_previous_completed_bar():
    source = action_source()
    assert "BarType.K_5M, select=2" in source
    assert "BarType.K_5M, select=3" in source


def test_quant_daily_signal_excludes_forming_day():
    assert "BarType.K_DAY, select=2" in action_source()


def test_quant_exit_precedes_entry():
    source = action_source()
    assert source.index("if _holding > 0:") < source.index("if _holding <= 0 and _exited == 0")


def test_quant_position_mode_is_not_overwritten_while_held():
    source = action_source()
    held = source[source.index("if _holding > 0:"):source.index("_entry_window")]
    assert "self.POSITION_MODE = _mode" not in held


def test_quant_trade_count_resets_at_0930_and_counts_new_cycles_only():
    source = action_source()
    assert "_now.minute >= 30" in source
    assert source.count("self.TRADE_COUNT = self.TRADE_COUNT + 1") == 1


def test_quant_entry_window_includes_boundaries():
    source = action_source()
    assert "_now.minute >= 45" in source
    assert "_now.minute <= 30" in source


def test_quant_force_flat_includes_1545():
    assert "_now.minute >= 45" in action_source()


def test_quant_target_is_total_equity_ratio_without_addition():
    source = action_source()
    assert "_target_qty = floor(_net * _ratio / _price / _lot) * _lot" in source
    assert "_target_qty - _holding" not in source


def test_execution_is_disabled_and_panic_branch_not_promoted():
    assert VARIABLES["EXECUTION_ENABLED"] == "0"
    assert VARIABLES["PANIC_POSITION"] == "0"


def test_graph_builder_preserves_full_known_good_canvas(tmp_path):
    template = Path.home() / "Downloads/超跌反弹SOXL_QQQ_DYNAMIC_REVERSION_V4.quant"
    if not template.exists():
        return
    result = build_graph_variant(
        template,
        tmp_path / "graph.quant",
        "SOXL_GRAPH_TEST",
        RESEARCH_BALANCED_OVERRIDES,
    )
    assert result["passed"] is True
    assert result["cards"] == 92
    assert result["lines"] == 91
    assert result["changed_card_ids"] == [1]
    assert result["checks"]["all_lines_byte_identical"] is True
    assert set(result["variable_diffs"]).issubset(RESEARCH_BALANCED_OVERRIDES)
    assert result["checks"]["only_requested_variables_changed"] is True


def test_aggressive_v2_uses_completed_qqq_daily_and_soxl_5m_bars():
    source = v2_action_source()
    assert 'Contract("US.QQQ")' in source
    assert "device_time(" not in source
    assert "TimeZone." not in source
    assert "US_EASTERN" not in source
    assert "MARKET_TIME_ZONE" not in source
    assert "BarType.K_DAY, select=2" in source
    assert "BarType.K_5M, select=2" in source
    assert "BarType.K_5M, select=3" in source


def test_aggressive_v2_exits_before_entries_and_disables_down_panic():
    source = v2_action_source()
    assert source.index("if _holding > 0:") < source.index("if _holding <= 0")
    assert V2_VARIABLES["PANIC_POSITION"][0] == "0"
    assert V2_VARIABLES["EXECUTION_ENABLED"][0] == "1"


def test_aggressive_v2_preserves_full_connected_canvas_and_gates_legacy_graph(tmp_path):
    template = Path.home() / "Downloads/超跌反弹SOXL_QQQ_DYNAMIC_REVERSION_V4.quant"
    if not template.exists():
        return
    result = build_aggressive_v2(template, tmp_path / "aggressive.quant")
    assert result["passed"] is True
    assert result["cards"] == 98
    assert result["lines"] == 98
    assert len(result["connected_card_ids"]) == 98
    assert result["checks"]["no_rule_card_is_terminal"] is True
    assert result["checks"]["legacy_gate_is_permanently_false"] is True
    assert result["checks"]["legacy_false_branch_has_safe_action"] is True
    assert result["checks"]["original_rule_1005_keeps_action"] is True
    assert result["checks"]["no_unsupported_timezone_code"] is True
    assert result["checks"]["native_entry_window_1000_1530_et"] is True
    assert result["checks"]["native_reset_branch_preserved"] is True
    assert result["checks"]["native_force_flat_branch_preserved"] is True
    assert result["checks"]["force_flat_excludes_trend_mode"] is True
