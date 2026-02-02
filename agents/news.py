"""
消息面：从 yfinance 拉取新闻标题与链接，供 LLM 或报告展示。
"""
import yfinance as yf
from typing import List, Dict, Any

from llm import ask_llm


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


def get_news_summary_llm(ticker: str, news_items: List[Dict[str, Any]], max_titles: int = 8) -> str:
    """
    用 LLM 对近期新闻做 1-2 句话摘要，可带简要情绪（偏多/中性/偏空）。
    无新闻或调用失败时返回空字符串。
    """
    if not news_items:
        return ""
    lines = []
    for n in news_items[:max_titles]:
        title = (n.get("title") or "").strip()
        pub = (n.get("published") or "").strip()
        if title:
            lines.append(f"- {title}" + (f" ({pub})" if pub else ""))
    if not lines:
        return ""
    text = "\n".join(lines)
    try:
        out = ask_llm(
            user=f"""以下为 {ticker} 近期新闻标题（含发布时间）。请用 1-2 句话概括要点，并简要说明对股价影响偏多、中性还是偏空。直接输出摘要，不要标题。

{text}"""
        )
        return (out or "").strip()
    except Exception:
        return ""
