import math
from typing import Optional, Tuple


def safe_float(v) -> Optional[float]:
    """将值转为 float，若结果为 NaN/Inf 则返回 None。"""
    if v is None:
        return None
    try:
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None


def latest_valid_close_pair(hist) -> Tuple[Optional[float], Optional[float]]:
    """返回最近两个有效收盘价，忽略 Yahoo 尾部 NaN 空 K 线。"""
    if hist is None or not hasattr(hist, "columns") or "Close" not in hist.columns:
        return None, None
    try:
        closes = hist["Close"].dropna()
        if len(closes) == 0:
            return None, None
        current = safe_float(closes.iloc[-1])
        prev = safe_float(closes.iloc[-2]) if len(closes) >= 2 else None
        return current, prev
    except Exception:
        return None, None


def last_close_is_invalid(hist) -> bool:
    """判断历史K线最后一行是否存在但收盘价为空。"""
    if hist is None or not hasattr(hist, "columns") or "Close" not in hist.columns or len(hist) == 0:
        return False
    try:
        return safe_float(hist["Close"].iloc[-1]) is None
    except Exception:
        return False


def drop_invalid_ohlc_rows(hist):
    """清理 OHLC 关键列为空的空 K 线。"""
    if hist is None or not hasattr(hist, "dropna"):
        return hist
    try:
        if not hasattr(hist, "columns"):
            return hist
        required = [col for col in ("Close", "High", "Low") if col in hist.columns]
        if len(required) < 3:
            return hist
        return hist.dropna(subset=required).copy()
    except Exception:
        return hist
