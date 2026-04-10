"""
技术面：从 yfinance 历史数据计算 MA、MACD、KDJ、RSI、布林带、OBV、量能、ATR%，并给出数值摘要供 LLM 解读。
支持 MACD/RSI 背离自动检测。支持日K（1d）与分K（1m/5m/15m），可选盘前盘后（prepost）。
入场/离场规则可配置：ATR 止损倍数、放量突破阈值（见 config/analysis_config）。
指标计算使用 ta 库（Python>=3.10，pip install ta）。
"""
from config.yf_suppress import suppress_yf_noise
suppress_yf_noise()
import pandas as pd
import yfinance as yf
from typing import Optional, Tuple, List
from ta.trend import MACD as _TaMacd
from utils.yf_cache import get_history as _yf_get_history
from ta.momentum import RSIIndicator as _TaRsi, StochasticOscillator as _TaStoch
from ta.volatility import BollingerBands as _TaBb
from ta.volume import OnBalanceVolumeIndicator as _TaObv

from config.analysis_config import (
    ATR_STOP_MULT,
    VOLUME_BREAKOUT_RATIO,
    BB_PERIOD,
    BB_STD_MULT,
    VOLUME_MA_PERIOD,
    DIVERGENCE_LOOKBACK,
    DIVERGENCE_MIN_BARS,
)

# 分K 默认拉取周期（yfinance 限制：1m 最多约 7d，5m/15m 最多约 60d）
_INTERVAL_DEFAULT_PERIOD = {"1d": "6mo", "1m": "5d", "5m": "5d", "15m": "5d", "30m": "5d", "60m": "5d"}
# 分K 最少需要根数（MA60 需 60 根，分K 可放宽到 30）
_MIN_BARS_DAILY = 60
_MIN_BARS_INTRADAY = 30


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """ATR(14)，用于止损参考。"""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def _find_swing_highs(series: pd.Series, window: int = 3) -> List[int]:
    """返回近期 swing high 的索引列表（从旧到新）。"""
    idxs = []
    for i in range(window, len(series) - window):
        if series.iloc[i] == series.iloc[i - window : i + window + 1].max():
            idxs.append(i)
    return idxs


def _find_swing_lows(series: pd.Series, window: int = 3) -> List[int]:
    """返回近期 swing low 的索引列表（从旧到新）。"""
    idxs = []
    for i in range(window, len(series) - window):
        if series.iloc[i] == series.iloc[i - window : i + window + 1].min():
            idxs.append(i)
    return idxs


def _detect_divergence(
    close: pd.Series,
    macd_line: pd.Series,
    rsi_series: pd.Series,
    lookback: int = 30,
    min_bars: int = 20,
) -> dict:
    """
    检测 MACD/RSI 顶背离与底背离。
    顶背离：价格创新高，指标未创新高。
    底背离：价格创新低，指标未创新低。
    返回 {"macd_top", "macd_bottom", "rsi_top", "rsi_bottom"} 布尔值。
    """
    out = {"macd_top": False, "macd_bottom": False, "rsi_top": False, "rsi_bottom": False}
    n = len(close)
    if n < min_bars or lookback < 5:
        return out
    use = min(lookback, n - 1)
    window = 3

    # 取近期数据
    c = close.iloc[-use:]
    m = macd_line.iloc[-use:]
    r = rsi_series.iloc[-use:]

    # Swing highs/lows
    sh = _find_swing_highs(c, window)
    sl = _find_swing_lows(c, window)

    # 顶背离：取最近两个 swing high，价格更高但指标更低
    if len(sh) >= 2:
        i1, i2 = sh[-2], sh[-1]
        if c.iloc[i2] > c.iloc[i1] and m.iloc[i2] < m.iloc[i1]:
            out["macd_top"] = True
        if c.iloc[i2] > c.iloc[i1] and r.iloc[i2] < r.iloc[i1]:
            out["rsi_top"] = True

    # 底背离：取最近两个 swing low，价格更低但指标更高
    if len(sl) >= 2:
        i1, i2 = sl[-2], sl[-1]
        if c.iloc[i2] < c.iloc[i1] and m.iloc[i2] > m.iloc[i1]:
            out["macd_bottom"] = True
        if c.iloc[i2] < c.iloc[i1] and r.iloc[i2] > r.iloc[i1]:
            out["rsi_bottom"] = True

    return out


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

    mult = ATR_STOP_MULT
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
        and volume_ratio_tech >= VOLUME_BREAKOUT_RATIO
        and resistance_20d is not None
        and resistance_20d > price
    ):
        entry_parts.append(f"若放量（量比≥{VOLUME_BREAKOUT_RATIO}）突破近期高点更佳")
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
    bb_summary: Optional[dict] = None,
    obv_summary: Optional[dict] = None,
    divergence_summary: Optional[dict] = None,
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
    if bb_summary:
        if bb_summary.get("above_upper"):
            parts.append("布林带上轨上方")
        elif bb_summary.get("below_lower"):
            parts.append("布林带下轨下方")
        else:
            bp = bb_summary.get("bollinger_pct")
            if bp is not None:
                parts.append(f"布林带{bp:.0f}%")
    if obv_summary and obv_summary.get("obv_above_ma") is True:
        parts.append("OBV上穿均量")
    elif obv_summary and obv_summary.get("obv_above_ma") is False:
        parts.append("OBV下穿均量")
    if divergence_summary:
        if divergence_summary.get("macd_top") or divergence_summary.get("rsi_top"):
            parts.append("顶背离")
        if divergence_summary.get("macd_bottom") or divergence_summary.get("rsi_bottom"):
            parts.append("底背离")
    if volume_ratio_tech is not None:
        if volume_ratio_tech >= VOLUME_BREAKOUT_RATIO:
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
    interval = (interval or "1d").strip().lower()
    period = period or _INTERVAL_DEFAULT_PERIOD.get(interval, "6mo")
    is_daily = interval == "1d"
    min_bars = _MIN_BARS_DAILY if is_daily else _MIN_BARS_INTRADAY

    hist = _yf_get_history(ticker, period=period, interval=interval, prepost=prepost)
    if hist is None or len(hist) < min_bars:
        return {
            "ok": False,
            "reason": "历史数据不足",
            "trend_ma": None,
            "macd_summary": None,
            "kdj_summary": None,
            "rsi_summary": None,
            "bb_summary": None,
            "obv_summary": None,
            "divergence_summary": None,
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
            "bb_summary": None,
            "obv_summary": None,
            "divergence_summary": None,
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

    # 量能上下文：近期成交量 / N 日均量
    volume_ratio_tech = None
    volume_ma20 = None
    if volume is not None and len(volume) >= VOLUME_MA_PERIOD:
        volume_ma20 = float(volume.rolling(VOLUME_MA_PERIOD).mean().iloc[-1])
        if volume_ma20 and volume_ma20 > 0:
            volume_ratio_tech = float(volume.iloc[-1]) / volume_ma20

    # 均线
    ma5 = close.rolling(5).mean().iloc[-1] if len(close) >= 5 else None
    ma10 = close.rolling(10).mean().iloc[-1] if len(close) >= 10 else None
    ma20 = close.rolling(20).mean().iloc[-1] if len(close) >= 20 else None
    ma60 = close.rolling(60).mean().iloc[-1] if len(close) >= 60 else None
    price = close.iloc[-1]

    # MACD（ta 库：fast=12, slow=26, signal=9）
    _macd_ind = _TaMacd(close=close)
    macd_line = _macd_ind.macd().fillna(0)
    signal_line = _macd_ind.macd_signal().fillna(0)
    macd_hist_s = _macd_ind.macd_diff().fillna(0)
    macd_val = float(macd_line.iloc[-1])
    signal_val = float(signal_line.iloc[-1])
    hist_val = float(macd_hist_s.iloc[-1])
    macd_above_zero = macd_val > 0
    macd_golden = (macd_val > signal_val) and (
        len(macd_line) > 1 and float(macd_line.iloc[-2]) <= float(signal_line.iloc[-2])
    )

    # KDJ：ta StochasticOscillator → K/D，J=3K-2D
    _stoch_ind = _TaStoch(high=high, low=low, close=close)
    k = _stoch_ind.stoch().fillna(50)
    d = _stoch_ind.stoch_signal().fillna(50)
    j = 3 * k - 2 * d
    k_val = float(k.iloc[-1])
    d_val = float(d.iloc[-1])
    j_val = float(j.iloc[-1])
    kdj_overbought = k_val > 80
    kdj_oversold = k_val < 20

    # RSI(14)（ta 库 Wilder 平滑，与原公式一致）
    rsi_series = _TaRsi(close=close).rsi()
    rsi_val = float(rsi_series.iloc[-1]) if len(rsi_series) >= 14 and pd.notna(rsi_series.iloc[-1]) else None
    rsi_overbought = rsi_val is not None and rsi_val > 70
    rsi_oversold = rsi_val is not None and rsi_val < 30

    # 布林带（ta 库：window=BB_PERIOD, window_dev=BB_STD_MULT）
    _bb_ind = _TaBb(close=close, window=BB_PERIOD, window_dev=BB_STD_MULT)
    bb_summary = None
    if len(close) >= BB_PERIOD:
        u = float(_bb_ind.bollinger_hband().iloc[-1])
        m = float(_bb_ind.bollinger_mavg().iloc[-1])
        l = float(_bb_ind.bollinger_lband().iloc[-1])
        bb_summary = {
            "upper": round(u, 2),
            "middle": round(m, 2),
            "lower": round(l, 2),
            "above_upper": price > u,
            "below_lower": price < l,
            "bollinger_pct": round((price - l) / (u - l) * 100, 1) if (u - l) > 0 else None,
        }

    # OBV 能量潮（ta 库）
    obv_summary = None
    if volume is not None and len(volume) >= 5:
        obv_series = _TaObv(close=close, volume=volume.astype(float)).on_balance_volume()
        obv_now = float(obv_series.iloc[-1])
        obv_ma = float(obv_series.rolling(VOLUME_MA_PERIOD).mean().iloc[-1]) if len(obv_series) >= VOLUME_MA_PERIOD else None
        obv_summary = {
            "obv": round(obv_now, 0),
            "obv_ma": round(obv_ma, 0) if obv_ma is not None else None,
            "obv_above_ma": obv_now > obv_ma if obv_ma is not None else None,
        }

    # MACD/RSI 背离检测
    divergence_summary = _detect_divergence(
        close, macd_line, rsi_series,
        lookback=DIVERGENCE_LOOKBACK,
        min_bars=DIVERGENCE_MIN_BARS,
    )

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
        bb_summary=bb_summary,
        obv_summary=obv_summary,
        divergence_summary=divergence_summary,
    )

    # 动量摘要：N 根 K 收益率、相对窗口内最高价的距离（供定量基准与 Prompt）
    momentum_summary = None
    try:
        n = len(close)
        ret_20d_pct = None
        if n >= 21:
            c0 = float(close.iloc[-1])
            c20 = float(close.iloc[-21])
            if c20 and c20 > 0:
                ret_20d_pct = round((c0 / c20 - 1) * 100, 2)
        ret_60d_pct = None
        if n >= 61:
            c0 = float(close.iloc[-1])
            c60 = float(close.iloc[-61])
            if c60 and c60 > 0:
                ret_60d_pct = round((c0 / c60 - 1) * 100, 2)
        win = min(252, n) if is_daily else min(120, n)
        dist_to_52w_high_pct = None
        if win >= 20:
            hh = float(high.iloc[-win:].max())
            if hh > 0:
                dist_to_52w_high_pct = round((float(price) / hh - 1) * 100, 2)
        momentum_summary = {
            "return_20d_pct": ret_20d_pct,
            "return_60d_pct": ret_60d_pct,
            "dist_to_52w_high_pct": dist_to_52w_high_pct,
            "momentum_window_bars": win,
        }
    except Exception:
        momentum_summary = None

    return {
        "ok": True,
        "trend_ma": trend_ma,
        "macd_summary": macd_summary,
        "kdj_summary": kdj_summary,
        "rsi_summary": rsi_summary,
        "bb_summary": bb_summary,
        "obv_summary": obv_summary,
        "divergence_summary": divergence_summary,
        "volume_context": volume_context,
        "daily_long_align": long_align,
        "last_date": last_date_str,
        "interval": interval,
        "prepost": prepost,
        "tech_levels": tech_levels,
        "tech_status_one_line": tech_status_one_line,
        "momentum_summary": momentum_summary,
    }
