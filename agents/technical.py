"""
技术面：从 yfinance 历史数据计算 MA、MACD、KDJ、RSI、量能、ATR%，并给出数值摘要供 LLM 解读。
支持日K（1d）与分K（1m/5m/15m），可选盘前盘后（prepost）。
入场/离场规则可配置：ATR 止损倍数、放量突破阈值（见环境变量）。
"""
import os
import pandas as pd
import yfinance as yf
from typing import Optional

# 分K 默认拉取周期（yfinance 限制：1m 最多约 7d，5m/15m 最多约 60d）
_INTERVAL_DEFAULT_PERIOD = {"1d": "6mo", "1m": "5d", "5m": "5d", "15m": "5d", "30m": "5d", "60m": "5d"}
# 分K 最少需要根数（MA60 需 60 根，分K 可放宽到 30）
_MIN_BARS_DAILY = 60
_MIN_BARS_INTRADAY = 30

# 入场/离场可调：ATR 止损倍数（离场）；放量突破阈值（量比 >= 该值且突破近期高点视为放量突破）
_TECH_ATR_STOP_MULT = float(os.environ.get("TECH_ATR_STOP_MULT", "1.5").strip() or "1.5")
_TECH_VOLUME_BREAKOUT_RATIO = float(os.environ.get("TECH_VOLUME_BREAKOUT_RATIO", "1.5").strip() or "1.5")


def _macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def _kdj(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 9, m1: int = 3, m2: int = 3):
    low_min = low.rolling(n).min()
    high_max = high.rolling(n).max()
    rsv = (close - low_min) / (high_max - low_min + 1e-10) * 100
    k = rsv.ewm(com=m1 - 1, adjust=False).mean()
    d = k.ewm(com=m2 - 1, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI(14)：相对强弱，>70 超买，<30 超卖。"""
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-10)
    return 100 - (100 / (1 + rs))


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """ATR(14)，用于止损参考。"""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def _compute_entry_exit_levels(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    ma20: Optional[float],
    ma60: Optional[float],
    price: float,
    is_daily: bool,
    volume_ratio_tech: Optional[float] = None,
) -> dict:
    """
    基于均线、波动(ATR/ATR%)与量能计算技术面入场/离场参考，供报告展示与 LLM 评估。
    规则可调：TECH_ATR_STOP_MULT（ATR 止损倍数）、TECH_VOLUME_BREAKOUT_RATIO（放量突破量比阈值）。
    """
    out = {
        "support_ma20": None,
        "support_ma60": None,
        "resistance_20d": None,
        "atr": None,
        "atr_pct": None,
        "entry_note": "",
        "exit_note": "",
    }
    if not is_daily or len(close) < 20:
        return out

    lookback = min(20, len(high) - 1)
    resistance_20d = float(high.iloc[-lookback:].max()) if lookback > 0 else None
    atr_series = _atr(high, low, close)
    atr_val = float(atr_series.iloc[-1]) if len(atr_series) >= 14 else None
    atr_pct = round(atr_val / price * 100, 2) if (atr_val is not None and price and price > 0) else None

    out["support_ma20"] = round(ma20, 2) if ma20 is not None else None
    out["support_ma60"] = round(ma60, 2) if ma60 is not None else None
    out["resistance_20d"] = round(resistance_20d, 2) if resistance_20d is not None else None
    out["atr"] = round(atr_val, 2) if atr_val is not None else None
    out["atr_pct"] = atr_pct

    mult = _TECH_ATR_STOP_MULT
    exit_parts = []
    if ma20 is not None:
        exit_parts.append(f"跌破 MA20 约 {ma20:.2f} 考虑减仓")
    if ma60 is not None:
        exit_parts.append(f"跌破 MA60 约 {ma60:.2f} 考虑离场")
    if atr_val is not None and atr_val > 0:
        stop_approx = price - atr_val * mult
        exit_parts.append(f"或收盘跌破约 {stop_approx:.2f}（{mult}×ATR 止损）离场")
    out["exit_note"] = "；".join(exit_parts) if exit_parts else "—"

    entry_parts = []
    if ma20 is not None:
        entry_parts.append(f"突破/站稳 MA20 约 {ma20:.2f} 可考虑入场")
    if resistance_20d is not None and resistance_20d > price:
        entry_parts.append(f"突破近期高点约 {resistance_20d:.2f} 为强势信号")
    if (
        volume_ratio_tech is not None
        and volume_ratio_tech >= _TECH_VOLUME_BREAKOUT_RATIO
        and resistance_20d is not None
        and resistance_20d > price
    ):
        entry_parts.append(f"若放量（量比≥{_TECH_VOLUME_BREAKOUT_RATIO}）突破近期高点更佳")
    out["entry_note"] = "；".join(entry_parts) if entry_parts else "—"

    return out


def _build_tech_status_one_line(
    long_align: bool,
    macd_above_zero: bool,
    macd_golden: bool,
    kdj_overbought: bool,
    kdj_oversold: bool,
    rsi_val: Optional[float],
    rsi_overbought: bool,
    rsi_oversold: bool,
    volume_ratio_tech: Optional[float],
    atr_pct: Optional[float],
) -> str:
    """生成一句技术面状态摘要，供 LLM 与报告展示。"""
    parts = []
    if long_align:
        parts.append("日线多头排列")
    else:
        parts.append("非多头排列")
    if macd_golden:
        parts.append("MACD金叉")
    elif macd_above_zero:
        parts.append("MACD零轴上方")
    else:
        parts.append("MACD零轴下方")
    if kdj_overbought:
        parts.append("KDJ超买")
    elif kdj_oversold:
        parts.append("KDJ超卖")
    else:
        parts.append("KDJ中性")
    if rsi_val is not None:
        if rsi_overbought:
            parts.append(f"RSI超买({rsi_val:.0f})")
        elif rsi_oversold:
            parts.append(f"RSI超卖({rsi_val:.0f})")
        else:
            parts.append(f"RSI中性({rsi_val:.0f})")
    if volume_ratio_tech is not None:
        if volume_ratio_tech >= _TECH_VOLUME_BREAKOUT_RATIO:
            parts.append(f"量能放大(量比{volume_ratio_tech:.2f})")
        else:
            parts.append(f"量比{volume_ratio_tech:.2f}")
    if atr_pct is not None:
        parts.append(f"ATR%{atr_pct:.2f}%")
    return "；".join(parts) if parts else "—"


def get_technical_summary(
    ticker: str,
    period: Optional[str] = None,
    interval: str = "1d",
    prepost: bool = False,
) -> dict:
    """
    拉取历史数据并计算技术指标，返回数值摘要（供 LLM 生成「趋势结构 / MACD状态 / KDJ状态」描述）。
    interval: 1d=日K，5m/15m/1m=分K（超短线）。
    prepost: 是否含盘前盘后数据。
    """
    stock = yf.Ticker(ticker)
    interval = (interval or "1d").strip().lower()
    period = period or _INTERVAL_DEFAULT_PERIOD.get(interval, "6mo")
    is_daily = interval == "1d"
    min_bars = _MIN_BARS_DAILY if is_daily else _MIN_BARS_INTRADAY

    try:
        hist = stock.history(period=period, interval=interval, prepost=prepost)
    except Exception:
        hist = None
    if hist is None or len(hist) < min_bars:
        return {
            "ok": False,
            "reason": "历史数据不足",
            "trend_ma": None,
            "macd_summary": None,
            "kdj_summary": None,
            "rsi_summary": None,
            "volume_context": None,
            "tech_levels": {},
            "tech_status_one_line": None,
            "interval": interval,
            "prepost": prepost,
        }
    if not hasattr(hist, "columns") or "Close" not in hist.columns or "High" not in hist.columns or "Low" not in hist.columns:
        return {
            "ok": False,
            "reason": "行情结构异常或暂无价格数据",
            "trend_ma": None,
            "macd_summary": None,
            "kdj_summary": None,
            "rsi_summary": None,
            "volume_context": None,
            "tech_levels": {},
            "tech_status_one_line": None,
            "interval": interval,
            "prepost": prepost,
        }

    close = hist["Close"]
    high = hist["High"]
    low = hist["Low"]
    volume = hist["Volume"] if hasattr(hist, "columns") and "Volume" in hist.columns else None

    # 量能上下文：近期成交量 / 20 日均量
    volume_ratio_tech = None
    volume_ma20 = None
    if volume is not None and len(volume) >= 20:
        volume_ma20 = float(volume.rolling(20).mean().iloc[-1])
        if volume_ma20 and volume_ma20 > 0:
            volume_ratio_tech = float(volume.iloc[-1]) / volume_ma20

    # 均线
    ma5 = close.rolling(5).mean().iloc[-1] if len(close) >= 5 else None
    ma10 = close.rolling(10).mean().iloc[-1] if len(close) >= 10 else None
    ma20 = close.rolling(20).mean().iloc[-1] if len(close) >= 20 else None
    ma60 = close.rolling(60).mean().iloc[-1] if len(close) >= 60 else None
    price = close.iloc[-1]

    # MACD
    macd_line, signal_line, macd_hist = _macd(close)
    macd_val = macd_line.iloc[-1]
    signal_val = signal_line.iloc[-1]
    hist_val = macd_hist.iloc[-1]
    macd_above_zero = macd_val > 0
    macd_golden = macd_line.iloc[-1] > signal_line.iloc[-1] and (macd_line.iloc[-2] <= signal_line.iloc[-2] if len(macd_line) > 1 else False)

    # KDJ
    k, d, j = _kdj(high, low, close)
    k_val = k.iloc[-1]
    d_val = d.iloc[-1]
    j_val = j.iloc[-1]
    kdj_overbought = k_val > 80
    kdj_oversold = k_val < 20

    # RSI(14)
    rsi_series = _rsi(close, 14)
    rsi_val = float(rsi_series.iloc[-1]) if len(rsi_series) >= 14 else None
    rsi_overbought = rsi_val is not None and rsi_val > 70
    rsi_oversold = rsi_val is not None and rsi_val < 30

    # 日线多头排列：价格 > MA5 > MA10 > MA20 > MA60（日 K 维度）
    long_align = False
    if all(x is not None for x in (ma5, ma10, ma20, ma60)):
        long_align = (price > ma5 > ma10 > ma20 > ma60)

    # 趋势：价格与均线关系
    trend_ma = {
        "price": round(price, 2),
        "ma5": round(ma5, 2) if ma5 is not None else None,
        "ma10": round(ma10, 2) if ma10 is not None else None,
        "ma20": round(ma20, 2) if ma20 is not None else None,
        "ma60": round(ma60, 2) if ma60 is not None else None,
        "above_ma5": price > ma5 if ma5 else None,
        "above_ma20": price > ma20 if ma20 else None,
        "above_ma60": price > ma60 if ma60 else None,
        "daily_long_align": long_align,  # 日线多头排列
    }

    macd_summary = {
        "macd": round(macd_val, 4),
        "signal": round(signal_val, 4),
        "histogram": round(hist_val, 4),
        "above_zero": macd_above_zero,
        "golden_cross": macd_golden,
    }

    kdj_summary = {
        "k": round(k_val, 2),
        "d": round(d_val, 2),
        "j": round(j_val, 2),
        "overbought": kdj_overbought,
        "oversold": kdj_oversold,
    }

    rsi_summary = None
    if rsi_val is not None:
        rsi_summary = {
            "rsi": round(rsi_val, 2),
            "overbought": rsi_overbought,
            "oversold": rsi_oversold,
        }

    volume_context = None
    if volume_ratio_tech is not None:
        volume_context = {
            "volume_ratio": round(volume_ratio_tech, 2),
            "volume_ma20": round(volume_ma20, 0) if volume_ma20 is not None else None,
        }

    last_ts = hist.index[-1]
    last_date_str = str(last_ts.date()) if hasattr(last_ts, "date") else str(last_ts)[:19]

    tech_levels = _compute_entry_exit_levels(
        close=close, high=high, low=low,
        ma20=ma20, ma60=ma60, price=price,
        is_daily=is_daily,
        volume_ratio_tech=volume_ratio_tech,
    )

    # 一句技术面状态摘要，供 LLM 直接使用
    tech_status_one_line = _build_tech_status_one_line(
        long_align=long_align,
        macd_above_zero=macd_above_zero,
        macd_golden=macd_golden,
        kdj_overbought=kdj_overbought,
        kdj_oversold=kdj_oversold,
        rsi_val=rsi_val,
        rsi_overbought=rsi_overbought,
        rsi_oversold=rsi_oversold,
        volume_ratio_tech=volume_ratio_tech,
        atr_pct=tech_levels.get("atr_pct"),
    )

    return {
        "ok": True,
        "trend_ma": trend_ma,
        "macd_summary": macd_summary,
        "kdj_summary": kdj_summary,
        "rsi_summary": rsi_summary,
        "volume_context": volume_context,
        "daily_long_align": long_align,
        "last_date": last_date_str,
        "interval": interval,
        "prepost": prepost,
        "tech_levels": tech_levels,
        "tech_status_one_line": tech_status_one_line,
    }
