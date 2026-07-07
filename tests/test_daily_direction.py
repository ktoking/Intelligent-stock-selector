import json
from types import SimpleNamespace

import pandas as pd

from daily_direction.direction import (
    MarketJob,
    build_fallback_direction,
    build_llm_prompt,
    eval_daily_signal,
    format_for_seatalk,
    generate_direction_report,
    scan_market,
)
from daily_direction.news import build_news_context, collect_direction_news
from daily_direction.seatalk_webhook import (
    build_text_payload,
    build_webhook_url,
)
from daily_direction.seatalk_bot import send_group_text_via_bot
from daily_direction.hithink_market import fetch_hithink_cn_quotes


def _signal_frame(last_close: float = 108.0, last_volume: float = 3_000_000.0) -> pd.DataFrame:
    rows = []
    for i in range(60):
        close = 100.0 + (i % 5) * 0.2
        rows.append(
            {
                "High": close + 0.5,
                "Low": close - 0.5,
                "Close": close,
                "Volume": 1_000_000.0,
            }
        )
    rows[-2]["Close"] = 100.0
    rows[-2]["High"] = 101.0
    rows[-1]["Close"] = last_close
    rows[-1]["High"] = last_close + 0.8
    rows[-1]["Volume"] = last_volume
    return pd.DataFrame(rows)


def test_eval_daily_signal_returns_rankable_market_signal():
    signal = eval_daily_signal(_signal_frame())

    assert signal is not None
    assert signal["daily_pct"] == 8.0
    assert signal["vol_ratio"] == 3.0
    assert signal["breakout_20d"] is True
    assert signal["above_sma50"] is True
    assert signal["signal_score"] > signal["daily_pct"]


def test_eval_daily_signal_detects_ma5_breakout_and_quant_baseline():
    df = _signal_frame(last_close=103.0, last_volume=2_000_000.0)
    df.loc[df.index[-7:-1], "Close"] = [101.0, 101.0, 101.0, 101.0, 101.0, 99.0]
    df.loc[df.index[-7:-1], "High"] = [101.5, 101.5, 101.5, 101.5, 101.5, 99.5]

    signal = eval_daily_signal(df)

    assert signal is not None
    assert signal["breakout_ma5"] is True
    assert signal["ma5"] < signal["close"]
    assert signal["quant_baseline_score"] > 50
    assert "突破五日线" in signal["baseline_note"]


def test_scan_market_ranks_by_quant_baseline_before_raw_daily_pct(monkeypatch):
    job = MarketJob(key="us", label="美股", market="us", pool="nasdaq100", limit=2)
    strong = _signal_frame(last_close=103.0, last_volume=2_500_000.0)
    strong.loc[strong.index[-7:-1], "Close"] = [101.0, 101.0, 101.0, 101.0, 101.0, 99.0]
    weak_spike = _signal_frame(last_close=110.0, last_volume=700_000.0)

    monkeypatch.setattr("daily_direction.direction.get_report_tickers", lambda **_: ["STRONG", "SPIKE"])
    monkeypatch.setattr(
        "daily_direction.direction._download_ohlcv_by_ticker",
        lambda *_args, **_kwargs: {"STRONG": strong, "SPIKE": weak_spike},
    )

    snapshot = scan_market(job, max_items=2)

    assert [row["ticker"] for row in snapshot["top_signals"]] == ["STRONG", "SPIKE"]


def test_scan_market_overlays_cn_quotes_from_hithink(monkeypatch):
    job = MarketJob(key="cn", label="A股", market="cn", pool="csi300", limit=1)
    frame = _signal_frame(last_close=100.0, last_volume=1_000_000.0)
    frame.loc[frame.index[-2], "Close"] = 100.0
    frame.loc[frame.index[-1], "Close"] = 100.0

    monkeypatch.setattr("daily_direction.direction.get_report_tickers", lambda **_: ["600030.SS"])
    monkeypatch.setattr(
        "daily_direction.direction._download_ohlcv_by_ticker",
        lambda *_args, **_kwargs: {"600030.SS": frame},
    )
    monkeypatch.setattr(
        "daily_direction.direction.fetch_hithink_cn_quotes",
        lambda tickers, **_kwargs: {
            "600030.SS": {
                "close": 110.0,
                "daily_pct": 10.0,
                "volume": 5_000_000.0,
                "high": 111.0,
                "low": 99.0,
                "open": 101.0,
            }
        },
    )

    snapshot = scan_market(job, max_items=1)

    assert snapshot["data_source"] == "yfinance+hithink"
    assert snapshot["top_signals"][0]["ticker"] == "600030.SS"
    assert snapshot["top_signals"][0]["close"] == 110.0
    assert snapshot["top_signals"][0]["daily_pct"] == 10.0
    assert snapshot["top_signals"][0]["quote_source"] == "hithink"


def test_scan_market_falls_back_to_yfinance_when_hithink_fails(monkeypatch):
    job = MarketJob(key="cn", label="A股", market="cn", pool="csi300", limit=1)
    frame = _signal_frame(last_close=106.0, last_volume=2_000_000.0)

    monkeypatch.setattr("daily_direction.direction.get_report_tickers", lambda **_: ["600030.SS"])
    monkeypatch.setattr(
        "daily_direction.direction._download_ohlcv_by_ticker",
        lambda *_args, **_kwargs: {"600030.SS": frame},
    )
    monkeypatch.setattr(
        "daily_direction.direction.fetch_hithink_cn_quotes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("hithink down")),
    )

    snapshot = scan_market(job, max_items=1)

    assert snapshot["data_source"] == "yfinance"
    assert snapshot["quote_overlay_error"] == "hithink down"
    assert snapshot["top_signals"][0]["quote_source"] == "yfinance"


def test_fetch_hithink_cn_quotes_batches_large_requests(monkeypatch):
    calls = []

    def fake_call(query, *, limit, timeout):
        calls.append((query, limit))
        if "600001.SH" in query:
            return {
                "datas": [
                    {"股票代码": "600001.SH", "最新价": "10.1", "最新涨跌幅": 1.0},
                    {"股票代码": "600002.SH", "最新价": "10.2", "最新涨跌幅": 2.0},
                ]
            }
        return {
            "datas": [
                {"股票代码": "600003.SH", "最新价": "10.3", "最新涨跌幅": 3.0},
            ]
        }

    monkeypatch.setattr("daily_direction.hithink_market._call_query2data", fake_call)

    result = fetch_hithink_cn_quotes(["600001.SS", "600002.SS", "600003.SS"], batch_size=2)

    assert len(calls) == 2
    assert set(result) == {"600001.SS", "600002.SS", "600003.SS"}


def test_build_llm_prompt_contains_three_market_contexts():
    jobs = [
        MarketJob(key="us", label="美股", market="us", pool="nasdaq100", limit=20),
        MarketJob(key="cn", label="A股", market="cn", pool="csi300", limit=20),
        MarketJob(key="hk", label="港股", market="hk", pool="hsi", limit=20),
    ]
    snapshots = {
        "us": {"label": "美股", "top_signals": [{"ticker": "NVDA", "daily_pct": 4.2, "vol_ratio": 2.1, "close": 150.0}]},
        "cn": {"label": "A股", "top_signals": [{"ticker": "300750.SZ", "daily_pct": 3.1, "vol_ratio": 1.8, "close": 200.0}]},
        "hk": {"label": "港股", "top_signals": [{"ticker": "0700.HK", "daily_pct": 2.8, "vol_ratio": 1.6, "close": 390.0}]},
    }

    news_context = {
        "market_news": [{"title": "Fed keeps rates unchanged", "ticker": "SPY"}],
        "ticker_news": {"NVDA": [{"title": "Nvidia supplier warning", "ticker": "NVDA"}]},
        "event_risks": [{"title": "semiconductor export controls", "risk_type": "regulation"}],
    }

    prompt = build_llm_prompt(snapshots, jobs, news_context=news_context)

    assert "美股" in prompt
    assert "A股" in prompt
    assert "港股" in prompt
    assert "NVDA" in prompt
    assert "今天方向" in prompt
    assert "不要使用 Markdown 标题" in prompt
    assert "📅 今天方向简报" in prompt
    assert "总判断：今天建议" in prompt
    assert "✅ 今天优先关注" in prompt
    assert "今日资讯" in prompt
    assert "异动" in prompt
    assert "semiconductor export controls" in prompt


def test_build_news_context_marks_event_risks():
    context = build_news_context(
        market_news=[
            {"ticker": "SPY", "title": "Fed rate decision hits markets", "publisher": "Yahoo", "published": "2026-07-06"}
        ],
        ticker_news={
            "NVDA": [
                {"ticker": "NVDA", "title": "Nvidia faces export control investigation", "publisher": "Wire"}
            ]
        },
    )

    assert context["event_risks"]
    assert context["event_risks"][0]["risk_type"] in {"policy", "regulation"}


def test_collect_direction_news_uses_top_signals_and_losers(monkeypatch):
    calls = []

    def fake_fetch(tickers, **_kwargs):
        calls.extend(tickers)
        return {ticker: [{"ticker": ticker, "title": f"{ticker} news"}] for ticker in tickers}

    monkeypatch.setattr("daily_direction.news.fetch_ticker_news", fake_fetch)
    snapshots = {
        "us": {
            "top_signals": [{"ticker": "AAPL"}, {"ticker": "VRTX"}],
            "top_losers": [{"ticker": "TER"}, {"ticker": "KLAC"}],
        },
        "cn": {"top_signals": [{"ticker": "600276.SS"}], "top_losers": []},
    }

    context = collect_direction_news(snapshots, max_tickers=4)

    assert "AAPL" in calls
    assert "TER" in calls
    assert context["ticker_news"]["AAPL"][0]["title"] == "AAPL news"


def test_format_for_seatalk_strips_unsupported_markdown():
    raw = "### 🔍 分市场观察方向\n**美股：** `AAPL`\n*   强趋势： **AAPL**"

    text = format_for_seatalk(raw)

    assert "###" not in text
    assert "🔍 分市场观察方向" in text
    assert "**美股：**" in text
    assert "`AAPL`" in text
    assert "*   强趋势： **AAPL**" in text


def test_generate_direction_report_formats_llm_output_for_seatalk():
    text = generate_direction_report(
        {"us": {"label": "美股", "top_signals": []}},
        [MarketJob(key="us", label="美股", market="us", pool="nasdaq100", limit=1)],
        llm_func=lambda **_: "### 标题\n**AAPL**",
    )

    assert "###" not in text
    assert "**AAPL**" in text


def test_build_fallback_direction_is_readable_without_llm():
    snapshots = {
        "us": {"label": "美股", "top_signals": [{"ticker": "NVDA", "daily_pct": 4.2, "vol_ratio": 2.1, "close": 150.0}]},
        "cn": {"label": "A股", "top_signals": []},
        "hk": {"label": "港股", "top_signals": [{"ticker": "0700.HK", "daily_pct": -1.5, "vol_ratio": 1.3, "close": 390.0}]},
    }

    text = build_fallback_direction(snapshots)

    assert "📅 今天方向简报" in text
    assert "总判断：今天建议【观察】" in text
    assert "美股" in text
    assert "NVDA" in text
    assert "A股" in text
    assert "无明显强信号" in text
    assert "📢 今日资讯/事件" in text
    assert "✅ 今天优先关注" in text


def test_build_text_payload_is_seatalk_system_account_shape():
    payload = build_text_payload("hello")

    assert payload == {"tag": "text", "text": {"content": "hello"}}
    assert json.dumps(payload, ensure_ascii=False)


def test_build_webhook_url_supports_dingtalk_style_query_signature():
    url = build_webhook_url(
        "https://openapi.seatalk.io/webhook/group/test",
        signing_secret="secret",
        timestamp_ms=1700000000000,
        sign_mode="query",
    )

    assert url.startswith("https://openapi.seatalk.io/webhook/group/test?")
    assert "timestamp=1700000000000" in url
    assert "sign=" in url


def test_send_group_text_via_bot_uses_explicit_group_id():
    calls = []
    settings = SimpleNamespace(allowed_group_ids=set())

    result = send_group_text_via_bot(
        "hello",
        group_id="G1",
        thread_id="T1",
        settings=settings,
        send_group_func=lambda *args: calls.append(args),
    )

    assert result == {"delivery": "seatalk-bot", "group_id": "G1", "thread_id": "T1"}
    assert calls == [("G1", "T1", "hello", settings)]


def test_send_group_text_via_bot_falls_back_to_allowed_group(monkeypatch):
    calls = []
    settings = SimpleNamespace(allowed_group_ids={"G2", "G1"})
    monkeypatch.delenv("DAILY_DIRECTION_SEATALK_GROUP_ID", raising=False)

    result = send_group_text_via_bot(
        "hello",
        settings=settings,
        send_group_func=lambda *args: calls.append(args),
    )

    assert result["group_id"] == "G1"
    assert calls[0][0] == "G1"
