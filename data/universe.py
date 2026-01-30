"""
动态股票池：从市值、近期增长等维度拉取美股优质标的（默认 S&P 500 池内取 top N）。
"""
import time
from typing import List, Optional

import pandas as pd
import yfinance as yf

# 内存缓存：避免每次 /report 都拉 Wikipedia + 批量行情（缓存整份排序列表，取前 n 只）
_CACHE: Optional[List[str]] = None
_CACHE_TS: float = 0
_CACHE_TTL_SEC = 86400  # 1 天
# 拉取时一次算出的池大小（按市值+增长排序后缓存）
_POOL_SIZE = 200


def _get_sp500_tickers() -> List[str]:
    """从 Wikipedia 拉取 S&P 500 成分，失败时退回内置列表（多行业覆盖）。"""
    try:
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        tables = pd.read_html(url)
        df = tables[0]
        # 股票代码：Yahoo 用 - 代替 .
        symbols = df["Symbol"].astype(str).str.replace(".", "-", regex=False).tolist()
        return [s for s in symbols if s and len(s) <= 6]
    except Exception:
        pass
    # 退回：多行业常见标的（科技/消费/医药/金融/工业等）
    return [
        "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK-B", "JPM", "V",
        "JNJ", "WMT", "PG", "UNH", "HD", "MA", "DIS", "PYPL", "ADBE", "NFLX",
        "CRM", "INTC", "AMD", "QCOM", "AVGO", "TXN", "ORCL", "CSCO", "IBM", "NOW",
        "ABT", "PEP", "KO", "COST", "MCD", "NKE", "PM", "ABBV", "TMO", "DHR",
        "LLY", "MRK", "PFE", "BMY", "AMGN", "GILD", "VRTX", "REGN", "MRNA",
        "HON", "UPS", "CAT", "DE", "BA", "GE", "MMM", "LMT", "RTX", "NOC",
        "XOM", "CVX", "COP", "SLB", "EOG", "PXD", "MPC", "PSX", "VLO", "OXY",
        "BAC", "WFC", "GS", "MS", "C", "AXP", "BLK", "SCHW", "SPGI", "CME",
        "AMAT", "LRCX", "KLAC", "MU", "ADI", "MRVL", "SNPS", "CDNS", "FTNT",
        "MDT", "SYK", "BSX", "ZBH", "EW", "ISRG", "DXCM", "HUM", "CI", "ELV",
        "LOW", "TGT", "HD", "COST", "SBUX", "NKE", "TJX", "ORLY", "AZO",
        "SPY", "QQQ",
    ]


def _batch_returns(tickers: List[str], period: str = "1mo") -> dict:
    """批量拉取近期涨跌幅（日 K 维度）。"""
    if not tickers:
        return {}
    out = {}
    try:
        data = yf.download(
            tickers, period=period, interval="1d", auto_adjust=True,
            threads=True, progress=False, ignore_tz=True, group_by="ticker"
        )
    except Exception:
        return {}
    if data is None or data.empty:
        return {}
    # 多标的时列为 (Ticker, OHLC)，单标的时列为 Open/High/Low/Close
    if isinstance(data.columns, pd.MultiIndex):
        level0 = data.columns.get_level_values(0)
        for t in tickers:
            try:
                if t not in level0:
                    continue
                sub = data[t] if t in data.columns.get_level_values(0) else None
                if sub is None:
                    continue
                close = sub["Close"] if isinstance(sub, pd.DataFrame) and "Close" in sub.columns else None
                if close is None:
                    continue
                s = close.dropna()
                if len(s) >= 2:
                    out[t] = (float(s.iloc[-1]) - float(s.iloc[0])) / float(s.iloc[0]) * 100
            except Exception:
                continue
    else:
        close = data["Close"] if "Close" in data.columns else data.iloc[:, 0]
        if close is not None and len(close.dropna()) >= 2:
            s = close.dropna()
            out[tickers[0]] = (float(s.iloc[-1]) - float(s.iloc[0])) / float(s.iloc[0]) * 100
    return out


def get_top_by_market_cap_and_growth(
    n: int = 100,
    min_market_cap: Optional[float] = None,
    growth_weight: float = 0.3,
) -> List[str]:
    """
    从 S&P 500 池中按市值与近期增长综合排序，取前 n 只。行业覆盖多领域。
    min_market_cap: 最低市值（美元），可选。
    growth_weight: 近期涨幅权重 0~1，其余为市值权重。
    """
    global _CACHE, _CACHE_TS
    now = time.time()
    if _CACHE is not None and (now - _CACHE_TS) < _CACHE_TTL_SEC and len(_CACHE) >= n:
        return _CACHE[:n]

    all_tickers = _get_sp500_tickers()
    # 限制参与排名的数量以控制首次拉取时间（约 200 只，多行业已覆盖）
    tickers = all_tickers[:250]
    # 批量取 1 个月涨跌幅（日 K）
    returns = _batch_returns(tickers, period="1mo")
    # 逐个取市值（避免单次请求过大）
    rows = []
    for i, t in enumerate(tickers):
        try:
            st = yf.Ticker(t)
            info = st.info or {}
            mcap = info.get("marketCap")
            if mcap is None:
                continue
            mcap = float(mcap)
            if min_market_cap is not None and mcap < min_market_cap:
                continue
            ret = returns.get(t)
            if ret is None:
                ret = 0.0
            rows.append({"ticker": t, "market_cap": mcap, "return_1m": ret})
        except Exception:
            continue
        if (i + 1) % 50 == 0:
            time.sleep(0.2)
    if not rows:
        return tickers[:n]

    df = pd.DataFrame(rows)
    df["mcap_rank"] = df["market_cap"].rank(ascending=False, method="first")
    df["ret_rank"] = df["return_1m"].rank(ascending=False, method="first")
    max_m, max_r = df["mcap_rank"].max(), df["ret_rank"].max()
    df["score"] = (1 - df["mcap_rank"] / (max_m + 1e-10)) * (1 - growth_weight) + (
        1 - df["ret_rank"] / (max_r + 1e-10)
    ) * growth_weight
    df = df.sort_values("score", ascending=False).head(_POOL_SIZE)
    out_full = df["ticker"].astype(str).tolist()
    _CACHE = out_full
    _CACHE_TS = now
    return out_full[:n]
