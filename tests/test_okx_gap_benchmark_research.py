from __future__ import annotations

import pandas as pd
import pytest

from scripts.okx_gap_benchmark_research import apply_benchmark


def test_apply_benchmark_uses_weights_and_excludes_benchmark_instruments():
    common = {"date": "2026-01-02", "first5_bps": 0.0, "previous_day_bps": 0.0}
    rows = pd.DataFrame([
        {**common, "symbol": "SPY-USDT-SWAP", "gap_bps": 20.0},
        {**common, "symbol": "QQQ-USDT-SWAP", "gap_bps": 40.0},
        {**common, "symbol": "NVDA-USDT-SWAP", "gap_bps": 130.0},
    ])
    value = apply_benchmark(rows, {"SPY": .5, "QQQ": .5})
    assert value.symbol.tolist() == ["NVDA-USDT-SWAP"]
    assert value.iloc[0].spy_gap == pytest.approx(30.0)
    assert value.iloc[0].relative_gap == pytest.approx(100.0)
