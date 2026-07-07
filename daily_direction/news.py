from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping

from config.yf_suppress import suppress_yf_noise

suppress_yf_noise()
import yfinance as yf


RISK_KEYWORDS = {
    "regulation": ["investigation", "probe", "lawsuit", "sec", "doj", "监管", "调查", "诉讼", "立案"],
    "policy": ["tariff", "sanction", "export control", "rate decision", "fed", "制裁", "关税", "出口管制", "降息", "加息"],
    "earnings": ["guidance cut", "miss", "profit warning", "业绩预警", "业绩预亏", "下修指引"],
    "credit": ["default", "bankruptcy", "liquidity", "违约", "破产", "流动性"],
    "geopolitical": ["war", "attack", "conflict", "地缘", "战争", "冲突"],
}


def _news_url(link: Any) -> str:
    if isinstance(link, dict):
        return str(link.get("url") or "").strip()
    return str(link or "").strip()


def normalize_yf_news_item(ticker: str, item: Mapping[str, Any]) -> Dict[str, str]:
    inner = item.get("content") if isinstance(item, Mapping) else None
    if isinstance(inner, Mapping):
        provider = inner.get("provider")
        publisher = ""
        if isinstance(provider, Mapping):
            publisher = str(provider.get("displayName") or provider.get("name") or "")
        elif provider:
            publisher = str(provider)
        link = inner.get("canonicalUrl") or inner.get("clickThroughUrl")
        return {
            "ticker": ticker,
            "title": str(inner.get("title") or "").strip(),
            "summary": str(inner.get("summary") or inner.get("description") or "").strip(),
            "publisher": publisher.strip(),
            "published": str(inner.get("pubDate") or inner.get("displayTime") or "")[:19],
            "url": _news_url(link),
        }
    return {
        "ticker": ticker,
        "title": str(item.get("title") or "").strip(),
        "summary": str(item.get("summary") or "").strip(),
        "publisher": str(item.get("publisher") or "").strip(),
        "published": str(item.get("published") or "")[:19] if item.get("published") else "",
        "url": str(item.get("link") or "").strip(),
    }


def fetch_ticker_news(
    tickers: Iterable[str],
    *,
    max_items_per_ticker: int = 2,
) -> Dict[str, List[Dict[str, str]]]:
    out: Dict[str, List[Dict[str, str]]] = {}
    for ticker in tickers:
        symbol = str(ticker or "").strip().upper()
        if not symbol:
            continue
        try:
            raw_items = yf.Ticker(symbol).news or []
        except Exception:
            raw_items = []
        items = []
        for raw in raw_items:
            if not isinstance(raw, Mapping):
                continue
            item = normalize_yf_news_item(symbol, raw)
            if item.get("title"):
                items.append(item)
            if len(items) >= max_items_per_ticker:
                break
        if items:
            out[symbol] = items
    return out


def _risk_type(title: str, summary: str = "") -> str:
    text = f"{title} {summary}".lower()
    for kind, keywords in RISK_KEYWORDS.items():
        if any(keyword.lower() in text for keyword in keywords):
            return kind
    return ""


def build_news_context(
    *,
    market_news: List[Dict[str, str]] | None = None,
    ticker_news: Dict[str, List[Dict[str, str]]] | None = None,
) -> Dict[str, Any]:
    market_items = list(market_news or [])
    ticker_items = dict(ticker_news or {})
    risks: List[Dict[str, str]] = []
    for item in market_items:
        kind = _risk_type(item.get("title", ""), item.get("summary", ""))
        if kind:
            risks.append({**item, "risk_type": kind})
    for items in ticker_items.values():
        for item in items:
            kind = _risk_type(item.get("title", ""), item.get("summary", ""))
            if kind:
                risks.append({**item, "risk_type": kind})
    return {
        "market_news": market_items[:10],
        "ticker_news": ticker_items,
        "event_risks": risks[:10],
    }


def _unique_tickers_from_snapshots(snapshots: Mapping[str, Mapping[str, Any]], max_tickers: int) -> List[str]:
    tickers: List[str] = []
    for snap in snapshots.values():
        rows = list(snap.get("top_signals") or [])[:5] + list(snap.get("top_losers") or [])[:5]
        for row in rows:
            ticker = str(row.get("ticker") or "").strip().upper()
            if ticker and ticker not in tickers:
                tickers.append(ticker)
            if len(tickers) >= max_tickers:
                return tickers
    return tickers


def collect_direction_news(
    snapshots: Mapping[str, Mapping[str, Any]],
    *,
    max_tickers: int = 18,
    max_items_per_ticker: int = 2,
) -> Dict[str, Any]:
    macro_tickers = ["SPY", "QQQ", "ASHR", "FXI", "2800.HK"]
    tickers = _unique_tickers_from_snapshots(snapshots, max_tickers=max_tickers)
    ticker_news = fetch_ticker_news(tickers, max_items_per_ticker=max_items_per_ticker)
    macro_news_map = fetch_ticker_news(macro_tickers, max_items_per_ticker=1)
    market_news = [item for items in macro_news_map.values() for item in items]
    return build_news_context(market_news=market_news, ticker_news=ticker_news)
