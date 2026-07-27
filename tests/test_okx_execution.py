from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from types import SimpleNamespace

from scripts.okx_candidate_ws import (
    Engine, breakout_held, exchange_exit_reason, in_opening_reversal_window, position_notional_usdt, protection_prices_from_fill, r_multiple, select_dynamic_leverage, session_allows_entry, side_reversed, side_still_valid,
)
from scripts.okx_intraday_agent import (
    _DEMO_TRADEABLE_CACHE,
    aggregate_closed_candles,
    demo_instrument_tradeable,
    estimated_slippage_bps,
    next_scan_boundary,
    risk_sized_order,
    spread_bps,
    today_pnl_line,
)
from scripts.okx_trade_replay import aggregate_10m, analyze_path
from scripts.okx_multitimeframe_backtest import StrategyConfig, aggregate_bars, deployment_viable, stats, weekday_sessions
from scripts.okx_walkforward_train import _portfolio_filter
from scripts.okx_return_model_research import frame as return_model_frame


def test_long_candidate_requires_trend_vwap_and_volume():
    metrics = {"ema9": 101, "ema21": 100, "price": 102, "vwap20": 101, "volume_ratio": 1.2}
    assert side_still_valid("LONG", metrics)[0]
    metrics["volume_ratio"] = 0.8
    assert not side_still_valid("LONG", metrics)[0]


def test_breakout_confirmation_works_for_both_sides():
    assert breakout_held({"side": "LONG", "breakout_high": 100}, 100.1)[0]
    assert not breakout_held({"side": "LONG", "breakout_high": 100}, 99.9)[0]
    assert breakout_held({"side": "SHORT", "breakout_low": 100}, 99.9)[0]


def test_spread_and_book_slippage_are_bps():
    assert round(spread_bps({"bidPx": "99", "askPx": "101"}), 2) == 200.0
    book = {"asks": [["100", "2"], ["101", "2"]], "bids": [["99", "2"]]}
    assert estimated_slippage_bps(book, "LONG", 4, 1) == 50.0
    assert estimated_slippage_bps(book, "SHORT", 3, 1) == float("inf")


def test_position_size_respects_contract_value_and_notional_cap():
    cfg = SimpleNamespace(quote_risk_usdt=25, risk_fraction=0.0025, max_notional_usdt=250)
    balance = {"totalEq": "10000"}
    instrument = {"ctVal": "0.01", "lotSz": "1", "minSz": "1"}
    result = risk_sized_order(cfg, balance, instrument, price=100, atr14=1)
    assert result["contracts"] == "250"
    assert result["notional"] == 250


def test_scan_is_aligned_to_wall_clock_ten_minute_boundary():
    now = datetime(2026, 7, 17, 8, 53, 42, tzinfo=timezone.utc)
    assert next_scan_boundary(now) == datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc)
    boundary = datetime(2026, 7, 17, 9, 0, 0, tzinfo=timezone.utc)
    assert next_scan_boundary(boundary) == datetime(2026, 7, 17, 9, 10, tzinfo=timezone.utc)


def test_scanner_builds_full_ten_minute_bars_from_newest_first_five_minute_data():
    candles = [
        ["900000", "103", "105", "102", "104", "4", "4", "416", "1"],
        ["600000", "102", "104", "101", "103", "3", "3", "309", "1"],
        ["300000", "101", "103", "99", "102", "2", "2", "204", "1"],
        ["0", "100", "102", "98", "101", "1", "1", "101", "1"],
    ]

    result = aggregate_closed_candles(candles, 10)

    assert len(result) == 2
    assert result[0][:6] == ["0", "100", "103.0", "98.0", "102", "3.0"]
    assert result[1][:6] == ["600000", "102", "105.0", "101.0", "104", "7.0"]


def test_return_model_entry_times_are_real_unix_milliseconds():
    start = int(datetime(2026, 7, 17, 13, 30, tzinfo=timezone.utc).timestamp() * 1000)
    rows = [[str(start + minute * 60_000), "100", "101", "99", "100", "1", "1", "100", "1"]
            for minute in range(300)]

    result = return_model_frame(rows, "TEST")

    assert len(result) >= 2
    assert result.iloc[0]["entry_time"] > 1_000_000_000_000
    assert result.iloc[1]["entry_time"] - result.iloc[0]["entry_time"] == 30 * 60_000


def test_demo_24x7_session_allows_equity_token_entry():
    assert session_allows_entry(SimpleNamespace(session="24x7"), "AAPL-USDT-SWAP")


def test_unavailable_demo_instrument_is_filtered():
    class Client:
        def max_size(self, _inst_id):
            raise RuntimeError("51001 Instrument ID doesn't exist")

    _DEMO_TRADEABLE_CACHE.pop("NOPE-USDT-SWAP", None)
    assert not demo_instrument_tradeable(Client(), "NOPE-USDT-SWAP")


def test_ema_and_vwap_must_both_reverse_before_early_exit():
    assert not side_reversed("LONG", {"ema9": 99, "ema21": 100, "price": 101, "vwap20": 100})
    assert side_reversed("LONG", {"ema9": 99, "ema21": 100, "price": 99, "vwap20": 100})
    assert side_reversed("SHORT", {"ema9": 101, "ema21": 100, "price": 101, "vwap20": 100})


def test_r_multiple_is_directional_and_uses_initial_stop():
    assert r_multiple("LONG", entry=100, initial_stop=98, mark=102) == 1
    assert round(r_multiple("SHORT", entry=100, initial_stop=102, mark=97.6), 6) == 1.2


def test_exchange_oco_exit_reason_uses_protection_prices():
    trade = {"active_stop_px": 98, "target_px": 104, "tick_size": "0.01"}
    assert exchange_exit_reason(trade, 104.01) == "交易所 OCO 止盈触发"
    assert exchange_exit_reason(trade, 98.0) == "交易所 OCO 止损触发"


def test_protection_prices_are_rebased_to_actual_fill():
    assert protection_prices_from_fill("LONG", 101, 2) == (99, 104.6)
    assert protection_prices_from_fill("SHORT", 101, 2) == (103, 97.4)


def test_position_notional_prefers_exchange_value_and_has_a_contract_fallback():
    assert position_notional_usdt({"notionalUsd": "-1500"}) == 1500
    assert position_notional_usdt({"pos": "2", "markPx": "100", "ctVal": "0.1"}) == 20


def test_runner_stop_lookup_uses_the_expected_side_and_algo_id():
    algos = [
        {"algoId": "old", "posSide": "long", "slTriggerPx": "98"},
        {"algoId": "runner", "posSide": "long", "slTriggerPx": "101"},
    ]
    assert Engine._stop_algo(algos, "long", "runner")["slTriggerPx"] == "101"
    assert Engine._stop_algo(algos, "short") is None


def test_today_pnl_line_combines_completed_and_open_pnl():
    now_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))

    class Client:
        def request(self, _method, path, *_args, **_kwargs):
            if path.endswith("positions-history"):
                return {"data": [{"uTime": now_ms, "realizedPnl": "1.25"}]}
            return {"data": [{"pos": "1", "upl": "-0.5"}]}

    text = today_pnl_line(Client())
    assert "已实现 🔴 +$1.2500" in text
    assert "持仓浮盈亏 🟢 -$0.5000" in text
    assert "合计 🔴 +$0.7500" in text


def test_dynamic_leverage_only_increases_for_liquid_high_quality_candidates():
    assert select_dynamic_leverage({"score": 86, "volume_ratio": 2.1, "spread_bps": 1.5, "estimated_slippage_bps": 1.5})[0] == 5
    assert select_dynamic_leverage({"score": 76, "volume_ratio": 1.6, "spread_bps": 4.9, "estimated_slippage_bps": 4.9})[0] == 4
    assert select_dynamic_leverage({"score": 90, "volume_ratio": 2.5, "spread_bps": 1, "estimated_slippage_bps": 1, "event_risk": True})[0] == 3


def test_opening_reversal_window_is_limited_to_the_first_thirty_cash_minutes():
    ny = ZoneInfo("America/New_York")
    assert in_opening_reversal_window(datetime(2026, 7, 17, 9, 30, tzinfo=ny))
    assert in_opening_reversal_window(datetime(2026, 7, 17, 9, 59, tzinfo=ny))
    assert not in_opening_reversal_window(datetime(2026, 7, 17, 10, 0, tzinfo=ny))


def test_two_closed_five_minute_bars_form_one_ten_minute_bar():
    rows = [
        ["600000", "100", "103", "99", "102", "10", "20", "30", "1"],
        ["900000", "102", "104", "101", "103", "11", "21", "31", "1"],
    ]
    result = aggregate_10m(rows)
    assert result == [["600000", "100", "104.0", "99.0", "103", "21.0", "41.0", "61.0", "1"]]


def test_trade_path_reports_directional_mfe_mae_and_false_breakout():
    position = {
        "cTime": "600000", "uTime": "780000", "openAvgPx": "100",
        "closeAvgPx": "99", "posSide": "long",
    }
    rows_1m = [
        ["540000", "99", "100", "99", "100", "1", "1", "1", "1"],
        ["600000", "100", "101", "99", "99.5", "1", "1", "1", "1"],
        ["660000", "99.5", "102", "99", "101", "1", "1", "1", "1"],
        ["720000", "101", "101", "98", "99", "1", "1", "1", "1"],
        ["780000", "99", "100", "98", "99", "1", "1", "1", "1"],
    ]
    result = analyze_path(position, rows_1m, [])
    assert result["mfe_bps"] == 200
    assert result["mae_bps"] == 200
    assert result["failed_breakout_first_3m"]


def test_one_minute_bars_aggregate_without_lookahead():
    rows = []
    for minute in range(5):
        rows.append([str(600_000 + minute * 60_000), "100", str(101 + minute), "99", str(100 + minute), "1", "1", "1", "1"])
    result = aggregate_bars(rows, 5)
    assert len(result) == 1
    assert result[0][0] == "600000"
    assert result[0][2] == "105.0"
    assert result[0][4] == "104"


def test_backtest_stats_are_net_cost_results():
    trades = [
        {"net_r": 1.0, "exit_time": 1, "scaled": False},
        {"net_r": -0.5, "exit_time": 2, "scaled": False},
    ]
    result = stats(trades)
    assert result["win_rate"] == 50
    assert result["net_r"] == 0.5
    assert result["profit_factor"] == 2


def test_backtest_does_not_deploy_a_training_or_validation_loser():
    candidate = {
        "train": {"net_r": 2, "profit_factor": 1.3},
        "validation": {"net_r": -1, "profit_factor": 0.8},
    }
    assert not deployment_viable(candidate)
    candidate["validation"] = {"net_r": 1, "profit_factor": 1.1}
    assert deployment_viable(candidate)


def test_ninety_session_window_is_weekdays_and_has_exact_size():
    sessions = weekday_sessions(datetime(2026, 7, 17).date(), 90)
    assert len(sessions) == 90
    assert sessions[-1].isoformat() == "2026-07-17"
    assert all(day.weekday() < 5 for day in sessions)


def test_walkforward_portfolio_never_exceeds_position_cap():
    import numpy as np
    import pandas as pd

    rows = pd.DataFrame([
        {"entry_time": 1000, "exit_time": 5000, "net_r": 1, "scaled": False}
        for _ in range(8)
    ])
    selected = _portfolio_filter(rows, np.ones(8), threshold=0.5, max_positions=5)
    assert len(selected) == 5
