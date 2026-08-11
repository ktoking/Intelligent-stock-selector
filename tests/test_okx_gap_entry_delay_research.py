from __future__ import annotations

import pandas as pd
import pytest

from scripts.okx_gap_entry_delay_research import (
    _session_record,
    causal_delay_trades,
    clock_for_open_offset,
    confirmation_clock,
    development_eligible,
    exit_bar_clock,
)


def _session(clocks: list[str], base: float = 100.0) -> pd.DataFrame:
    rows = []
    for index, clock in enumerate(clocks):
        price = base + index
        rows.append({
            "clock": clock,
            "ts": 1_000_000 + index * 300_000,
            "open": price,
            "high": price + 0.5,
            "low": price - 0.5,
            "close": price + 0.25,
        })
    return pd.DataFrame(rows).set_index("clock")


def test_delayed_clock_mapping_uses_next_bar_open_and_true_horizon_close():
    assert confirmation_clock(10) == "09:35"
    assert clock_for_open_offset(10) == "09:40"
    assert exit_bar_clock(10, 30) == "10:05"
    assert exit_bar_clock(15, 90) == "11:10"
    with pytest.raises(ValueError):
        clock_for_open_offset(7)


def test_session_record_enters_after_confirmation_and_replays_from_entry():
    previous = _session(["09:30", "15:55"], base=90.0)
    current_clocks = [clock_for_open_offset(value) for value in range(0, 105, 5)]
    current = _session(current_clocks, base=100.0)
    value = _session_record("TEST-USDT-SWAP", "2026-01-02", previous, current, 10)
    assert value is not None
    assert value["entry"] == current.loc["09:40"].open
    assert value["entry_time"] == current.loc["09:40"].ts
    assert value["first5_bps"] == pytest.approx(
        (current.loc["09:35"].close / current.loc["09:30"].open - 1.0) * 10_000.0
    )
    assert value["exit_30"] == current.loc["10:05"].close
    assert value["exit_90"] == current.loc["11:05"].close
    assert value["path_150"][0] == (
        current.loc["09:40"].high,
        current.loc["09:40"].low,
        current.loc["09:40"].close,
    )
    assert len(value["path_150"]) == 18


def test_development_gate_ignores_diagnostic_and_requires_every_selection_fold():
    good = {
        name: {"trades": 15, "equal_weight_net_pct": 1.0, "profit_factor": 1.11}
        for name in ("validation_a", "validation_b", "validation_c")
    }
    good["diagnostic"] = {"trades": 100, "equal_weight_net_pct": -99.0, "profit_factor": 0.1}
    assert development_eligible(good)
    good["validation_b"] = {"trades": 14, "equal_weight_net_pct": 1.0, "profit_factor": 2.0}
    assert not development_eligible(good)


def test_causal_delay_selector_cannot_see_current_day_outcome():
    days = [f"day-{index:02d}" for index in range(21)]
    results = {}
    for delay, prior_net in (("5", 1.0), ("10", 0.2), ("15", -0.2)):
        history = [
            {"date": day, "side": "LONG", "net_pct": prior_net}
            for day in days[:20]
        ]
        # The current-day 15m paper result is enormous, but it is not allowed
        # to change the delay selected before that session.
        history.append({
            "date": days[20], "side": "LONG",
            "net_pct": 100.0 if delay == "15" else -100.0,
        })
        results[delay] = {
            "selection_history": history,
            "trades": [{
                "date": days[20], "side": "LONG", "relative_gap_bps": -200.0,
                "net_pct": prior_net, "stop_bps": 100.0,
            }],
        }
    selected = causal_delay_trades(results, days, by_side=False)
    assert len(selected) == 1
    assert selected[0]["selected_delay_minutes"] == 5
