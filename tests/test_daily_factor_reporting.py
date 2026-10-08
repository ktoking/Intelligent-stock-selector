import pytest

from research.daily_factor_lab.reporting import build_report, ic_text, pair


def test_incomplete_run_cannot_generate_report(tmp_path):
    with pytest.raises(ValueError, match="completion marker"):
        build_report(tmp_path)


def test_missing_metrics_do_not_become_zero_or_formatted_as_profit():
    assert pair({"return_pct": None, "max_drawdown_pct": None}) == "数据不足"
    assert pair({"return_pct": 0., "max_drawdown_pct": 0.}) == "+0.00% / 0.00%"
    assert ic_text(None) == "数据不足"
    assert ic_text(-.0254) == "-0.0254"
