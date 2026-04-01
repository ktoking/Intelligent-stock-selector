"""
美股日终异动扫描：基于日 K（yfinance），用于收盘后批量筛选「冲高放量」等条件。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

from config.yf_suppress import suppress_yf_noise

suppress_yf_noise()
import yfinance as yf


def _download_ohlcv_by_ticker(
    tickers: List[str],
    period: str = "6mo",
    chunk: int = 60,
) -> Dict[str, pd.DataFrame]:
    """批量下载日 K，返回 ticker -> DataFrame(Open,High,Low,Close,Volume)。"""
    out: Dict[str, pd.DataFrame] = {}
    for i in range(0, len(tickers), chunk):
        batch = [t.strip().upper() for t in tickers[i : i + chunk] if (t or "").strip()]
        if not batch:
            continue
        try:
            data = yf.download(
                batch,
                period=period,
                interval="1d",
                auto_adjust=True,
                threads=True,
                progress=False,
                ignore_tz=True,
                group_by="ticker",
            )
        except Exception:
            continue
        if data is None or data.empty:
            continue
        need = ("Close", "High", "Low", "Volume")
        if isinstance(data.columns, pd.MultiIndex):
            for t in batch:
                try:
                    if t not in data.columns.get_level_values(0):
                        continue
                    sub = data[t]
                    if sub is None or sub.empty or not all(c in sub.columns for c in need):
                        continue
                    out[t] = sub[list(need)].copy()
                except Exception:
                    continue
        else:
            if len(batch) == 1 and all(c in data.columns for c in need):
                out[batch[0]] = data[list(need)].copy()
    return out


def eval_us_daily_mover(
    df: pd.DataFrame,
    *,
    min_daily_pct: float = 3.0,
    min_volume_ratio: float = 1.5,
    min_avg_dollar_volume_20d: float = 20_000_000.0,
    require_breakout_20d: bool = True,
    require_above_sma50: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    对单标的日 K 判断是否满足异动规则（评估日为最后一根 K 线）。

    默认 4 条：涨幅、量比(相对前 20 日均量)、20 日平均成交额(美元)、创近 20 日收盘价新高。
    可选第 5 条：收盘价站上截止昨收的 50 日收盘均线（避免未来函数）。
    """
    if df is None or df.empty:
        return None
    need = ("Close", "High", "Low", "Volume")
    if not all(c in df.columns for c in need):
        return None
    d = df[list(need)].copy()
    d["Volume"] = pd.to_numeric(d["Volume"], errors="coerce").fillna(0)
    for c in ("Close", "High", "Low"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.dropna(subset=["Close", "High", "Low"])
    if len(d) < 22:
        return None
    if require_above_sma50 and len(d) < 52:
        return None

    today = d.iloc[-1]
    yest = d.iloc[-2]
    prior = d.iloc[:-1]

    yc, tc = float(yest["Close"]), float(today["Close"])
    if yc <= 0 or tc <= 0:
        return None
    daily_pct = (tc / yc - 1.0) * 100.0

    vol_ma20 = float(prior["Volume"].tail(20).mean())
    tv = float(today["Volume"])
    if vol_ma20 <= 0:
        return None
    vol_ratio = tv / vol_ma20

    dollar_series = (prior["Close"].astype(float) * prior["Volume"].astype(float)).tail(20)
    avg_dollar_20 = float(dollar_series.mean())

    high_20 = float(prior["High"].tail(20).max())
    breakout = tc >= high_20 * 0.9999

    sma50_prior = float(prior["Close"].tail(50).mean()) if len(prior) >= 50 else None
    above50 = sma50_prior is not None and tc >= sma50_prior

    if daily_pct < min_daily_pct:
        return None
    if vol_ratio < min_volume_ratio:
        return None
    if avg_dollar_20 < min_avg_dollar_volume_20d:
        return None
    if require_breakout_20d and not breakout:
        return None
    if require_above_sma50 and not above50:
        return None

    return {
        "daily_pct": round(daily_pct, 2),
        "vol_ratio": round(vol_ratio, 2),
        "avg_dollar_vol_20d": round(avg_dollar_20, 0),
        "breakout_20d": breakout,
        "above_sma50": above50,
        "close": round(tc, 4),
        "volume": tv,
    }


def scan_us_equity_movers(
    tickers: List[str],
    *,
    period: str = "6mo",
    download_chunk: int = 60,
    min_daily_pct: float = 3.0,
    min_volume_ratio: float = 1.5,
    min_avg_dollar_volume_20d: float = 20_000_000.0,
    require_breakout_20d: bool = True,
    require_above_sma50: bool = False,
) -> List[Dict[str, Any]]:
    """对 ticker 列表下载日 K 并筛选，结果按当日涨幅降序。"""
    frames = _download_ohlcv_by_ticker(tickers, period=period, chunk=download_chunk)
    rows: List[Dict[str, Any]] = []
    for t, frame in frames.items():
        m = eval_us_daily_mover(
            frame,
            min_daily_pct=min_daily_pct,
            min_volume_ratio=min_volume_ratio,
            min_avg_dollar_volume_20d=min_avg_dollar_volume_20d,
            require_breakout_20d=require_breakout_20d,
            require_above_sma50=require_above_sma50,
        )
        if m:
            rows.append({"ticker": t, **m})
    rows.sort(key=lambda x: x["daily_pct"], reverse=True)
    return rows
