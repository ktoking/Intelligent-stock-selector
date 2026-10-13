import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from daily_direction import futunn_market as market
from daily_direction.direction import MarketJob, scan_market
from daily_direction.quality import completed_bars
from daily_direction.reporting import render_report
from daily_direction.seatalk_bot import build_brief_card, build_brief_cards, clean_brief_text, send_group_text_via_bot


def data(end="2026-10-12", volume=1000):
    return {"kline_list": [dict(date=int(day.strftime("%Y%m%d")), open=100, close=101, high=102, low=99,
                               volume=volume, sc_name="测试证券") for day in pd.bdate_range(end=end, periods=30)]}


def test_symbol_mapping_includes_actual_indices_and_padded_hong_kong_codes():
    assert market.futunn_symbol("^HSI") == "HK.800000"
    assert market.futunn_symbol("000001.SS") == "SH.000001"
    assert market.futunn_symbol("0700.HK") == "HK.00700"
    assert market.futunn_symbol("000858.SZ") == "SZ.000858"


def test_history_keeps_provider_dates_and_rejects_stale_partial_or_impossible_bars():
    frame = market.history_frame(data(), "2026-10-12")
    assert frame.index[-1] == pd.Timestamp("2026-10-12")
    assert frame.attrs["quote_source"] == "futunn"
    for payload in (data("2026-10-09"), data("2026-10-13")):
        with pytest.raises(ValueError):
            market.history_frame(payload, "2026-10-12")
    invalid = data()
    invalid["kline_list"][-1]["high"] = 95
    with pytest.raises(ValueError):
        market.history_frame(invalid, "2026-10-12")
    invalid = data()
    invalid["kline_list"][-1]["close"] = float("inf")
    with pytest.raises(ValueError):
        market.history_frame(invalid, "2026-10-12")


def test_index_zero_volume_does_not_invent_trading_volume_or_drop_valid_close():
    frame = market.history_frame(data(volume=0), "2026-10-12")
    assert completed_bars(frame, "2026-10-12").empty
    index = completed_bars(frame, "2026-10-12", require_volume=False)
    assert len(index) == 30 and index.iloc[-1]["Volume"] == 0


def test_exact_session_and_adjustment_cache_avoids_repeat_calls_but_reloads_bad_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(market, "configured", lambda: True)
    calls = []
    def request(*args):
        calls.append(args)
        return data()
    monkeypatch.setattr(market, "_history_request", request)
    frames, diag = market.fetch_daily_history(["0700.HK"], session="2026-10-12", cache_dir=tmp_path)
    assert len(frames) == 1 and diag[0]["status"] == "ok"
    frames, diag = market.fetch_daily_history(["0700.HK"], session="2026-10-12", cache_dir=tmp_path)
    assert len(calls) == 1 and diag[0]["status"] == "cached"
    cache = tmp_path / "HK.00700/2026-10-12-autype1.json"
    stored = json.loads(cache.read_text())
    stored["autype"] = 3
    cache.write_text(json.dumps(stored))
    market.fetch_daily_history(["0700.HK"], session="2026-10-12", cache_dir=tmp_path)
    assert len(calls) == 2


def test_rate_limit_uses_server_header_and_nested_error_cooldown(monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    monkeypatch.setenv("FUTUNN_APP_KEY", "test-app")
    monkeypatch.setenv("FUTUNN_PRIVATE_KEY_FILE", "/unused-test-key")
    monkeypatch.setattr(Path, "read_bytes", lambda _: b"test")
    monkeypatch.setattr(market.serialization, "load_pem_private_key", lambda *a, **kw: Ed25519PrivateKey.generate())
    monkeypatch.setattr(market, "_pace", lambda: None)
    cooldowns = []
    monkeypatch.setattr(market, "_cooldown", cooldowns.append)
    class Response:
        headers = {"Retry-After": "7"}
        def __init__(self, value): self.value = value
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return json.dumps(self.value).encode()
    replies = iter([Response({"ret_code": -11, "ret_msg": "rate limit exceeded", "error": {"message": "retry after 7s"}}),
                    Response({"ret_code": 0, "data": data()})])
    monkeypatch.setattr(market.urllib.request, "urlopen", lambda *a, **kw: next(replies))
    assert market._history_request("HK.00700", "2025-10-12", "2026-10-12")["kline_list"]
    assert cooldowns == [8]


def test_primary_futunn_restores_cn_candidate_without_using_stale_yahoo(monkeypatch):
    monkeypatch.setattr("daily_direction.direction.get_report_tickers", lambda **kw: ["600000.SS"])
    monkeypatch.setattr("daily_direction.direction.futunn_configured", lambda: True)
    payload = data()
    payload["kline_list"][-1]["close"] = 102
    frame = market.history_frame(payload, "2026-10-12")
    monkeypatch.setattr("daily_direction.direction.fetch_daily_history", lambda *a, **kw: ({"600000.SS": frame, "000001.SS": frame}, []))
    monkeypatch.setattr("daily_direction.direction._download_ohlcv_by_ticker", lambda *a, **kw: pytest.fail("Fresh primary history must not use Yahoo"))
    snapshot = scan_market(MarketJob("cn", "A股", "cn", "csi300", 1), now="2026-10-13T02:00:00Z")
    assert snapshot["data_date"] == "2026-10-12" and snapshot["coverage_ratio"] == 1
    assert snapshot["data_source"] == "futunn"
    assert snapshot["top_signals"][0]["quote_source"] == "futunn"


def test_full_brief_plain_content_and_native_card_remove_escape_artifacts():
    from daily_direction.quality import data_date
    frame = market.history_frame(data(), "2026-10-12")
    row = {"ticker": "0700.HK", "daily_pct": 2, "quant_baseline_score": 70, "vol_ratio": 1.5, "high": 102, "low": 99}
    snapshots = {"hk": {"label": "港股", "data_date": data_date(frame), "top_signals": [row], "top_gainers": [row], "data_source": "futunn",
                         "benchmarks": [{"ticker": "^HSI", "name": "恒生指数", "daily_pct": 0.2}], "breadth": {"total": 80, "advancing": 50, "declining": 30}}}
    text = render_report(snapshots, news_context={})
    assert "富途历史日线" in text and "Yahoo" not in text
    assert "**" not in text and "&#x20;" not in text and "\u200b" not in text
    card = build_brief_card(text)
    elements = card["interactive_message"]["elements"]
    assert card["tag"] == "interactive_message"
    assert elements[0]["title"]["text"] == "📅 今天方向简报"
    assert elements[1]["description"]["format"] == 1
    assert "触发：" in "\n".join(item["description"]["text"] for item in elements[1:])
    assert clean_brief_text("**新闻**\n&#x20;[标题](https://example.com)\u200b") == "新闻\n标题\nhttps://example.com"


def test_native_card_splits_description_at_api_rune_limit_without_losing_content():
    text = "📅 今天方向简报\n" + ("行情候选\n" * 350).strip()
    cards = build_brief_cards(text)
    assert all(len(card["interactive_message"]["elements"]) == 2 for card in cards)
    bodies = [card["interactive_message"]["elements"][1]["description"]["text"] for card in cards]
    assert all(0 < len(body) <= 1000 for body in bodies)
    assert "\n".join(bodies) == text.split("\n", 1)[1]


def test_default_sender_uses_card_and_requires_explicit_success(monkeypatch):
    from scripts import seatalk_hermes_adapter as adapter
    calls = []
    def post(path, body, cfg):
        calls.append(body)
        return {"code": 0, "message_id": "M1"}
    monkeypatch.setattr(adapter, "_post_openapi", post)
    settings = SimpleNamespace(allowed_group_ids={"G1"}, group_chat_path="/group")
    result = send_group_text_via_bot("📅 今天方向简报\n三市场内容", settings=settings, group_id="G1", thread_id="T1")
    assert result["api_code"] == 0 and result["message_id"] == "M1"
    assert calls[0]["message"]["tag"] == "interactive_message"
    assert calls[0]["message"]["thread_id"] == "T1"
    monkeypatch.setattr(adapter, "_post_openapi", lambda *a: {"code": 1})
    with pytest.raises(RuntimeError):
        send_group_text_via_bot("简报", settings=settings, group_id="G1")


def test_multi_card_sender_confirms_every_card_before_reporting_success(monkeypatch):
    from scripts import seatalk_hermes_adapter as adapter
    responses = iter([{"code": 0, "message_id": "M1"}, {"code": 1}])
    monkeypatch.setattr(adapter, "_post_openapi", lambda *a: next(responses))
    settings = SimpleNamespace(allowed_group_ids={"G1"}, group_chat_path="/group")
    with pytest.raises(RuntimeError):
        send_group_text_via_bot("今天方向简报\n" + "候选数据\n" * 260, settings=settings, group_id="G1")
