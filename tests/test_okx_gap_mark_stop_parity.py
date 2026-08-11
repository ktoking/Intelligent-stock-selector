from __future__ import annotations

import pandas as pd

from scripts.okx_gap_mark_stop_parity import (
    normalized_mark_path,
    replay_mark_stop,
    research,
)


def _trade(day: str = "d1", symbol: str = "A") -> dict:
    return {
        "date": day, "symbol": symbol, "entry_time": 1_000_000,
        "exit_time": 2_800_000, "exit_time_basis": "first_triggering_5m_bar_end",
        "stop_bar_number": 1, "side": "LONG", "relative_gap_bps": -200.0,
        "horizon_minutes": 30, "prior_side_samples": 10,
        "prior_best_horizon_expectancy_pct": 0.2, "stop_bps": 100.0,
        "net_pct": -1.14, "exit_reason": "atr_stop",
    }


def _mark_rows(*, low: float = 99.5) -> list[list[str]]:
    return [
        [str(1_000_000 + index * 300_000), "100", "100.5", str(low), "100", "1"]
        for index in range(6)
    ]


def test_normalized_mark_path_requires_exact_confirmed_horizon_bars():
    rows = _mark_rows()
    rows[2][-1] = "0"
    assert len(normalized_mark_path(_trade(), rows)) == 5


def test_mark_stop_replay_uses_mark_extreme_not_trade_candle_extreme():
    row = pd.Series({"entry": 100.0, "exit_30": 101.0})
    no_stop = replay_mark_stop(_trade(), row, _mark_rows(low=99.5), round_trip_cost_bps=14)
    stopped = replay_mark_stop(_trade(), row, _mark_rows(low=98.9), round_trip_cost_bps=14)

    assert no_stop["exit_reason"] == "horizon"
    assert no_stop["net_pct"] == 0.86
    assert stopped["exit_reason"] == "mark_price_atr_stop"
    assert stopped["net_pct"] == -1.14


def test_research_reports_coverage_and_never_promotes():
    trades = [_trade("d1", "A"), _trade("d2", "B")]
    source = {
        "generated_at": "source",
        "trade_filter": {"round_trip_cost_bps": 14},
        "trades": trades,
    }
    features = pd.DataFrame([
        {"date": "d1", "symbol": "A", "entry": 100.0, "exit_30": 101.0},
        {"date": "d2", "symbol": "B", "entry": 100.0, "exit_30": 101.0},
    ])
    value = research(features, source, {("d1", "A"): _mark_rows()}, generated_at="now")

    assert value["coverage"]["source_trades"] == 2
    assert value["coverage"]["covered_trades"] == 1
    assert value["coverage"]["missing"][0]["symbol"] == "B"
    assert value["research_governance"]["explicit_trial_count"] == 1
    assert value["promotion_passed"] is False
