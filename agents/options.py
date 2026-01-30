"""
近日多空期权：从 yfinance 期权链取 put/call 成交量或持仓，计算多空比供报告。
"""
from typing import Dict, Any

import yfinance as yf


def get_put_call_summary(ticker: str) -> Dict[str, Any]:
    """
    拉取最近到期日的 put/call 成交量（或持仓），计算多空比。
    返回：ratio（put/call）、description（偏多/偏空/中性）、ok。
    """
    try:
        stock = yf.Ticker(ticker)
        expirations = getattr(stock, "options", None)
        if not expirations or len(expirations) == 0:
            return {"ok": False, "ratio": None, "description": "无期权数据", "put_vol": 0, "call_vol": 0}
        expiry = expirations[0]
        chain = stock.option_chain(expiry)
        if chain is None:
            return {"ok": False, "ratio": None, "description": "无期权数据", "put_vol": 0, "call_vol": 0}
        calls = getattr(chain, "calls", None)
        puts = getattr(chain, "puts", None)
        if calls is None or puts is None:
            return {"ok": False, "ratio": None, "description": "无期权数据", "put_vol": 0, "call_vol": 0}
        put_vol = int(puts["volume"].sum()) if hasattr(puts, "columns") and "volume" in puts.columns else 0
        call_vol = int(calls["volume"].sum()) if hasattr(calls, "columns") and "volume" in calls.columns else 0
        if call_vol <= 0:
            ratio = 0.0 if put_vol == 0 else 2.0
        else:
            ratio = put_vol / call_vol
        if ratio < 0.7:
            description = "偏多（call 活跃）"
        elif ratio > 1.3:
            description = "偏空（put 活跃）"
        else:
            description = "中性"
        return {
            "ok": True,
            "ratio": round(ratio, 2),
            "description": description,
            "put_vol": put_vol,
            "call_vol": call_vol,
        }
    except Exception:
        return {"ok": False, "ratio": None, "description": "无期权数据", "put_vol": 0, "call_vol": 0}
