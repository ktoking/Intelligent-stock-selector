from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class RegimeConfig:
    enter_spread: float = 0.0075
    exit_spread: float = 0.0025
    slope_threshold: float = 0.0025
    range_spread: float = 0.006
    range_slope: float = 0.004
    range_width: float = 0.14
    panic_atr: float = 0.045
    persistence: int = 2


def classify(features: pd.DataFrame, config: RegimeConfig) -> pd.Series:
    state = "TRANSITION"
    candidate = state
    count = 0
    values: list[str] = []
    for row in features.itertuples():
        proposed = "TRANSITION"
        if pd.notna(row.atr14_pct) and row.atr14_pct >= config.panic_atr:
            proposed = "HIGH_VOL"
        elif row.ema_spread >= config.enter_spread and row.ema20_slope > config.slope_threshold and row.close > row.ema20:
            proposed = "TREND_UP"
        elif row.ema_spread <= -config.enter_spread and row.ema20_slope < -config.slope_threshold and row.close < row.ema20:
            proposed = "TREND_DOWN"
        elif abs(row.ema_spread) <= config.range_spread and abs(row.ema20_slope) <= config.range_slope and row.boll_width <= config.range_width:
            proposed = "RANGE"
        elif state == "TREND_UP" and row.ema_spread >= config.exit_spread:
            proposed = "TREND_UP"
        elif state == "TREND_DOWN" and row.ema_spread <= -config.exit_spread:
            proposed = "TREND_DOWN"
        if proposed == state:
            candidate, count = proposed, 0
        elif proposed == candidate:
            count += 1
        else:
            candidate, count = proposed, 1
        if candidate != state and count >= config.persistence:
            state, count = candidate, 0
        values.append(state)
    return pd.Series(values, index=features.index, name="regime")

