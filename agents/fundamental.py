import yfinance as yf
from llm import ask_llm
from typing import Optional, Dict, Any


def get_fundamental_data(ticker: str) -> Dict[str, Any]:
    """
    拉取财报与行情相关原始数据，供报告卡片和 LLM 综合研判使用。
    """
    stock = yf.Ticker(ticker)
    info = stock.info or {}
    hist = None
    try:
        financials = stock.financials
        financials_str = financials.to_string() if financials is not None and not financials.empty else "无"
    except Exception:
        financials_str = "无"
    try:
        hist = stock.history(period="5d")
        if hist is not None and len(hist) >= 2 and hasattr(hist, "columns") and "Close" in hist.columns:
            current = float(hist["Close"].iloc[-1])
            prev = float(hist["Close"].iloc[-2])
            change_pct = (current - prev) / prev * 100 if prev else 0
        else:
            current = info.get("currentPrice") or info.get("regularMarketPrice")
            current = float(current) if current is not None else None
            change_pct = info.get("regularMarketChangePercent")
            change_pct = float(change_pct) if change_pct is not None else None
    except Exception:
        current = info.get("currentPrice") or info.get("regularMarketPrice")
        current = float(current) if current is not None else None
        change_pct = None

    # 52周高低
    week52_high = info.get("fiftyTwoWeekHigh")
    week52_low = info.get("fiftyTwoWeekLow")
    if week52_high is not None:
        try:
            week52_high = float(week52_high)
        except (TypeError, ValueError):
            week52_high = None
    if week52_low is not None:
        try:
            week52_low = float(week52_low)
        except (TypeError, ValueError):
            week52_low = None

    # 量比：近期成交量 / 平均成交量（近5日日均 vs info 平均）
    volume_ratio = None
    try:
        avg_vol = info.get("averageVolume")
        if hist is not None and hasattr(hist, "columns") and "Volume" in hist.columns and len(hist) > 0 and avg_vol and float(avg_vol) > 0:
            recent_vol = float(hist["Volume"].iloc[-1])
            volume_ratio = recent_vol / float(avg_vol)
    except Exception:
        pass

    # 股息率（yfinance 多为小数，如 0.02 表示 2%）
    dividend_yield = info.get("dividendYield") or info.get("yield")
    if dividend_yield is not None:
        try:
            dividend_yield = float(dividend_yield)
            if dividend_yield > 1:
                dividend_yield = dividend_yield / 100.0
        except (TypeError, ValueError):
            dividend_yield = None
    else:
        dividend_yield = None

    # 机构/分析师倾向
    recommendation = info.get("recommendationKey") or info.get("recommendationMean")
    if recommendation is not None:
        recommendation = str(recommendation).strip()
        rec_map = {"strong_buy": "强烈买入", "buy": "买入", "hold": "持有", "sell": "卖出", "strong_sell": "强烈卖出"}
        recommendation = rec_map.get(recommendation.lower(), recommendation)
    else:
        recommendation = None

    # 下次财报日（calendar 中 Earnings Date 为日期列表；若无则用 get_earnings_dates）
    next_earnings = None
    try:
        from datetime import date
        cal = getattr(stock, "calendar", None) or getattr(stock, "get_calendar", lambda: None)()
        if isinstance(cal, dict) and cal.get("Earnings Date"):
            ed_list = cal["Earnings Date"]
            if isinstance(ed_list, list) and ed_list:
                today = date.today()
                for d in ed_list:
                    if hasattr(d, "strftime"):
                        d_date = d if hasattr(d, "year") else getattr(d, "date", lambda: d)()
                        if d_date >= today:
                            next_earnings = d_date.strftime("%Y-%m-%d") if hasattr(d_date, "strftime") else str(d_date)[:10]
                            break
            elif hasattr(ed_list, "strftime"):
                next_earnings = ed_list.strftime("%Y-%m-%d")
            else:
                next_earnings = str(ed_list)[:10]
        if not next_earnings:
            edf = stock.get_earnings_dates(limit=4)
            if edf is not None and not edf.empty and len(edf.index) > 0:
                for idx in list(edf.index)[:4]:
                    try:
                        dt = idx.to_pydatetime().date() if hasattr(idx, "to_pydatetime") else (idx.date() if hasattr(idx, "date") else idx)
                        if hasattr(dt, "strftime"):
                            next_earnings = dt.strftime("%Y-%m-%d")
                        else:
                            next_earnings = str(dt)[:10]
                        break
                    except Exception:
                        continue
    except Exception:
        pass

    def _fmt_mcap(v):
        if v is None:
            return "—"
        try:
            v = float(v)
            if v >= 1e12:
                return f"{v/1e12:.2f}万亿"
            if v >= 1e8:
                return f"{v/1e8:.2f}亿"
            if v >= 1e4:
                return f"{v/1e4:.2f}万"
            return str(v)
        except Exception:
            return "—"

    trailing_pe = info.get("trailingPE")
    forward_pe = info.get("forwardPE")
    pe_str = "—"
    if trailing_pe is not None:
        try:
            pe_str = f"{float(trailing_pe):.2f}"
        except Exception:
            pass
    elif forward_pe is not None:
        try:
            pe_str = f"{float(forward_pe):.2f}(F)"
        except Exception:
            pass

    return {
        "ticker": ticker.upper(),
        "short_name": info.get("shortName") or ticker.upper(),
        "sector": info.get("sector") or "—",
        "industry": info.get("industry") or "—",
        "market_cap": _fmt_mcap(info.get("marketCap")),
        "market_cap_raw": info.get("marketCap"),
        "current_price": round(current, 2) if current is not None else None,
        "change_pct": round(change_pct, 2) if change_pct is not None else None,
        "trailing_pe": float(trailing_pe) if trailing_pe is not None else None,
        "forward_pe": float(forward_pe) if forward_pe is not None else None,
        "pe": pe_str,
        "financials_str": financials_str,
        "gross_margins": info.get("grossMargins"),
        "earnings_growth": info.get("earningsGrowth"),
        "week52_high": week52_high,
        "week52_low": week52_low,
        "volume_ratio": round(volume_ratio, 2) if volume_ratio is not None else None,
        "dividend_yield": dividend_yield,
        "recommendation": recommendation,
        "next_earnings": next_earnings,
    }


def analyze_fundamental(ticker: str):
    """原有接口：仅基本面 LLM 分析文案。"""
    stock = yf.Ticker(ticker)
    financials = stock.financials
    info = stock.info
    prompt = f"""
请你作为一名美股基本面分析师，分析以下公司：

公司：{ticker}

【关键财务数据】
{financials.to_string()}

【公司信息】
市值: {info.get("marketCap")}
行业: {info.get("industry")}
毛利率: {info.get("grossMargins")}

请输出：
1. 收入与利润趋势
2. 商业模式是否健康
3. 中长期风险（不超过5条）
"""
    return ask_llm(
        system="你是长期价值投资取向的美股分析师，避免情绪化和炒作。",
        user=prompt
    )
