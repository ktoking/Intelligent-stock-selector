from datetime import date

import pandas as pd

from scripts.okx_strategy_weekly_replay import package_weekly_report, week_start


def _trade(day: str, symbol: str, relative_gap: float, net_pct: float = -0.89):
    return {
        "date": day, "symbol": symbol, "entry_time": 1_000_000,
        "exit_time": 1_300_000, "side": "SHORT", "relative_gap_bps": relative_gap,
        "horizon_minutes": 5, "stop_bps": 75.0, "net_pct": net_pct,
        "exit_reason": "atr_stop", "stop_bar_number": 1,
    }


def test_week_start_is_monday():
    assert week_start(date(2026, 8, 12)).isoformat() == "2026-08-10"


def test_weekly_report_uses_strict_subset_without_backfill_and_mark_stop():
    rows = pd.DataFrame([
        {"date": "2026-08-10", "symbol": "A", "relative_gap": 200.0,
         "relative_first5": -20.0, "entry": 100.0, "exit_5": 99.0},
        {"date": "2026-08-10", "symbol": "B", "relative_gap": 180.0,
         "relative_first5": 10.0, "entry": 100.0, "exit_5": 99.0},
    ])
    report = {
        "effective_sessions": {"count": 2, "start": "2026-08-07", "end": "2026-08-10"},
        "universe_size": 2,
        "trade_filter": {"round_trip_cost_bps": 14.0},
        "trades": [_trade("2026-08-10", "A", 200), _trade("2026-08-10", "B", 180)],
    }
    mark_rows = [["1000000", "100", "101", "99", "100", "1"]]

    value = package_weekly_report(
        rows, report, {("2026-08-10", "A"): mark_rows},
        end=date(2026, 8, 10), generated_at="fixed",
    )

    assert value["v5"]["metrics"]["trades"] == 2
    assert value["strict_mark_price"]["metrics"]["trades"] == 1
    assert value["strict_mark_price"]["trades"][0]["symbol"] == "A"
    assert value["strict_mark_price"]["trades"][0]["exit_reason"] == "mark_price_atr_stop"
    assert value["execution_enabled"] is False
