"""Cross-symbol hourly proxy comparison for more active main-wave settings.

This is research only. It does not emulate Futu's 5-minute Canvas execution.
"""

from __future__ import annotations

from dataclasses import asdict
import json

import pandas as pd
import yfinance as yf

from research.soxl_regime.main_wave_hourly import Config, simulate


def _download(symbol: str, interval: str) -> pd.DataFrame:
    frame = yf.download(
        symbol, period="730d" if interval == "60m" else "5y",
        interval=interval, auto_adjust=True, prepost=False,
        progress=False, threads=False,
    )
    if isinstance(frame.columns, pd.MultiIndex):
        frame = frame.xs(symbol, axis=1, level=1)
    frame.columns = [str(column).lower() for column in frame.columns]
    frame = frame[["open", "high", "low", "close", "volume"]].dropna()
    if interval == "60m":
        frame.index = frame.index.tz_convert("America/New_York")
        frame = frame.between_time("09:30", "16:00")
    else:
        frame.index = pd.to_datetime(frame.index.date)
    if frame.empty or frame.index.has_duplicates:
        raise ValueError(f"missing or duplicate {interval} data: {symbol}")
    return frame


def evaluate() -> dict:
    configurations = {
        "v2_strict": Config(regime="strict", position=.8, trail_pct=.12,
                            stop_pct=.08, soxl_slope_days=5,
                            cooldown_sessions=7, max_extension=.25),
        "cooldown_3": Config(regime="strict", position=.8, trail_pct=.12,
                             stop_pct=.08, soxl_slope_days=5,
                             cooldown_sessions=3, max_extension=.25),
        "active_strict": Config(regime="strict", position=.8, trail_pct=.08,
                                stop_pct=.06, soxl_slope_days=5,
                                cooldown_sessions=1, max_extension=.25),
        "active_broad": Config(regime="broad", position=.8, trail_pct=.08,
                               stop_pct=.06, soxl_slope_days=0,
                               cooldown_sessions=1, max_extension=.25),
    }
    pairs = {"SOXL": ("QQQ", "2024-09-25"),
             "ARMG": ("ARM", "2025-05-01"),
             "SNXX": ("SNDK", "2026-05-01")}
    rows = {}
    for symbol, (reference, start) in pairs.items():
        hourly = _download(symbol, "60m")
        target_daily = _download(symbol, "1d")
        reference_daily = _download(reference, "1d")
        daily = target_daily.add_prefix("s_").join(
            reference_daily.add_prefix("q_"), how="inner")
        windows = {"available": (start, "2026-09-22")}
        if symbol == "SOXL":
            windows["prior_year"] = ("2024-09-25", "2025-09-19")
            windows["2026_up"] = ("2026-01-02", "2026-06-30")
            windows["2026_down"] = ("2026-07-01", "2026-09-22")
        else:
            windows["2026_up"] = ("2026-01-02", "2026-06-30")
            windows["2026_down"] = ("2026-07-01", "2026-09-22")
        rows[symbol] = {
            "reference": reference,
            "hourly_data": [str(hourly.index.min()), str(hourly.index.max()), len(hourly)],
            "daily_data": [str(daily.index.min()), str(daily.index.max()), len(daily)],
            "configs": {
                name: {window: simulate(hourly, daily, config, *dates)[2]
                       for window, dates in windows.items()}
                for name, config in configurations.items()
            },
        }
    return {"status": "offline_hourly_proxy_not_futu_backtest",
            "cost_bps_per_side": 10,
            "configurations": {name: asdict(value) for name, value in configurations.items()},
            "symbols": rows}


if __name__ == "__main__":
    print(json.dumps(evaluate(), ensure_ascii=False, indent=2))
