"""
yfinance / Yahoo 抓取重试助手。

聚焦最常见的两类临时性失败：
1. DNS 解析失败，例如 `Could not resolve host`
2. Yahoo 限流，例如 HTTP 429 / Too Many Requests
"""
from __future__ import annotations

import time
from typing import Callable, Optional


_DNS_ERROR_TOKENS = (
    "could not resolve host",
    "name or service not known",
    "temporary failure in name resolution",
    "nodename nor servname provided",
    "failed to perform, curl: (6)",
)

_RATE_LIMIT_TOKENS = (
    "429",
    "too many requests",
    "rate limit",
    "rate-limited",
)


def classify_yahoo_error(exc: Exception) -> str:
    """将常见 Yahoo / curl_cffi / yfinance 异常归类为 dns / rate_limit / other。"""
    msg = str(exc or "").strip().lower()
    if any(token in msg for token in _DNS_ERROR_TOKENS):
        return "dns"
    if any(token in msg for token in _RATE_LIMIT_TOKENS):
        return "rate_limit"
    return "other"


def fetch_with_retry(
    action: Callable[[], object],
    *,
    ticker: str,
    op_name: str = "history",
    max_attempts: int = 3,
    dns_base_delay: float = 1.0,
    rate_limit_base_delay: float = 2.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    logger: Optional[Callable[[str], None]] = print,
):
    """
    对 Yahoo 临时性错误进行有限重试。

    - DNS 失败：较短退避，尽快重试
    - 429 限流：更长退避，避免立即再次打满
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            return action()
        except Exception as exc:
            last_exc = exc
            kind = classify_yahoo_error(exc)
            retryable = kind in {"dns", "rate_limit"} and attempt < max_attempts
            if logger is not None:
                logger(
                    f"[YahooRetry] {ticker} {op_name} attempt={attempt}/{max_attempts} "
                    f"kind={kind} error={str(exc).strip()[:160]}"
                )
            if not retryable:
                raise
            base_delay = dns_base_delay if kind == "dns" else rate_limit_base_delay
            sleep_fn(base_delay * (2 ** (attempt - 1)))
    if last_exc is not None:
        raise last_exc
    return None
