from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, Optional


DEFAULT_BASE_URL = "https://openapi.iwencai.com"
SKILL_ID = "hithink-market-query"
SKILL_VERSION = "1.0.0"


def _normalize_cn_ticker(value: str) -> str:
    raw = (value or "").strip().upper()
    if not raw:
        return raw
    if raw.endswith(".SH"):
        return raw[:-3] + ".SS"
    return raw


def _float_from_any(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    if text.endswith("%"):
        text = text[:-1]
    try:
        return float(text)
    except ValueError:
        return None


def _pick_float(item: Dict[str, Any], *contains: str) -> Optional[float]:
    for key, value in item.items():
        label = str(key)
        if all(part in label for part in contains):
            parsed = _float_from_any(value)
            if parsed is not None:
                return parsed
    return None


def _build_headers(api_key: str, call_type: str = "normal") -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Claw-Call-Type": call_type,
        "X-Claw-Skill-Id": SKILL_ID,
        "X-Claw-Skill-Version": SKILL_VERSION,
        "X-Claw-Plugin-Id": "none",
        "X-Claw-Plugin-Version": "none",
        "X-Claw-Trace-Id": secrets.token_hex(32),
    }


def _call_query2data(query: str, *, limit: int, timeout: int) -> Dict[str, Any]:
    api_key = os.environ.get("IWENCAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("IWENCAI_API_KEY is not configured")
    base_url = (os.environ.get("IWENCAI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    payload = {
        "query": query,
        "page": "1",
        "limit": str(max(1, limit)),
        "is_cache": "1",
        "expand_index": "true",
    }
    req = urllib.request.Request(
        f"{base_url}/v1/query2data",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=_build_headers(api_key),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore") if exc.fp else ""
        raise RuntimeError(f"hithink HTTP {exc.code}: {body[:300]}") from exc


def _parse_quote_item(item: Dict[str, Any]) -> Optional[Dict[str, float]]:
    code = _normalize_cn_ticker(str(item.get("股票代码") or item.get("成分代码") or ""))
    if not code:
        return None
    close = _pick_float(item, "最新价") or _pick_float(item, "收盘价")
    daily_pct = _pick_float(item, "涨跌幅")
    volume = _pick_float(item, "成交量")
    amount = _pick_float(item, "成交额")
    high = _pick_float(item, "最高")
    low = _pick_float(item, "最低")
    open_price = _pick_float(item, "开盘")
    out: Dict[str, float] = {}
    if close is not None:
        out["close"] = close
    if daily_pct is not None:
        out["daily_pct"] = daily_pct
    if volume is not None:
        out["volume"] = volume
    elif amount is not None and close:
        out["volume"] = amount / close
    if high is not None:
        out["high"] = high
    if low is not None:
        out["low"] = low
    if open_price is not None:
        out["open"] = open_price
    return {"ticker": code, **out}


def fetch_hithink_cn_quotes(
    tickers: Iterable[str],
    *,
    limit: int = 120,
    batch_size: int = 20,
    timeout: int = 20,
) -> Dict[str, Dict[str, float]]:
    """Fetch latest A-share quotes from hithink-market-query / iWenCai."""
    wanted = [_normalize_cn_ticker(t) for t in tickers if str(t or "").strip()]
    if not wanted:
        return {}
    out: Dict[str, Dict[str, float]] = {}
    capped = wanted[:limit]
    size = max(1, min(batch_size, 30))
    for start in range(0, len(capped), size):
        batch = capped[start : start + size]
        query_codes = "、".join(t.replace(".SS", ".SH") for t in batch)
        query = f"{query_codes} 最新价 涨跌幅 成交量 成交额 开盘价 最高价 最低价"
        result = _call_query2data(query, limit=max(size, len(batch)), timeout=timeout)
        datas = result.get("datas") or []
        if not isinstance(datas, list):
            continue
        for item in datas:
            if not isinstance(item, dict):
                continue
            parsed = _parse_quote_item(item)
            if not parsed:
                continue
            ticker = str(parsed.pop("ticker"))
            if ticker in batch and parsed:
                out[ticker] = parsed
    return out
