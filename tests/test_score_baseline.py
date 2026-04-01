"""agents.score_baseline 定量基准线单测。"""
from agents.score_baseline import compute_quant_baseline, baseline_to_score10_hint


def test_baseline_neutral_when_empty():
    s, note = compute_quant_baseline({}, {}, {})
    assert 0 <= s <= 100
    assert note


def test_baseline_bullish_stack():
    technical = {
        "ok": True,
        "daily_long_align": True,
        "macd_summary": {"golden_cross": True, "above_zero": True},
        "kdj_summary": {"oversold": False, "overbought": False},
        "rsi_summary": {"oversold": False, "overbought": False},
        "divergence_summary": {},
        "volume_context": {"volume_ratio": 1.6},
        "momentum_summary": {"return_20d_pct": 6.0, "dist_to_52w_high_pct": -5.0},
    }
    fundamental = {"change_pct": 2.5, "trailing_pe": 12.0, "recommendation": "买入"}
    options = {"description": "偏多"}
    s, _ = compute_quant_baseline(technical, fundamental, options)
    assert s >= 70


def test_hint_score10():
    assert "10" in baseline_to_score10_hint(100) or "/10" in baseline_to_score10_hint(100)
