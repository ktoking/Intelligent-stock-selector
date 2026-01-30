"""
消息面：从 yfinance 拉取新闻标题与链接，供 LLM 或报告展示。
"""
import yfinance as yf
from typing import List, Dict, Any


def get_news_summary(ticker: str, max_items: int = 10) -> dict:
    """
    拉取该标的近期新闻，返回标题、链接、发布时间等摘要。
    """
    stock = yf.Ticker(ticker)
    try:
        news = stock.news or []
    except Exception:
        news = []
    items = []
    for n in news[:max_items]:
        items.append({
            "title": n.get("title") or "",
            "link": n.get("link") or "",
            "publisher": n.get("publisher") or "",
            "published": str(n.get("published", ""))[:19] if n.get("published") else "",
        })
    return {"ok": True, "ticker": ticker, "news": items}
