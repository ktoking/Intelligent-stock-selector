"""
Alpha Vantage HTTP 备用数据源。

当 yfinance 无法获取有效价格时，自动尝试 Alpha Vantage GLOBAL_QUOTE API。
需在 .env.local 中设置：ALPHA_VANTAGE_API_KEY=your_key

免费版限制：25 次/天，250 次/月。本模块仅在 yfinance 返回 None/NaN 时触发，
配合 yf_cache.py 缓存可将网络请求降到最低。

Alpha Vantage 仅支持美股（US 市场），港股/A股 ticker 会静默跳过。
"""
import math
import os
from typing import Optional, Tuple

import requests

_AV_BASE = "https://www.alphavantage.co/query"
_US_ONLY_SUFFIX = {".HK", ".SS", ".SZ"}


def _is_us_ticker(ticker: str) -> bool:
    t = ticker.upper()
    return not any(t.endswith(s) for s in _US_ONLY_SUFFIX)


def _get_api_key() -> Optional[str]:
    key = os.environ.get("ALPHA_VANTAGE_API_KEY", "").strip()
    return key if key else None


def _safe_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None


def get_quote(ticker: str) -> Tuple[Optional[float], Optional[float]]:
    """
    通过 Alpha Vantage GLOBAL_QUOTE API 获取当前价格和涨跌幅。
    返回 (current_price, change_pct)，无法获取时返回 (None, None)。
    仅在 ALPHA_VANTAGE_API_KEY 存在且标的为美股时生效。
    """
    api_key = _get_api_key()
    if not api_key or not _is_us_ticker(ticker):
        return None, None

    try:
        resp = requests.get(
            _AV_BASE,
            params={
                "function": "GLOBAL_QUOTE",
                "symbol": ticker.upper(),
                "apikey": api_key,
            },
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        quote = data.get("Global Quote") or {}
        price = _safe_float(quote.get("05. price"))
        change_str = quote.get("10. change percent", "")
        change_pct = _safe_float(change_str.replace("%", "").strip() if change_str else None)
        if price is not None:
            print(f"[AV Fallback] {ticker} price={price}, change_pct={change_pct}", flush=True)
        return price, change_pct
    except Exception as e:
        print(f"[AV Fallback] {ticker} 请求失败: {e}", flush=True)
        return None, None
