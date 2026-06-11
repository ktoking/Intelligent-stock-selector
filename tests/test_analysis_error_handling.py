import pandas as pd
from fastapi.testclient import TestClient

import server
from agents import fundamental


def test_get_fundamental_data_prefers_realtime_price_when_last_daily_bar_is_nan(monkeypatch):
    class DummyTicker:
        info = {
            "shortName": "Micron Technology, Inc.",
            "sector": "Technology",
            "industry": "Semiconductors",
            "currentPrice": 891.88,
            "regularMarketPrice": 891.88,
            "regularMarketChangePercent": -4.70248,
        }
        financials = pd.DataFrame()

    hist = pd.DataFrame(
        {
            "Open": [988.18, None],
            "High": [989.15, None],
            "Low": [928.65, None],
            "Close": [935.89, None],
            "Volume": [72824300, 0],
        }
    )

    monkeypatch.setattr(fundamental.yf, "Ticker", lambda ticker: DummyTicker())
    monkeypatch.setattr(fundamental, "_yf_get_history", lambda *args, **kwargs: hist)
    monkeypatch.setattr(fundamental, "_av_get_quote", lambda ticker: (None, None))

    payload = fundamental.get_fundamental_data("MU")

    assert payload["current_price"] == 891.88
    assert payload["change_pct"] == -4.7


def test_agent_analyze_returns_structured_error_when_base_analysis_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("STOCK_AGENT_ANALYSIS_MEMORY_DB", str(tmp_path / "analysis_memory.sqlite"))
    monkeypatch.setenv("STOCK_AGENT_MEM0_SYNC", "0")

    client = TestClient(server.app)

    monkeypatch.setattr(server, "run_full_analysis", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        server,
        "explain_analysis_gap",
        lambda *args, **kwargs: {
            "code": "ANALYSIS_DATA_UNAVAILABLE",
            "ticker": "MU",
            "message": "未获取到 MU 的分析结果",
            "diagnostics": {"failure_reasons": ["missing_current_price"]},
        },
    )

    response = client.get("/agent/analyze", params={"ticker": "MU", "deep": 0})

    assert response.status_code == 404
    payload = response.json()
    assert payload["detail"]["code"] == "ANALYSIS_DATA_UNAVAILABLE"
    assert payload["detail"]["ticker"] == "MU"
    assert "未获取到 MU 的分析结果" in payload["detail"]["message"]
    assert "diagnostics" in payload["detail"]
