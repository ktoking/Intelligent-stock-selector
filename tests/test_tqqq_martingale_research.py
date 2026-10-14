import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from scripts.tqqq_martingale_research import Rule, _quality, backtest, fetch_history


def test_add_and_take_profit_fill_at_next_open():
    bars = [
        {"date": "2026-07-08", "open": 100, "close": 94},
        {"date": "2026-07-08", "open": 90, "close": 100},
        {"date": "2026-07-08", "open": 105, "close": 105},
    ]
    result = backtest(bars, Rule(0.05, 0.05, 1.0, 1), cost_bps=0)
    assert [trade["side"] for trade in result["trades"]] == ["BUY", "ADD", "SELL"]
    assert result["trades"][1]["price"] == 90
    assert result["trades"][2]["price"] == 105
    assert result["cycles"] == result["winning_cycles"] == 1
    assert result["open_shares"] == 0


def test_open_loser_is_in_equity_and_drawdown():
    bars = [{"date": "2026-07-08", "open": 100, "close": 100},
            {"date": "2026-07-09", "open": 80, "close": 80}]
    result = backtest(bars, Rule(0.05, 0.10, 1.0, 0), cost_bps=0)
    assert result["cycles"] == 0
    assert result["open_unrealized_usd"] == -200
    assert result["return_pct"] == -2
    assert result["max_drawdown_pct"] == 2
    assert result["shock_minus_20pct_return_pct"] == -3.6
    assert result["worst_historical_20pct_shock_return_pct"] == -3.6


def test_cached_futunn_history_includes_the_1600_closing_bar(tmp_path, monkeypatch):
    day = date(2026, 7, 8)
    end = date(2026, 7, 9)
    def stamp(hour, minute):
        return int(datetime(2026, 7, 8, hour, minute,
                            tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc).timestamp() * 1000)
    rows = [{"time_key": stamp(hour, minute), "open": 100, "high": 101,
             "low": 99, "close": 100, "volume": 10}
            for hour, minute in [(9, 35), (16, 0)]]
    cache = tmp_path / "US.TQQQ_5m_2026-07-08_2026-07-10.json"
    cache.write_text(json.dumps({"kline_list": rows}))
    monkeypatch.delenv("FUTUNN_APP_KEY", raising=False)
    monkeypatch.delenv("FUTUNN_PRIVATE_KEY_FILE", raising=False)
    result = fetch_history("US.TQQQ", day, end, tmp_path)
    assert len(result) == 2
    assert _quality(result)["last_bar_ny"] == "16:00"
