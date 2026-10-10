from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping
from concurrent.futures import ThreadPoolExecutor
import html
import re
import requests

from config.yf_suppress import suppress_yf_noise

suppress_yf_noise()
import yfinance as yf
import pandas as pd

from daily_direction.quality import utc_now


RISK_KEYWORDS = {
    "regulation": ["investigation", "probe", "lawsuit", "监管", "调查", "诉讼", "立案"],
    "policy": ["tariff", "sanction", "export control", "rate decision", "federal reserve", "制裁", "关税", "出口管制", "降息", "加息", "美联储"],
    "earnings": ["guidance cut", "miss", "profit warning", "业绩预警", "业绩预亏", "下修指引"],
    "credit": ["default", "bankruptcy", "liquidity", "违约", "破产", "流动性"],
    "geopolitical": ["war", "attack", "conflict", "地缘", "战争", "冲突"],
}


def _clean(value: Any) -> str:
    plain = re.sub(r"<[^>]+>", "", html.unescape(str(value or ""))).replace("\u200b", "")
    return re.sub(r"\s+", " ", plain).strip()


def _recent(item: Mapping[str, Any], moment: pd.Timestamp) -> bool:
    published = str(item.get("published") or "")
    stamp = pd.to_datetime(int(published), unit="s", utc=True, errors="coerce") if published.isdigit() else pd.to_datetime(published, utc=True, errors="coerce")
    return bool(not pd.isna(stamp) and moment - pd.Timedelta(hours=72) <= stamp <= moment)


def fetch_market_feed(source: str, market: str, *, now: Any = None) -> tuple[list[dict], dict]:
    """Public read-only feeds, with bounded requests and auditable failures."""
    moment = utc_now(now)
    diagnostic = {"source": source, "market": market, "raw": 0, "accepted": 0}
    try:
        if source == "wallstreetcn":
            channel = {"us": "us-stock-channel", "cn": "a-stock-channel", "hk": "hk-stock-channel"}[market]
            response = requests.get("https://api-one.wallstcn.com/apiv1/content/lives",
                                    params={"channel": channel, "limit": 50}, timeout=(4, 10))
        else:
            response = requests.get("https://np-listapi.eastmoney.com/comm/web/getFastNewsList",
                                    params={"client": "web", "biz": "web_724", "fastColumn": "102", "sortEnd": "", "pageSize": 50, "req_trace": "1"}, timeout=(4, 10))
        response.raise_for_status()
        payload = response.json()
        if str(payload.get("code")) != ("20000" if source == "wallstreetcn" else "1"):
            raise ValueError("feed returned unsuccessful code")
        data = payload.get("data") or {}
        rows = data.get("items" if source == "wallstreetcn" else "fastNewsList") or []
        diagnostic["raw"] = len(rows)
        items = []
        for row in rows:
            if source == "wallstreetcn":
                summary = _clean(row.get("content_text"))
                title = _clean(row.get("title")) or re.split(r"[。\n]", summary)[0][:100]
                stamp = pd.to_datetime(row.get("display_time"), unit="s", utc=True, errors="coerce")
                url = str(row.get("uri") or "")
                publisher = "华尔街见闻"
                importance = int(row.get("score") or 0)
            else:
                summary = _clean(row.get("summary"))
                title = _clean(row.get("title"))
                stamp = pd.to_datetime(row.get("showTime"), errors="coerce")
                if not pd.isna(stamp):
                    stamp = stamp.tz_localize("Asia/Shanghai").tz_convert("UTC")
                code = str(row.get("code") or "")
                url = f"https://finance.eastmoney.com/a/{code}.html" if code.isdigit() else ""
                publisher = "东方财富"
                importance = int(row.get("titleColor") or 0)
            item = {"title": title, "summary": summary[:600], "published": stamp.isoformat() if not pd.isna(stamp) else "",
                    "url": url, "publisher": publisher, "market": market, "source": source, "importance": importance}
            if title and url.startswith("https://") and _recent(item, moment):
                items.append(item)
        diagnostic.update(accepted=len(items), status="ok" if items else "no_recent_items")
        return items, diagnostic
    except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError) as exc:
        diagnostic.update(status="error", error=type(exc).__name__)
        return [], diagnostic


def fetch_market_news(*, now: Any = None) -> tuple[list[dict], list[dict]]:
    jobs = [("wallstreetcn", key) for key in ("us", "cn", "hk")] + [("eastmoney", "global")]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda job: fetch_market_feed(*job, now=now), jobs))
    def relevance(item: dict) -> tuple:
        title = item["title"]
        keywords = ("美股", "收盘", "纳指", "标普", "恒指", "港股", "A股", "关税", "美联储", "利率", "国债", "财报", "AI", "芯片", "半导体", "政策", "财政", "出口", "央行")
        local_keywords = {"us": ("美股", "标普", "纳指", "美联储", "美国", "AI", "芯片"),
                          "cn": ("我国", "中国", "央行", "七部门", "A股", "沪", "深", "工信部"),
                          "hk": ("港股", "香港", "恒", "A+H", "南向", "腾讯", "小米", "阿里")}.get(item["market"], ())
        return (3 * sum(word in title for word in local_keywords) + sum(word in title for word in keywords) + min(item["importance"], 3), item["published"])
    groups = [sorted(items, key=relevance, reverse=True)[:6] for items, _ in results]
    # Interleave markets, avoiding a flood of unrelated latest domestic headlines.
    items, seen = [], set()
    for index in range(6):
        for group in groups:
            if index >= len(group):
                continue
            item = group[index]
            key = re.sub(r"\W", "", item["title"])
            if key not in seen:
                seen.add(key)
                items.append(item)
    return items, [diagnostic for _, diagnostic in results]


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
            "published": str(inner.get("pubDate") or inner.get("displayTime") or ""),
            "url": _news_url(link),
        }
    return {
        "ticker": ticker,
        "title": str(item.get("title") or "").strip(),
        "summary": str(item.get("summary") or "").strip(),
        "publisher": str(item.get("publisher") or "").strip(),
        "published": str(item.get("published") or item.get("providerPublishTime") or ""),
        "url": str(item.get("link") or "").strip(),
    }


def fetch_ticker_news(
    tickers: Iterable[str],
    *,
    max_items_per_ticker: int = 2,
    now: Any = None,
    diagnostics: list[dict] | None = None,
) -> Dict[str, List[Dict[str, str]]]:
    out: Dict[str, List[Dict[str, str]]] = {}
    moment = utc_now(now)
    for ticker in tickers:
        symbol = str(ticker or "").strip().upper()
        if not symbol:
            continue
        diagnostic = {"source": "yahoo", "ticker": symbol}
        try:
            raw_items = yf.Ticker(symbol).news or []
        except Exception as exc:
            diagnostic.update(status="error", error=type(exc).__name__)
            raw_items = []
        items = []
        for raw in raw_items:
            if not isinstance(raw, Mapping):
                continue
            item = normalize_yf_news_item(symbol, raw)
            if not _recent(item, moment):
                continue
            published = item["published"]
            stamp = pd.to_datetime(int(published), unit="s", utc=True) if published.isdigit() else pd.to_datetime(published, utc=True)
            item.update(published=stamp.isoformat(), title=_clean(item["title"]), summary=_clean(item["summary"]))
            if item.get("title"):
                items.append(item)
            if len(items) >= max_items_per_ticker:
                break
        if items:
            out[symbol] = items
        if diagnostics is not None:
            diagnostic.update(raw=len(raw_items), accepted=len(items))
            diagnostic.setdefault("status", "ok" if items else "no_recent_items")
            diagnostics.append(diagnostic)
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
        "market_news": market_items[:18],
        "ticker_news": ticker_items,
        "event_risks": risks[:10],
    }


def _unique_tickers_from_snapshots(snapshots: Mapping[str, Mapping[str, Any]], max_tickers: int) -> List[str]:
    tickers: List[str] = []
    groups = [list(snap.get("top_signals") or [])[:5] + list(snap.get("top_losers") or [])[:5] for snap in snapshots.values()]
    for index in range(max((len(group) for group in groups), default=0)):
        rows = [group[index] for group in groups if index < len(group)]
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
    now: Any = None,
) -> Dict[str, Any]:
    from daily_direction.reporting import identity

    macro_tickers = ["SPY", "QQQ", "ASHR", "FXI", "2800.HK"]
    tickers = _unique_tickers_from_snapshots(snapshots, max_tickers=max_tickers)
    market_news, diagnostics = fetch_market_news(now=now)
    ticker_news = {}
    for ticker in tickers:
        name, _ = identity(ticker)
        matches = [item for item in market_news if (name and name in item["title"] + item["summary"]) or
                   (not ticker[0].isdigit() and re.search(r"\b" + re.escape(ticker) + r"\b", item["title"] + " " + item["summary"], flags=re.I))]
        if matches:
            ticker_news[ticker] = [{**item, "ticker": ticker} for item in matches[:max_items_per_ticker]]
    # Yahoo is a fallback, rather than 23 repeated requests to an empty endpoint.
    if not market_news:
        ticker_news = fetch_ticker_news(tickers, max_items_per_ticker=max_items_per_ticker, now=now, diagnostics=diagnostics)
        macro_news_map = fetch_ticker_news(macro_tickers, max_items_per_ticker=1, now=now, diagnostics=diagnostics)
        market_news = [item for items in macro_news_map.values() for item in items]
    context = build_news_context(market_news=market_news, ticker_news=ticker_news)
    context["diagnostics"] = diagnostics
    context["fetched_at"] = utc_now(now).isoformat()
    return context
