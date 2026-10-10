"""Regression gates for unattended news and conditional aggressive briefings."""
import json

import requests

from daily_direction import news
from daily_direction.direction import generate_direction_report
from daily_direction.reporting import candidates, render_report


NOW = "2026-10-10T05:00:00Z"


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def test_eastmoney_uses_shanghai_time_and_rejects_stale_future_and_undated(monkeypatch):
    times = ["2026-10-10 12:00:00", "2026-10-10 14:00:00", "2026-10-01 12:00:00", ""]
    rows = [{"code": str(2026101000000 + i), "title": str(i), "showTime": stamp} for i, stamp in enumerate(times)]
    monkeypatch.setattr(news.requests, "get", lambda *a, **kw: Response({"code": "1", "data": {"fastNewsList": rows}}))
    items, diag = news.fetch_market_feed("eastmoney", "global", now=NOW)
    assert [item["title"] for item in items] == ["0"]
    assert items[0]["published"] == "2026-10-10T04:00:00+00:00"
    assert items[0]["url"] == "https://finance.eastmoney.com/a/2026101000000.html"
    assert diag["raw"] == 4 and diag["accepted"] == 1


def test_one_feed_failure_preserves_other_news_and_diagnostic(monkeypatch):
    def get(url, **kw):
        assert kw["timeout"] == (4, 10)
        if "wallstcn" in url:
            raise requests.Timeout()
        return Response({"code": "1", "data": {"fastNewsList": [{"title": "关税政策更新", "showTime": "2026-10-10 12:00:00", "code": "20261010123"}]}})
    monkeypatch.setattr(news.requests, "get", get)
    items, diagnostics = news.fetch_market_news(now=NOW)
    assert items[0]["title"] == "关税政策更新"
    assert len([d for d in diagnostics if d["status"] == "error"]) == 3
    assert diagnostics[-1]["status"] == "ok"


def test_wallstreetcn_blank_title_uses_plain_text_without_html(monkeypatch):
    import pandas as pd
    stamp = int(pd.Timestamp("2026-10-10T04:00:00Z").timestamp())
    row = {"title": "", "content_text": "<p>标普收涨。其它信息</p>", "display_time": stamp, "uri": "https://wallstreetcn.com/livenews/123"}
    monkeypatch.setattr(news.requests, "get", lambda *a, **kw: Response({"code": 20000, "data": {"items": [row]}}))
    items, _ = news.fetch_market_feed("wallstreetcn", "us", now=NOW)
    assert items[0]["title"] == "标普收涨"


def test_independent_news_success_does_not_query_empty_yahoo(monkeypatch):
    item = {"title": "美光科技上调指引", "summary": "", "published": NOW, "url": "https://example.com/news"}
    monkeypatch.setattr(news, "fetch_market_news", lambda **kw: ([item], [{"status": "ok"}]))
    monkeypatch.setattr(news, "fetch_ticker_news", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("Yahoo should be fallback")))
    context = news.collect_direction_news({"us": {"top_signals": [{"ticker": "MU"}]}}, now=NOW)
    assert context["ticker_news"]["MU"][0]["title"] == item["title"]
    assert context["diagnostics"] == [{"status": "ok"}]


def test_yahoo_fallback_normalizes_epoch_and_preserves_failure_reason(monkeypatch):
    import pandas as pd
    class Ticker:
        news = [{"title": "&lt;b&gt;AI&#x20;更新&lt;/b&gt;\u200b", "providerPublishTime": int(pd.Timestamp("2026-10-10T04:00:00Z").timestamp())}]
    monkeypatch.setattr(news.yf, "Ticker", lambda _: Ticker())
    items = news.fetch_ticker_news(["QQQ"], now=NOW)
    assert items["QQQ"][0]["title"] == "AI 更新"
    assert items["QQQ"][0]["published"] == "2026-10-10T04:00:00+00:00"
    def fail(_):
        raise requests.Timeout()
    monkeypatch.setattr(news.yf, "Ticker", fail)
    diagnostics = []
    assert news.fetch_ticker_news(["QQQ"], now=NOW, diagnostics=diagnostics) == {}
    assert len(diagnostics) == 1 and diagnostics[0]["status"] == "error"


def snapshots():
    def snap(label, ticker, pct, baseline, benchmark, advance):
        row = {"ticker": ticker, "daily_pct": pct, "quant_baseline_score": baseline, "vol_ratio": 1.5,
               "breakout_ma5": True, "high": 101.2, "low": 98.1}
        return {"label": label, "data_date": "2026-10-09", "top_signals": [row], "top_gainers": [row], "top_losers": [],
                "benchmarks": [{"ticker": "QQQ", "name": "基准ETF", "daily_pct": benchmark}],
                "breadth": {"total": 80, "advancing": advance, "declining": 80-advance}, "downloaded": 80, "universe_size": 80}
    return {"us": snap("美股", "MU", 2, 54, -3.5, 20), "cn": snap("A股", "600000.SS", 2, 65, 0.5, 45)}


def test_selective_attack_does_not_turn_selloff_into_bull_market():
    text = render_report(snapshots())
    assert "【分市场进攻】" in text
    assert "主攻 A股" in text
    assert "美股 · 防守" in text and "逆势候选" in text
    assert "站上 101.2" in text and "跌破 98.1" in text
    assert "【防守】" in render_report(snapshots(), style="conservative")


def test_aggressive_expands_breakout_pool_but_excludes_chasing_and_decliners():
    data = snapshots()
    assert "MU" in candidates(data)["focus"]["us"]
    assert "MU" not in candidates(data, style="conservative")["focus"]["us"]
    data["us"]["top_signals"][0]["daily_pct"] = 7
    assert "MU" not in candidates(data)["focus"]["us"]
    data["us"]["top_signals"][0]["daily_pct"] = -1
    assert "MU" not in candidates(data)["focus"]["us"]


def test_llm_empty_news_selection_still_shows_available_event():
    context = {"market_news": [{"title": "央行政策更新", "publisher": "东方财富", "published": NOW, "url": "https://example.com/news"}]}
    choice = {"focus": {"us": ["MU"], "cn": ["600000.SS"]}, "risk": {"us": [], "cn": []}, "news_ids": []}
    text = generate_direction_report(snapshots(), [], news_context=context, llm_func=lambda **kw: json.dumps(choice))
    assert "央行政策更新" in text and "10-10 13:00" in text
    assert "资讯源暂不可用" not in text
    assert len(text) <= 1500 and "&#x20;" not in text and "\u200b" not in text


def test_no_llm_cli_still_collects_news(monkeypatch, tmp_path):
    from scripts import daily_direction_push as push
    context = {"market_news": [{"title": "央行政策更新", "publisher": "东方财富", "published": NOW}]}
    monkeypatch.setattr(push, "load_env", lambda: None)
    monkeypatch.setattr(push, "scan_markets", lambda *a, **kw: snapshots())
    monkeypatch.setattr(push, "quality_errors", lambda *_: [])
    monkeypatch.setattr(push, "collect_direction_news", lambda *_: context)
    monkeypatch.setattr("sys.argv", ["daily_direction_push.py", "--markets", "us,cn", "--no-llm", "--output-dir", str(tmp_path)])
    assert push.main() == 0
    assert "央行政策更新" in (tmp_path / "report.txt").read_text()
    assert "规则版扫描，本次未调用 LLM" in (tmp_path / "report.txt").read_text()
