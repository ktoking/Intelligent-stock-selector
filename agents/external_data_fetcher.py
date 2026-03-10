"""
股票分析工作流 - 外部数据 JSON 模板生成器

将 yfinance 等数据源拉取的数据，转换为「股票分析工作流 - 外部数据JSON模板」格式，
供下游项目直接消费，无需再调用外部 API。

用法:
    from agents.external_data_fetcher import fetch_external_data_json
    data = fetch_external_data_json("AAPL")
    # data 符合 docs 中定义的外部 JSON 模板结构
"""
from config.yf_suppress import suppress_yf_noise
suppress_yf_noise()
from datetime import datetime
from typing import Dict, Any, Optional, List
import yfinance as yf


def _market_type(ticker: str) -> str:
    """根据 ticker 后缀识别市场类型。"""
    t = (ticker or "").upper()
    if ".HK" in t:
        return "hk"
    if ".SZ" in t or ".SS" in t:
        return "cn"
    return "us"


def _to_json_safe(obj):
    """将对象转为 JSON 可序列化形式。"""
    if obj is None:
        return None
    if isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {k: _to_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_json_safe(x) for x in obj]
    try:
        import numpy as np
        if isinstance(obj, (np.integer, np.floating)):
            return float(obj)
        if isinstance(obj, np.bool_):
            return bool(obj)
    except Exception:
        pass
    return str(obj)


def _float_or_none(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int_or_none(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _ts_to_unix(published) -> Optional[int]:
    """将 yfinance 的 published（datetime 或 str）转为 Unix 时间戳。"""
    if published is None:
        return None
    try:
        if hasattr(published, "timestamp"):
            return int(published.timestamp())
        if isinstance(published, (int, float)):
            return int(published)
        s = str(published).strip()
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s[:19], fmt)
                return int(dt.timestamp())
            except ValueError:
                continue
        return None
    except Exception:
        return None


def _safe_get_df(obj, *attrs):
    """安全获取 DataFrame，避免用 or 判断（DataFrame 的 bool 会抛错）。"""
    for a in attrs:
        v = getattr(obj, a, None)
        if v is not None and (not hasattr(v, "empty") or not v.empty):
            return v
    return None


def _extract_financial_row(df, row_names: List[str], col_idx: int = 0) -> Optional[float]:
    """从 financials/cashflow DataFrame 中按行名提取数值。index 为行名，columns 为日期。"""
    if df is None or (hasattr(df, "empty") and df.empty):
        return None
    try:
        import numpy as np
        idx = df.index.astype(str)
        for name in row_names:
            for i, row in enumerate(idx):
                if name.lower() in row.lower():
                    val = df.iloc[i, col_idx]
                    if val is None or (hasattr(np, "isnan") and np.isnan(val)):
                        return None
                    return float(val)
    except Exception:
        pass
    return None


def _build_stock_data(ticker: str, info: dict, current_price: Optional[float]) -> dict:
    """构建 stock_data 区块。"""
    price = current_price or _float_or_none(info.get("currentPrice") or info.get("regularMarketPrice"))
    div_yield = _float_or_none(info.get("dividendYield") or info.get("yield"))
    if div_yield is not None and div_yield > 1:
        div_yield = div_yield / 100.0
    return {
        "symbol": (ticker or "").upper(),
        "company_name": (info.get("longName") or info.get("shortName") or ticker or "").strip(),
        "current_price": price,
        "currency": (info.get("currency") or "USD").strip(),
        "market_cap": _float_or_none(info.get("marketCap")),
        "pe_ratio": _float_or_none(info.get("trailingPE") or info.get("forwardPE")),
        "dividend_yield": div_yield,
        "52_week_high": _float_or_none(info.get("fiftyTwoWeekHigh")),
        "52_week_low": _float_or_none(info.get("fiftyTwoWeekLow")),
        "average_volume": _float_or_none(info.get("averageVolume")),
        "sector": (info.get("sector") or "").strip() or None,
        "industry": (info.get("industry") or "").strip() or None,
        "description": (info.get("longBusinessSummary") or "").strip() or None,
    }


def _build_historical_data(hist, period: str = "6mo") -> dict:
    """将 yfinance history DataFrame 转为 historical_data 格式。"""
    if hist is None or (hasattr(hist, "empty") and hist.empty) or len(hist) == 0:
        return {"period": period, "data": []}
    rows = []
    for ts, row in hist.iterrows():
        date_str = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]
        o = _float_or_none(row.get("Open"))
        h = _float_or_none(row.get("High"))
        l = _float_or_none(row.get("Low"))
        c = _float_or_none(row.get("Close"))
        v = _float_or_none(row.get("Volume"))
        rows.append({
            "date": date_str,
            "open": round(o, 2) if o is not None else None,
            "high": round(h, 2) if h is not None else None,
            "low": round(l, 2) if l is not None else None,
            "close": round(c, 2) if c is not None else None,
            "volume": int(v) if v is not None else None,
        })
    return {"period": period, "data": rows}


def _build_financial_data(stock) -> dict:
    """从 yfinance Ticker 构建 financial_data 区块。"""
    out = {
        "revenue": {"current_year": None, "previous_year": None, "growth_rate": None},
        "net_income": {"current_year": None, "previous_year": None, "growth_rate": None},
        "operating_cash_flow": {"current_year": None, "previous_year": None, "growth_rate": None},
        "free_cash_flow": {"current_year": None, "previous_year": None, "growth_rate": None},
        "profit_margin": None,
        "return_on_equity": None,
        "debt_to_equity": None,
        "quarterly_revenues": [],
    }
    try:
        inc = _safe_get_df(stock, "financials", "income_stmt")
        if inc is not None:
            rev_names = ["Total Revenue", "Operating Revenue", "Revenue", "Total Revenues"]
            ni_names = ["Net Income", "Net Income Common Stockholders", "Net Income Including Noncontrolling Interests"]
            rev_cur = _extract_financial_row(inc, rev_names, 0)
            rev_prev = _extract_financial_row(inc, rev_names, 1) if inc.shape[1] > 1 else None
            ni_cur = _extract_financial_row(inc, ni_names, 0)
            ni_prev = _extract_financial_row(inc, ni_names, 1) if inc.shape[1] > 1 else None
            out["revenue"]["current_year"] = rev_cur
            out["revenue"]["previous_year"] = rev_prev
            out["net_income"]["current_year"] = ni_cur
            out["net_income"]["previous_year"] = ni_prev
            if rev_cur and rev_prev and rev_prev != 0:
                out["revenue"]["growth_rate"] = round((rev_cur - rev_prev) / rev_prev * 100, 2)
            if ni_cur and ni_prev and ni_prev != 0:
                out["net_income"]["growth_rate"] = round((ni_cur - ni_prev) / ni_prev * 100, 2)
            if rev_cur and ni_cur and rev_cur != 0:
                out["profit_margin"] = round(ni_cur / rev_cur * 100, 2)
    except Exception:
        pass
    try:
        cf = _safe_get_df(stock, "cashflow", "cash_flow")
        if cf is not None:
            ocf_names = ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities", "Operating Cash Flow"]
            fcf_names = ["Free Cash Flow", "Free Cash Flow"]
            ocf_cur = _extract_financial_row(cf, ocf_names, 0)
            ocf_prev = _extract_financial_row(cf, ocf_names, 1) if cf.shape[1] > 1 else None
            fcf_cur = _extract_financial_row(cf, fcf_names, 0)
            fcf_prev = _extract_financial_row(cf, fcf_names, 1) if cf.shape[1] > 1 else None
            out["operating_cash_flow"]["current_year"] = ocf_cur
            out["operating_cash_flow"]["previous_year"] = ocf_prev
            out["free_cash_flow"]["current_year"] = fcf_cur
            out["free_cash_flow"]["previous_year"] = fcf_prev
            if ocf_cur and ocf_prev and ocf_prev != 0:
                out["operating_cash_flow"]["growth_rate"] = round((ocf_cur - ocf_prev) / ocf_prev * 100, 2)
            if fcf_cur and fcf_prev and fcf_prev != 0:
                out["free_cash_flow"]["growth_rate"] = round((fcf_cur - fcf_prev) / fcf_prev * 100, 2)
    except Exception:
        pass
    try:
        info = stock.info
        if info is None or not isinstance(info, dict):
            info = {}
        roe = _float_or_none(info.get("returnOnEquity"))
        if roe is not None:
            # yfinance 多为小数(0.5=50%)，转为百分比
            out["return_on_equity"] = round(roe * 100, 2) if roe < 10 else roe
        bs = _safe_get_df(stock, "balance_sheet")
        if bs is not None:
            debt_names = ["Total Debt", "Long Term Debt", "Debt"]
            equity_names = ["Total Stockholder Equity", "Total Equity Gross Minority Interest", "Stockholders Equity"]
            debt = _extract_financial_row(bs, debt_names, 0)
            equity = _extract_financial_row(bs, equity_names, 0)
            if debt is not None and equity is not None and equity != 0:
                out["debt_to_equity"] = round(debt / equity, 2)
    except Exception:
        pass
    try:
        q = _safe_get_df(stock, "quarterly_financials", "quarterly_income_stmt")
        if q is not None:
            rev_names = ["Total Revenue", "Operating Revenue", "Revenue"]
            for c in range(min(4, q.shape[1])):
                rev = _extract_financial_row(q, rev_names, c)
                if rev is not None:
                    col = q.columns[c]
                    q_label = str(col)[:7] if hasattr(col, "strftime") else f"Q{c+1}"
                    try:
                        if hasattr(col, "strftime"):
                            q_label = col.strftime("%Y-%m-%d")[:7]
                    except Exception:
                        pass
                    out["quarterly_revenues"].append({"quarter": q_label, "revenue": rev})
    except Exception:
        pass
    return out


def _build_news_data(news_list: List[dict]) -> dict:
    """将 yfinance news 转为 news_data 格式。兼容新旧结构：旧版 {title,link,publisher,published}；新版 {content:{title,pubDate,provider,canonicalUrl,summary}}。"""
    articles = []
    for n in (news_list or [])[:10]:
        # 新版 yfinance：content 为内层 dict
        inner = n.get("content")
        if isinstance(inner, dict):
            provider = inner.get("provider")
            if isinstance(provider, dict):
                provider = provider.get("displayName") or provider.get("name") or ""
            else:
                provider = str(provider or "")
            link = inner.get("canonicalUrl") or inner.get("clickThroughUrl")
            link = link if isinstance(link, str) else (link.get("url") if isinstance(link, dict) else "")
            articles.append({
                "title": str(inner.get("title") or "").strip(),
                "link": str(link or "").strip(),
                "publisher": str(provider or "").strip(),
                "publish_time": _ts_to_unix(inner.get("pubDate")),
                "summary": str(inner.get("summary") or inner.get("description") or "").strip() or "",
            })
        else:
            # 旧版结构
            articles.append({
                "title": (n.get("title") or "").strip(),
                "link": (n.get("link") or "").strip(),
                "publisher": (n.get("publisher") or "").strip(),
                "publish_time": _ts_to_unix(n.get("published")),
                "summary": (n.get("summary") or "").strip() or "",
            })
    return {"total_count": len(articles), "articles": articles}


def _build_options_data(stock, current_price: Optional[float]) -> dict:
    """从 yfinance option_chain 构建 options_data 格式。"""
    out = {
        "current_price": current_price,
        "options_expiry": None,
        "calls": [],
        "puts": [],
    }
    try:
        expirations = getattr(stock, "options", None)
        if not expirations or len(expirations) == 0:
            return out
        expiry = expirations[0]
        out["options_expiry"] = str(expiry)[:10] if expiry else None
        chain = stock.option_chain(expiry)
        if chain is None:
            return out
        price = current_price or out["current_price"]
        for df, key in [(chain.calls, "calls"), (chain.puts, "puts")]:
            if df is None or df.empty:
                continue
            rows = []
            # 选取当前价附近的 3-5 个行权价
            strikes = df["strike"].values if "strike" in df.columns else []
            if price and len(strikes) > 0:
                dist = [abs(s - price) for s in strikes]
                idx_sorted = sorted(range(len(strikes)), key=lambda i: dist[i])
                take = idx_sorted[:5]
            else:
                take = list(range(min(5, len(df))))
            for i in take:
                if i >= len(df):
                    continue
                row = df.iloc[i]
                strike = _float_or_none(row.get("strike"))
                last_price = _float_or_none(row.get("lastPrice") or row.get("last"))
                vol = _int_or_none(row.get("volume"))
                oi = _int_or_none(row.get("openInterest"))
                iv = _float_or_none(row.get("impliedVolatility"))
                itm = row.get("inTheMoney")
                if isinstance(itm, str):
                    itm = itm.lower() in ("true", "1", "yes")
                rows.append({
                    "strike": strike,
                    "last_price": last_price,
                    "volume": vol or 0,
                    "open_interest": oi or 0,
                    "implied_volatility": iv,
                    "in_the_money": bool(itm) if itm is not None else False,
                })
            out[key] = rows
    except Exception:
        pass
    return out


def fetch_external_data_json(
    ticker: str,
    period: str = "6mo",
    interval: str = "1d",
    max_news: int = 10,
) -> Dict[str, Any]:
    """
    拉取单只标的的完整数据，并转换为「股票分析工作流 - 外部数据JSON模板」格式。

    Args:
        ticker: 股票代码，如 AAPL, 0700.HK, 600519.SS
        period: 历史数据周期，默认 6mo（至少需约 60 个交易日用于技术指标）
        interval: K 线周期，默认 1d（日 K）
        max_news: 新闻条数，默认 10

    Returns:
        符合外部 JSON 模板的 dict，可直接 json.dumps 传给下游。
    """
    ticker = (ticker or "").upper().strip()
    if not ticker:
        return {"stock_code": "", "market_type": "us", "error": "ticker 为空"}

    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        if info is None or not isinstance(info, dict):
            info = {}
    except Exception as e:
        return {"stock_code": ticker, "market_type": _market_type(ticker), "error": f"数据拉取失败: {str(e)[:100]}"}

    # 当前价
    current_price = _float_or_none(info.get("currentPrice") or info.get("regularMarketPrice"))
    if current_price is None:
        try:
            hist_5d = stock.history(period="5d", interval=interval)
            if hist_5d is not None and len(hist_5d) > 0 and "Close" in hist_5d.columns:
                current_price = float(hist_5d["Close"].iloc[-1])
        except Exception:
            pass

    # 历史 K 线（用于技术分析，至少 60 根）
    hist = None
    try:
        hist = stock.history(period=period, interval=interval)
    except Exception:
        pass

    # 新闻
    news_list = []
    try:
        news_list = stock.news or []
    except Exception:
        pass
    news_list = news_list[:max_news]

    try:
        stock_data = _build_stock_data(ticker, info, current_price)
        historical_data = _build_historical_data(hist, period)
        financial_data = _build_financial_data(stock)
        news_data = _build_news_data(news_list)
        options_data = _build_options_data(stock, current_price)
    except Exception as e:
        return {"stock_code": ticker, "market_type": _market_type(ticker), "error": f"数据转换失败: {str(e)[:100]}"}

    result = {
        "stock_code": ticker,
        "market_type": _market_type(ticker),
        "stock_data": _to_json_safe(stock_data),
        "historical_data": _to_json_safe(historical_data),
        "financial_data": _to_json_safe(financial_data),
        "news_data": _to_json_safe(news_data),
        "options_data": _to_json_safe(options_data),
    }
    return result
