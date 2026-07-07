from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import requests


def _truncate_utf8(text: str, max_bytes: int) -> str:
    raw = (text or "").encode("utf-8")
    if len(raw) <= max_bytes:
        return text or ""
    marker = "\n...(内容过长，已截断)"
    budget = max(0, max_bytes - len(marker.encode("utf-8")))
    return raw[:budget].decode("utf-8", errors="ignore") + marker


def build_text_payload(
    text: str,
    *,
    at_all: bool = False,
    max_bytes: int = 3900,
) -> Dict[str, Any]:
    body = {"tag": "text", "text": {"content": _truncate_utf8(text, max_bytes)}}
    if at_all:
        body["text"]["at_all"] = True
    return body


def _query_signature(signing_secret: str, timestamp_ms: int) -> str:
    string_to_sign = f"{timestamp_ms}\n{signing_secret}".encode("utf-8")
    digest = hmac.new(signing_secret.encode("utf-8"), string_to_sign, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def build_webhook_url(
    webhook_url: str,
    *,
    signing_secret: str = "",
    timestamp_ms: Optional[int] = None,
    sign_mode: str = "auto",
) -> str:
    url = (webhook_url or "").strip()
    if not url:
        raise ValueError("webhook_url is required")
    mode = (sign_mode or "auto").strip().lower()
    if mode in {"none", "off", "false"} or not signing_secret:
        return url
    if mode not in {"auto", "query"}:
        raise ValueError("sign_mode must be auto, query, or none")

    ts = int(timestamp_ms if timestamp_ms is not None else time.time() * 1000)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{urlencode({'timestamp': ts, 'sign': _query_signature(signing_secret, ts)})}"


def post_text(
    webhook_url: str,
    text: str,
    *,
    signing_secret: str = "",
    sign_mode: str = "auto",
    timeout: int = 20,
    at_all: bool = False,
) -> Dict[str, Any]:
    payload = build_text_payload(text, at_all=at_all)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}

    signed_url = build_webhook_url(webhook_url, signing_secret=signing_secret, sign_mode=sign_mode)
    resp = requests.post(signed_url, data=body, headers=headers, timeout=timeout)
    if resp.status_code in {400, 401, 403} and signing_secret and sign_mode == "auto":
        resp = requests.post(webhook_url, data=body, headers=headers, timeout=timeout)
    if resp.status_code >= 400:
        raise RuntimeError(f"SeaTalk webhook HTTP {resp.status_code}: {resp.text[:500]}")
    return {
        "status_code": resp.status_code,
        "response": resp.text[:1000],
    }
