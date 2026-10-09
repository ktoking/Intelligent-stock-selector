import json

import pandas as pd
import pytest

from daily_direction.quality import completed_bars, latest_completed_session, quality_errors
from daily_direction.reporting import candidates, identity, market_stance, parse_selection, render_report
from daily_direction.direction import generate_direction_report, MarketJob
from daily_direction.news import fetch_ticker_news


@pytest.mark.parametrize("market,now,expected", [
    ("us", "2026-10-09T01:00:00Z", "2026-10-08"),
    ("us", "2026-10-08T19:59:00Z", "2026-10-07"),
    ("us", "2026-07-06T01:00:00Z", "2026-07-02"),
    ("us", "2026-11-27T18:30:00Z", "2026-11-27"),
    ("cn", "2026-10-07T01:00:00Z", "2026-09-30"),
    ("cn", "2026-10-09T05:00:00Z", "2026-10-08"),
    ("hk", "2026-10-01T01:00:00Z", "2026-09-30"),
])
def test_latest_completed_session_handles_close_holidays_and_half_days(market, now, expected):
    assert latest_completed_session(market, now) == expected


def test_daily_dates_keep_exchange_timezone_and_drop_partial_bars():
    frame = pd.DataFrame({"Close": [100, 90, 101], "High": [101, 91, 102], "Low": [99, 89, 100], "Volume": [1000] * 3},
                         index=pd.date_range("2026-10-07", periods=3, tz="Asia/Hong_Kong"))
    clean = completed_bars(frame, "2026-10-08")
    assert clean.index[-1].date().isoformat() == "2026-10-08"
    assert clean.iloc[-1]["Close"] == 90


def test_quality_rejects_stale_data_low_coverage_and_source_disagreement():
    errors = quality_errors({"us": {"label": "美股", "expected_session": "2026-10-08", "data_date": "2026-10-07",
                                   "coverage_ratio": 0.5, "missing_benchmarks": ["QQQ"], "crosscheck_errors": ["MU 来源不一致"]}})
    assert len(errors) == 4


def _snapshot():
    mu = {"ticker": "MU", "daily_pct": -4.79, "vol_ratio": 1.16, "quant_baseline_score": 51}
    pep = {"ticker": "PEP", "daily_pct": 3.73, "vol_ratio": 1.13, "quant_baseline_score": 70}
    return {"us": {"label": "美股", "data_date": "2026-10-08", "expected_session": "2026-10-08",
                   "coverage_ratio": 1, "downloaded": 80, "universe_size": 80,
                   "benchmarks": [{"ticker": "QQQ", "name": "纳指100ETF", "daily_pct": -1.34},
                                  {"ticker": "SOXX", "name": "半导体ETF", "daily_pct": -3.35}],
                   "breadth": {"advancing": 38, "declining": 42, "unchanged": 0, "total": 80},
                   "top_signals": [pep], "top_gainers": [pep], "top_losers": [mu]}}


def test_oct8_selloff_report_is_defensive_despite_positive_candidates():
    snapshots = _snapshot()
    text = render_report(snapshots)
    assert "【防守】" in text
    assert "纳指100ETF -1.34%" in text
    assert "半导体ETF -3.35%" in text
    assert "MU 美光科技（存储芯片） -4.79%" in text
    assert "基准分 70/100" in text
    assert "未取得可用的近期资讯" in text
    assert "缺乏统一的宏观驱动" not in text


@pytest.mark.parametrize("ticker,name,sector", [
    ("1299.HK", "友邦保险", "保险"), ("1088.HK", "中国神华", "煤炭"),
    ("NXPI", "恩智浦", "汽车/工业芯片"), ("MCHP", "微芯科技", "微控制器"),
])
def test_known_misclassified_tickers_use_verified_identity(ticker, name, sector):
    assert identity(ticker) == (name, sector)


def test_llm_can_select_only_existing_tickers_and_cannot_supply_numbers():
    allowed = candidates(_snapshot())
    selection = {"focus": {"us": ["PEP"]}, "risk": {"us": ["MU"]}, "news_ids": []}
    assert parse_selection(json.dumps(selection), allowed, []) == selection
    selection["daily_pct"] = 4.06
    with pytest.raises(ValueError):
        parse_selection(json.dumps(selection), allowed, [])
    del selection["daily_pct"]
    selection["focus"]["us"] = ["MU"]
    with pytest.raises(ValueError):
        parse_selection(json.dumps(selection), allowed, [])


def test_valid_llm_selection_renders_input_values_without_rewriting():
    selection = {"focus": {"us": ["PEP"]}, "risk": {"us": ["MU"]}, "news_ids": []}
    text = generate_direction_report(_snapshot(), [MarketJob("us", "美股", "us", "nasdaq100")],
                                     llm_func=lambda **_: json.dumps(selection), news_context={})
    assert "【防守】" in text and "-4.79%" in text
    assert "模型选择未通过校验" not in text


def test_news_filter_rejects_undated_old_and_future_items(monkeypatch):
    class Ticker:
        news = [{"content": {"title": "old", "pubDate": "2026-10-01T12:00:00Z"}},
                {"content": {"title": "future", "pubDate": "2026-10-10T12:00:00Z"}},
                {"content": {"title": "undated"}},
                {"content": {"title": "recent", "pubDate": "2026-10-08T22:00:00Z"}}]
    monkeypatch.setattr("daily_direction.news.yf.Ticker", lambda _: Ticker())
    items = fetch_ticker_news(["QQQ"], now="2026-10-09T01:00:00Z")
    assert [item["title"] for item in items["QQQ"]] == ["recent"]


def test_cli_never_generates_or_sends_when_session_validation_fails(monkeypatch, tmp_path):
    from scripts import daily_direction_push as push
    snapshots = _snapshot()
    snapshots["us"]["data_date"] = "2026-10-07"
    monkeypatch.setattr(push, "load_env", lambda: None)
    monkeypatch.setattr(push, "scan_markets", lambda *_a, **_kw: snapshots)
    monkeypatch.setattr(push, "generate_direction_report", lambda *_a, **_kw: pytest.fail("Must not generate stale report"))
    monkeypatch.setattr("sys.argv", ["daily_direction_push.py", "--markets", "us", "--send", "--force-send", "--output-dir", str(tmp_path)])
    assert push.main() == 1
    assert (tmp_path / "quality-errors.json").exists()
