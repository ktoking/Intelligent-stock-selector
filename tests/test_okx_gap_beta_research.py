from __future__ import annotations

import pandas as pd
import pytest

from scripts.okx_gap_beta_research import attach_causal_gap_beta


def _row(symbol: str, day: int, gap: float) -> dict:
    return {
        "symbol": symbol,
        "date": f"2026-01-{day:02d}",
        "gap_bps": gap,
        "first5_bps": gap / 2.0,
        "previous_day_bps": gap / 4.0,
    }


def test_causal_beta_uses_prior_sessions_only_and_shrinks_to_one():
    rows = []
    for day in range(1, 7):
        spy_gap = 10.0 * day
        rows.append(_row("SPY-USDT-SWAP", day, spy_gap))
        stock_gap = 2.0 * spy_gap if day < 6 else -10_000.0
        rows.append(_row("TEST-USDT-SWAP", day, stock_gap))
    value = attach_causal_gap_beta(
        pd.DataFrame(rows), lookback_sessions=40, prior_strength=20.0, min_sessions=5,
    )
    current = value[(value.symbol == "TEST-USDT-SWAP") & (value.date == "2026-01-06")].iloc[0]
    # Five prior observations imply raw beta=2 and shrinkage weight=5/(5+20).
    assert current.causal_gap_beta == pytest.approx(1.2)
    assert current.relative_gap == pytest.approx(-10_000.0 - 1.2 * 60.0)


def test_causal_beta_defaults_to_one_before_minimum_history():
    rows = pd.DataFrame([
        _row("SPY-USDT-SWAP", 1, 10.0),
        _row("TEST-USDT-SWAP", 1, 30.0),
    ])
    value = attach_causal_gap_beta(rows, min_sessions=5)
    assert value.iloc[0].causal_gap_beta == 1.0
    assert value.iloc[0].relative_gap == 20.0
