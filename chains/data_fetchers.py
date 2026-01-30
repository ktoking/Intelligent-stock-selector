"""
外部数据接入：将 yfinance / universe 等封装为 LangChain Runnable，供链中调用。
输入一般为 {"ticker": "AAPL"} 或 {"ticker": "AAPL", "peers": "MSFT,GOOGL"}，输出为扩充后的 dict。
"""
from typing import Any, Dict

from langchain_core.runnables import RunnableLambda

# 延迟导入，避免循环依赖
def _fetch_stock_data(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """拉取单标的：财务、info、季度、新闻、同行列表。"""
    from agents.analysis_deep import _get_stock_data, _get_peers_list
    ticker = (inputs.get("ticker") or "").upper().strip()
    if not ticker:
        return {**inputs, "financials": "无", "company_info": "无", "quarterly_summary": "无", "news_summary": "无", "peers": "无"}
    financials_str, company_info, quarterly_str, info = _get_stock_data(ticker)
    peers = inputs.get("peers") or _get_peers_list(ticker, info)
    from agents.news import get_news_summary
    news = get_news_summary(ticker)
    news_summary = "\n".join(
        f"- {n.get('title', '')} ({n.get('published', '')})"
        for n in (news.get("news") or [])[:8]
    )
    return {
        **inputs,
        "ticker": ticker,
        "financials": financials_str,
        "company_info": company_info,
        "quarterly_summary": quarterly_str,
        "news_summary": news_summary,
        "peers": peers,
    }


def _fetch_for_report(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """单标的报告用：技术 + 消息 + 财报 + 期权，供 full_analysis 链。"""
    from agents.technical import get_technical_summary
    from agents.news import get_news_summary
    from agents.fundamental import get_fundamental_data
    from agents.options import get_put_call_summary
    ticker = (inputs.get("ticker") or "").upper().strip()
    if not ticker:
        return inputs
    technical = get_technical_summary(ticker)
    news = get_news_summary(ticker)
    fundamental = get_fundamental_data(ticker)
    options_summary = get_put_call_summary(ticker)
    return {
        **inputs,
        "ticker": ticker,
        "technical": technical,
        "news": news,
        "fundamental": fundamental,
        "options_summary": options_summary,
    }


# 供链使用的 Runnable
fetch_stock_data = RunnableLambda(_fetch_stock_data)
fetch_for_report = RunnableLambda(_fetch_for_report)
