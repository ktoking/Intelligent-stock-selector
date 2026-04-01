"""data.universe 纳指100 拉取：解析逻辑单测（网络请求 mock）。"""
from unittest.mock import MagicMock, patch

from data.universe import get_nasdaq100_tickers_from_nasdaq_api


def test_get_nasdaq100_tickers_from_nasdaq_api_parses_rows():
    rows = [{"symbol": f"S{i:04d}"} for i in range(55)]
    rows.append({"symbol": "BRK.B"})
    payload = {"data": {"data": {"rows": rows}}}
    mock_resp = MagicMock()
    mock_resp.json.return_value = payload
    mock_resp.raise_for_status = MagicMock()
    with patch("data.universe.requests.get", return_value=mock_resp):
        out = get_nasdaq100_tickers_from_nasdaq_api()
    assert out is not None
    assert len(out) == 56
    assert "BRK-B" in out
    assert "S0000" in out


def test_get_nasdaq100_tickers_from_nasdaq_api_short_list_returns_none():
    payload = {"data": {"data": {"rows": [{"symbol": "AAPL"}, {"symbol": "MSFT"}]}}}
    mock_resp = MagicMock()
    mock_resp.json.return_value = payload
    mock_resp.raise_for_status = MagicMock()
    with patch("data.universe.requests.get", return_value=mock_resp):
        assert get_nasdaq100_tickers_from_nasdaq_api() is None
