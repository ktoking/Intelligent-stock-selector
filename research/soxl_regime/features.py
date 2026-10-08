from __future__ import annotations

import numpy as np
import pandas as pd


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    change = close.diff()
    gain = change.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-change.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    value = 100 - 100 / (1 + rs)
    value = value.where(~((loss == 0) & (gain > 0)), 100.0)
    value = value.where(~((loss == 0) & (gain == 0)), 50.0)
    return value.fillna(50.0)


def add_features(frame: pd.DataFrame, *, slope_bars: int = 6, boll_std: float = 1.75) -> pd.DataFrame:
    result = frame.copy()
    close = result["close"].astype(float)
    result["ema20"] = close.ewm(span=20, adjust=False, min_periods=20).mean()
    result["ema60"] = close.ewm(span=60, adjust=False, min_periods=60).mean()
    result["ema_spread"] = result["ema20"] / result["ema60"] - 1
    result["ema20_slope"] = result["ema20"].pct_change(slope_bars)
    result["boll_mid"] = close.rolling(20, min_periods=20).mean()
    sd = close.rolling(20, min_periods=20).std(ddof=0)
    result["boll_upper"] = result["boll_mid"] + boll_std * sd
    result["boll_lower"] = result["boll_mid"] - boll_std * sd
    result["boll_width"] = (result["boll_upper"] - result["boll_lower"]) / result["boll_mid"]
    result["rsi14"] = rsi(close, 14)
    previous_close = close.shift(1)
    true_range = pd.concat([
        result["high"] - result["low"],
        (result["high"] - previous_close).abs(),
        (result["low"] - previous_close).abs(),
    ], axis=1).max(axis=1)
    result["atr14_pct"] = true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean() / close
    if result.index.tz is not None:
        session = result.index.strftime("%Y-%m-%d")
        day_open = result["open"].groupby(session).transform("first")
        result["day_drop"] = 1 - close / day_open
        result["is_last_bar"] = pd.Series(session, index=result.index).ne(pd.Series(session, index=result.index).shift(-1))
    return result
