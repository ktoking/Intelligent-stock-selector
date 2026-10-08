import pandas as pd
import pytest

from research.daily_factor_lab.refresh import latest_completed_session, merge_daily


def rows():
    return [{"date":20260921,"open":10.,"high":11.,"low":9.,"close":10.,
             "volume":100.,"turnover":1000.},
            {"date":20260922,"open":11.,"high":12.,"low":10.,"close":11.,
             "volume":100.,"turnover":1100.}]


def test_refresh_overlap_is_deduplicated_and_future_bar_ignored():
    original=rows()
    future={**original[-1],"date":20260923}
    merged=merge_daily(pd.DataFrame(original[:1]),original+[future],"2026-09-22")
    assert list(merged.date)==[20260921,20260922]
    assert len(original)==2


def test_adjustment_revision_fails_instead_of_corrupting_price_history():
    original=rows()
    revised=[{**original[0],"close":5.},original[1]]
    with pytest.raises(ValueError,match="revised"):
        merge_daily(pd.DataFrame(original[:1]),revised,"2026-09-22")


def test_market_calendar_requires_complete_us_close():
    assert latest_completed_session("2026-09-23T19:59:00Z")=="2026-09-22"
    assert latest_completed_session("2026-09-23T20:01:00Z")=="2026-09-23"
    assert latest_completed_session("2026-07-03T23:00:00Z")=="2026-07-02"
