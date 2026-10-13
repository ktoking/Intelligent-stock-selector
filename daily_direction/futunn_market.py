"""Read-only Futunn snapshot validation; credentials stay outside the repository."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable
import tempfile
import pandas as pd
import numpy as np

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def configured() -> bool:
    return bool(os.getenv("FUTUNN_APP_KEY") and os.getenv("FUTUNN_PRIVATE_KEY_FILE"))


_REQUEST_LOCK = threading.Lock()
_NEXT_REQUEST = 0.0


def _pace() -> None:
    global _NEXT_REQUEST
    with _REQUEST_LOCK:
        delay = max(0, _NEXT_REQUEST - time.monotonic())
        if delay:
            time.sleep(delay)
        _NEXT_REQUEST = time.monotonic() + 2.0


def _cooldown(seconds: float) -> None:
    global _NEXT_REQUEST
    with _REQUEST_LOCK:
        _NEXT_REQUEST = max(_NEXT_REQUEST, time.monotonic() + seconds)


def futunn_symbol(ticker: str) -> str:
    if ticker == "^HSI":
        return "HK.800000"
    if ticker.endswith(".HK"):
        return "HK." + ticker[:-3].zfill(5)
    if ticker.endswith(".SS"):
        return "SH." + ticker[:-3]
    if ticker.endswith(".SZ"):
        return "SZ." + ticker[:-3]
    return "US." + ticker


def _history_request(symbol: str, start: str, end: str) -> dict:
    if not re.fullmatch(r"(?:US|HK|SH|SZ)\.[A-Za-z0-9.-]+", symbol):
        raise ValueError("Unsupported history symbol")
    key = serialization.load_pem_private_key(Path(os.environ["FUTUNN_PRIVATE_KEY_FILE"]).read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Futunn requires Ed25519")
    path = f"/api/v1.0/quote/{symbol}/history-kline"
    query = urllib.parse.urlencode({"start": start, "end": end, "ktype": 2, "num": 370, "autype": 1})
    for attempt in range(4):
        _pace()
        stamp = str(int(time.time() * 1000))
        signature = base64.b64encode(key.sign("\n".join([stamp, "GET", path, query, ""]).encode())).decode()
        request = urllib.request.Request("https://webapi.futunn.com" + path + "?" + query, headers={
            "X-Api-Key": os.environ["FUTUNN_APP_KEY"], "X-Timestamp": stamp,
            "X-Nonce": secrets.token_urlsafe(24), "Authorization": signature,
        })
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.load(response)
                retry_header = response.headers.get("Retry-After")
            if result.get("ret_code") == 0:
                return result["data"]
            if result.get("ret_code") != -11:
                raise ValueError(f"Futunn history ret_code={result.get('ret_code')}")
            retry = re.search(r"retry after (\d+)s", str(result.get("error", {}).get("message", "")) + str(result.get("ret_msg", "")), re.I)
            delay = int(retry_header) + 1 if retry_header else int(retry.group(1)) + 1 if retry else 15 * (attempt + 1)
        except urllib.error.HTTPError as exc:
            if exc.code != 429:
                raise
            delay = int(exc.headers.get("Retry-After", 2 ** (attempt + 1)))
        if attempt < 3:
            _cooldown(delay)
    raise ValueError("Futunn history rate limit exhausted")


def history_frame(data: dict, session: str) -> pd.DataFrame:
    """Keep one consistent split-adjustment basis and exchange-local dates."""
    rows = data.get("kline_list") or []
    frame = pd.DataFrame(rows)
    required = ["date", "open", "high", "low", "close", "volume"]
    if frame.empty or not all(key in frame for key in required):
        raise ValueError("Missing daily OHLCV")
    dates = pd.to_datetime(frame["date"].astype(str), format="%Y%m%d", errors="coerce")
    if dates.isna().any() or (dates > pd.Timestamp(session)).any():
        raise ValueError("Invalid or incomplete daily session")
    prices = frame[["open", "high", "low", "close", "volume"]].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(prices.to_numpy()).all() or (prices[["open", "high", "low", "close"]] <= 0).any().any() or (prices.volume < 0).any():
        raise ValueError("Invalid daily prices or volume")
    if (prices.high < prices[["open", "close", "low"]].max(axis=1)).any() or (prices.low > prices[["open", "close", "high"]].min(axis=1)).any():
        raise ValueError("Invalid daily high/low")
    prices.index = pd.DatetimeIndex(dates)
    prices = prices.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"}).sort_index()
    prices = prices[~prices.index.duplicated(keep="last")]
    if len(prices) < 22 or prices.index[-1].date().isoformat() != session:
        raise ValueError("History does not reach completed session")
    precision = int(data.get("volume_precision") or 0)
    prices["Volume"] /= 10 ** precision
    last = max(rows, key=lambda row: row["date"])
    prices.attrs.update(quote_source="futunn", company_name=last.get("sc_name") or last.get("name") or "", adjustment="split_forward_excluding_dividends")
    return prices


def fetch_daily_history(tickers: Iterable[str], *, session: str, cache_dir: Path | None = None) -> tuple[dict, list[dict]]:
    """Read-only REST history; exact-session local cache and bounded concurrency."""
    if not configured():
        return {}, [{"source": "futunn", "status": "not_configured"}]
    cache_dir = cache_dir or Path.home() / ".local/state/stock-agent/daily-direction-history/v1"
    start = (pd.Timestamp(session) - pd.DateOffset(years=1)).date().isoformat()
    def fetch(ticker):
        symbol = futunn_symbol(ticker)
        cache = cache_dir / symbol / f"{session}-autype1.json"
        diagnostic = {"ticker": ticker, "symbol": symbol, "source": "futunn", "session": session}
        try:
            try:
                stored = json.loads(cache.read_text())
                if stored.get("start") != start or stored.get("end") != session or stored.get("symbol") != symbol or stored.get("autype") != 1:
                    raise ValueError("Cache query mismatch")
                frame = history_frame(stored["data"], session)
                return ticker, frame, {**diagnostic, "status": "cached", "rows": len(frame)}
            except (OSError, ValueError, KeyError, TypeError):
                pass
            data = _history_request(symbol, start, session)
            frame = history_frame(data, session)
            cache.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", dir=cache.parent, delete=False, encoding="utf-8") as temporary:
                json.dump({"symbol": symbol, "start": start, "end": session, "autype": 1, "data": data}, temporary, ensure_ascii=False)
            Path(temporary.name).replace(cache)
            return ticker, frame, {**diagnostic, "status": "ok", "rows": len(frame)}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return ticker, None, {**diagnostic, "status": "error", "error": str(exc)[:160]}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(fetch, dict.fromkeys(tickers)))
    return {ticker: frame for ticker, frame, _ in results if frame is not None}, [diag for _, _, diag in results]


def fetch_us_snapshots(tickers: Iterable[str]) -> dict[str, dict]:
    if not configured():
        return {}
    private_key = serialization.load_pem_private_key(Path(os.environ["FUTUNN_PRIVATE_KEY_FILE"]).read_bytes(), password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        raise RuntimeError("Futunn requires an Ed25519 key")
    symbols = list(dict.fromkeys(tickers))
    result = {}
    path = "/api/v1.0/quote/snapshot"
    for start in range(0, len(symbols), 20):
        body = json.dumps({"code_list": [f"US.{ticker}" for ticker in symbols[start:start + 20]]}, separators=(",", ":")).encode()
        timestamp = str(int(time.time() * 1000))
        signing_text = "\n".join([timestamp, "POST", path, "", hashlib.sha256(body).hexdigest()]).encode()
        request = urllib.request.Request(
            f"https://webapi.futunn.com{path}", data=body,
            headers={
                "Content-Type": "application/json", "X-Api-Key": os.environ["FUTUNN_APP_KEY"],
                "X-Timestamp": timestamp, "X-Nonce": secrets.token_urlsafe(24),
                "Authorization": base64.b64encode(private_key.sign(signing_text)).decode(),
            },
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.load(response)
        if data.get("ret_code") != 0:
            raise RuntimeError(f"Futunn quote error {data.get('ret_code')}")
        for row in (data.get("data") or {}).get("snapshot_list", []):
            result[str(row.get("code", "")).removeprefix("US.")] = row
    return result
