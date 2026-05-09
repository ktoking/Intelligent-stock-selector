"""
消息面：从 yfinance 拉取新闻标题与链接，供 LLM 或报告展示。
"""
from config.yf_suppress import suppress_yf_noise
suppress_yf_noise()
import re
import yfinance as yf
from typing import List, Dict, Any, Tuple

from llm import ask_llm


_COMPANY_SUFFIX_WORDS = {
    "a",
    "adr",
    "and",
    "class",
    "co",
    "common",
    "companies",
    "company",
    "corp",
    "corporation",
    "inc",
    "incorporated",
    "limited",
    "ltd",
    "nv",
    "plc",
    "sa",
    "se",
    "stock",
    "the",
}


def _base_ticker(ticker: str) -> str:
    """去掉交易所后缀，只保留新闻里常见的主 ticker。"""
    return (ticker or "").upper().split(".")[0].strip()


def _news_url(link: Any) -> str:
    if isinstance(link, dict):
        return str(link.get("url") or "").strip()
    return str(link or "").strip()


def normalize_news_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    兼容 yfinance 新旧新闻结构，统一输出 title/link/publisher/published/summary。
    """
    item = item or {}
    inner = item.get("content")
    if isinstance(inner, dict):
        provider = inner.get("provider")
        if isinstance(provider, dict):
            publisher = provider.get("displayName") or provider.get("name") or ""
        else:
            publisher = provider or ""
        link = inner.get("canonicalUrl") or inner.get("clickThroughUrl")
        published = inner.get("pubDate") or inner.get("displayTime") or ""
        return {
            "title": str(inner.get("title") or "").strip(),
            "link": _news_url(link),
            "publisher": str(publisher or "").strip(),
            "published": str(published or "")[:19],
            "summary": str(inner.get("summary") or inner.get("description") or "").strip(),
        }
    return {
        "title": str(item.get("title") or "").strip(),
        "link": str(item.get("link") or "").strip(),
        "publisher": str(item.get("publisher") or "").strip(),
        "published": str(item.get("published") or "")[:19] if item.get("published") else "",
        "summary": str(item.get("summary") or "").strip(),
    }


def _company_terms(company_name: str) -> List[str]:
    raw = re.sub(r"[^A-Za-z0-9]+", " ", company_name or "").strip().lower()
    if not raw:
        return []
    words = [w for w in raw.split() if w and w not in _COMPANY_SUFFIX_WORDS]
    terms = []
    if words:
        joined = " ".join(words)
        if len(joined) >= 4:
            terms.append(joined)
        for w in words[:3]:
            if len(w) >= 4:
                terms.append(w)
    # 去重并保序
    out = []
    for t in terms:
        if t not in out:
            out.append(t)
    return out


def _matches_word(text_upper: str, term_upper: str) -> bool:
    if not term_upper:
        return False
    return re.search(rf"(?<![A-Z0-9]){re.escape(term_upper)}(?![A-Z0-9])", text_upper) is not None


def _news_relevance_reason(ticker: str, company_name: str, item: Dict[str, Any]) -> str:
    """
    只认定“个股直相关”新闻，避免把泛 AI/行业/大盘新闻送进综合判断。
    """
    base = _base_ticker(ticker)
    text = f"{item.get('title') or ''} {item.get('summary') or ''}".strip()
    if not text:
        return ""
    text_upper = text.upper()
    text_lower = text.lower()
    if base and _matches_word(text_upper, base):
        return "ticker"
    for term in _company_terms(company_name):
        if term in text_lower:
            return "company"
    return ""


def filter_relevant_news(
    ticker: str,
    news_items: List[Dict[str, Any]],
    company_name: str = "",
    max_items: int = 10,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    返回 (个股直相关新闻, 被过滤新闻)。被过滤新闻不进入 LLM，避免污染消息面判断。
    """
    relevant = []
    excluded = []
    for raw in news_items or []:
        item = normalize_news_item(raw)
        if not item.get("title"):
            continue
        reason = _news_relevance_reason(ticker, company_name, item)
        if reason:
            item["relevance"] = "direct"
            item["relevance_reason"] = reason
            relevant.append(item)
        else:
            item["relevance"] = "excluded"
            item["relevance_reason"] = "no_ticker_or_company_match"
            excluded.append(item)
    return relevant[:max_items], excluded


def get_news_summary(ticker: str, max_items: int = 10) -> dict:
    """
    拉取该标的近期新闻，返回标题、链接、发布时间等摘要。
    """
    stock = yf.Ticker(ticker)
    try:
        news = stock.news or []
    except Exception:
        news = []
    company_name = ""
    try:
        info = stock.info or {}
        company_name = info.get("shortName") or info.get("longName") or ""
    except Exception:
        company_name = ""
    items, excluded = filter_relevant_news(ticker, news, company_name=company_name, max_items=max_items)
    return {
        "ok": True,
        "ticker": ticker,
        "news": items,
        "original_count": len(news or []),
        "excluded_news_count": len(excluded),
        "news_relevance_note": "仅保留标题或摘要命中 ticker/公司名的个股直相关新闻",
    }



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
