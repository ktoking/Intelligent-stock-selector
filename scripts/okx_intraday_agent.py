#!/usr/bin/env python3
"""Demo-only OKX intraday agent for tokenized-equity perpetuals.

The decision path is deterministic: market data -> indicators -> risk checks ->
demo order.  An LLM can be added for commentary, but never approves an order.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9/3.10 compatibility for the running FastAPI service.
    import tomli as tomllib
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from config.env_loader import load_env

load_env()
LOG = logging.getLogger("okx-intraday")
NY = ZoneInfo("America/New_York")
UTC = timezone.utc
SEATALK_WEBHOOK_KEYCHAIN_SERVICE = "stock-agent-seatalk-trading-webhook"
STATE_PATH = ROOT / "data" / "okx_intraday_state.json"
DB_PATH = ROOT / "data" / "okx_intraday.db"
YF_EVENTS_CACHE_PATH = ROOT / "data" / "yfinance_market_events.json"
EXECUTION_STATE_PATH = ROOT / "data" / "okx_execution_state.json"
KILL_SWITCH_PATH = ROOT / "data" / "okx_kill_switch.json"
MONITOR_CONTROL_PATH = ROOT / "data" / "okx_monitor_control.json"
# Keep the initial universe liquid and interpretable.  The environment variable
# OKX_INTRADAY_SYMBOLS still takes precedence for a custom universe.
DEFAULT_SCAN_SYMBOLS = (
    "BTC-USDT-SWAP,SPY-USDT-SWAP,QQQ-USDT-SWAP,SMH-USDT-SWAP,"
    "NVDA-USDT-SWAP,AMD-USDT-SWAP,AVGO-USDT-SWAP,MU-USDT-SWAP,"
    "MRVL-USDT-SWAP,INTC-USDT-SWAP,TSM-USDT-SWAP,SNDK-USDT-SWAP,"
    "AAPL-USDT-SWAP,MSFT-USDT-SWAP,AMZN-USDT-SWAP,META-USDT-SWAP,GOOGL-USDT-SWAP,"
    "TSLA-USDT-SWAP,MSTR-USDT-SWAP,HOOD-USDT-SWAP,COIN-USDT-SWAP,"
    "PLTR-USDT-SWAP,SMCI-USDT-SWAP,RKLB-USDT-SWAP,ASTS-USDT-SWAP,IREN-USDT-SWAP,"
    # Live public market data + current Demo-account private-trading support.
    "ADBE-USDT-SWAP,ORCL-USDT-SWAP,CRWV-USDT-SWAP,AAOI-USDT-SWAP,CRCL-USDT-SWAP,SAMSUNG-USDT-SWAP"
)
_DEMO_TRADEABLE_CACHE: dict[str, bool] = {}


@dataclass(frozen=True)
class Settings:
    profile: str
    base_url: str
    proxy_url: str | None
    symbols: tuple[str, ...]
    leverage: int
    max_dynamic_leverage: int
    quote_risk_usdt: float
    max_daily_loss_usdt: float
    daily_loss_guard_bypass_date: str
    max_entries_per_day: int
    max_open_positions: int
    cooldown_minutes: int
    notify_group_id: str
    notify_webhook_url: str
    session: str
    risk_fraction: float
    max_notional_usdt: float
    max_gross_notional_usdt: float
    max_spread_bps: float
    max_slippage_bps: float
    max_holding_minutes: int


def profile_settings(profile: str) -> tuple[dict[str, str], str | None, str]:
    path = Path.home() / ".okx" / "config.toml"
    raw = tomllib.loads(path.read_text())
    value = (raw.get("profiles") or {}).get(profile) or {}
    required = ("api_key", "secret_key", "passphrase")
    if any(not value.get(key) for key in required):
        raise RuntimeError(f"OKX profile {profile!r} is missing credentials")
    site = value.get("site", "global")
    base_url = {"global": "https://www.okx.com", "eea": "https://eea.okx.com", "us": "https://us.okx.com"}.get(site)
    if not base_url:
        raise RuntimeError(f"unsupported OKX site {site!r}")
    return {key: str(value[key]) for key in required}, value.get("proxy_url"), base_url


def keychain_secret(service: str) -> str:
    """Read an optional local secret without placing it in repo config."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            text=True, capture_output=True, check=True, timeout=5,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def yahoo_symbol(inst_id: str) -> str:
    return inst_id.split("-", 1)[0].upper()


def yfinance_events(inst_ids: tuple[str, ...], held_inst_ids: list[str]) -> dict[str, Any]:
    """Cached Yahoo Finance news and earnings calendar for analysis only."""
    try:
        cached = json.loads(YF_EVENTS_CACHE_PATH.read_text())
        generated_at = datetime.fromisoformat(cached["generated_at"])
        if datetime.now(UTC) - generated_at < timedelta(minutes=30):
            return cached
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        pass
    held = [yahoo_symbol(inst) for inst in held_inst_ids]
    symbols = list(dict.fromkeys(held + [yahoo_symbol(inst) for inst in inst_ids if yahoo_symbol(inst) != "BTC"]))[:12]
    events, news = [], []
    for symbol in symbols:
        try:
            ticker = yf.Ticker(symbol)
            calendar = ticker.calendar or {}
            earnings = calendar.get("Earnings Date") or []
            if earnings:
                events.append({"symbol": symbol, "type": "earnings", "date": str(earnings[0]), "held": symbol in held})
            for item in (ticker.news or [])[:2]:
                content = item.get("content", item)
                title = content.get("title") or item.get("title")
                published = content.get("pubDate") or item.get("providerPublishTime")
                if title:
                    news.append({"symbol": symbol, "title": str(title), "published_at": str(published or ""), "held": symbol in held})
        except Exception as exc:
            LOG.warning("yfinance event lookup skipped for %s: %s", symbol, exc)
        time.sleep(0.08)
    snapshot = {"generated_at": datetime.now(UTC).isoformat(), "source": "yfinance", "events": events, "news": news[:12]}
    YF_EVENTS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    YF_EVENTS_CACHE_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
    return snapshot


def settings() -> Settings:
    profile = os.getenv("OKX_INTRADAY_PROFILE", "demo")
    _, configured_proxy, base_url = profile_settings(profile)
    symbols = tuple(s.strip().upper() for s in os.getenv(
        "OKX_INTRADAY_SYMBOLS", DEFAULT_SCAN_SYMBOLS
    ).split(",") if s.strip())
    return Settings(
        profile=profile,
        base_url=os.getenv("OKX_API_BASE_URL", base_url),
        proxy_url=os.getenv("OKX_INTRADAY_PROXY_URL", configured_proxy or "") or None,
        symbols=symbols,
        leverage=min(max(int(os.getenv("OKX_INTRADAY_LEVERAGE", "3")), 1), 10),
        max_dynamic_leverage=min(max(int(os.getenv("OKX_INTRADAY_MAX_DYNAMIC_LEVERAGE", "5")), 1), 10),
        quote_risk_usdt=float(os.getenv("OKX_INTRADAY_RISK_USDT", "35")),
        max_daily_loss_usdt=float(os.getenv("OKX_INTRADAY_MAX_DAILY_LOSS_USDT", "50")),
        daily_loss_guard_bypass_date=os.getenv("OKX_INTRADAY_DAILY_LOSS_GUARD_BYPASS_DATE", "").strip(),
        # 0 means no daily entry cap.  Demo mode starts uncapped so the
        # strategy can gather samples; all other risk guards still apply.
        max_entries_per_day=max(0, int(os.getenv("OKX_INTRADAY_MAX_ENTRIES_PER_DAY", "0"))),
        max_open_positions=max(1, int(os.getenv("OKX_INTRADAY_MAX_OPEN_POSITIONS", "2"))),
        cooldown_minutes=max(0, int(os.getenv("OKX_INTRADAY_COOLDOWN_MINUTES", "30"))),
        notify_group_id=os.getenv("SEATALK_TRADING_GROUP_ID", "").strip(),
        notify_webhook_url=(os.getenv("SEATALK_TRADING_WEBHOOK_URL", "").strip() or keychain_secret(SEATALK_WEBHOOK_KEYCHAIN_SERVICE)),
        session=os.getenv("OKX_INTRADAY_SESSION", "24x7").strip().lower(),
        risk_fraction=max(0.0001, float(os.getenv("OKX_INTRADAY_RISK_FRACTION", "0.0035"))),
        max_notional_usdt=max(10, float(os.getenv("OKX_INTRADAY_MAX_NOTIONAL_USDT", "1500"))),
        max_gross_notional_usdt=max(10, float(os.getenv("OKX_INTRADAY_MAX_GROSS_NOTIONAL_USDT", "7500"))),
        max_spread_bps=max(1, float(os.getenv("OKX_INTRADAY_MAX_SPREAD_BPS", "25"))),
        max_slippage_bps=max(1, float(os.getenv("OKX_INTRADAY_MAX_SLIPPAGE_BPS", "35"))),
        max_holding_minutes=max(5, int(os.getenv("OKX_INTRADAY_MAX_HOLDING_MINUTES", "60"))),
    )


class OKX:
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self.creds, _, _ = profile_settings(cfg.profile)
        self.session = requests.Session()
        if cfg.proxy_url:
            self.session.proxies.update({"http": cfg.proxy_url, "https": cfg.proxy_url})

    def request(self, method: str, path: str, params: dict[str, Any] | None = None,
                body: dict[str, Any] | None = None, private: bool = False) -> dict[str, Any]:
        request_path = path + (f"?{urlencode(params)}" if params else "")
        payload = json.dumps(body, separators=(",", ":")) if body else ""
        headers = {"Content-Type": "application/json"}
        if private:
            headers["x-simulated-trading"] = "1"
            timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            prehash = f"{timestamp}{method.upper()}{request_path}{payload}"
            signature = base64.b64encode(hmac.new(
                self.creds["secret_key"].encode(), prehash.encode(), hashlib.sha256
            ).digest()).decode()
            headers.update({
                "OK-ACCESS-KEY": self.creds["api_key"],
                "OK-ACCESS-SIGN": signature,
                "OK-ACCESS-TIMESTAMP": timestamp,
                "OK-ACCESS-PASSPHRASE": self.creds["passphrase"],
            })
        for attempt in range(3):
            try:
                response = self.session.request(method, self.cfg.base_url + path, params=params, data=payload or None,
                                                headers=headers, timeout=15)
            except requests.RequestException as exc:
                if attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                raise RuntimeError(f"OKX {path}: network request failed after retries: {exc}") from exc
            if response.status_code == 429 and attempt < 2:
                time.sleep(1.0 * (attempt + 1))
                continue
            response.raise_for_status()
            result = response.json()
            if result.get("code") != "0":
                # OKX sometimes puts the actionable per-order rejection in
                # data[].sCode/sMsg even when the top-level response is only
                # the unhelpful "All operations failed".
                detail = result.get("data") or []
                raise RuntimeError(f"OKX {path}: {result.get('code')} {result.get('msg')} detail={detail}")
            return result
        raise RuntimeError(f"OKX {path}: rate limit retries exhausted")

    def candles(self, inst_id: str, limit: int = 120, bar: str = "5m") -> list[list[str]]:
        # Equity perpetuals currently return 51001 from /market/candles but
        # support the older-bar endpoint used here.
        # Keep a 26-symbol scan below the public endpoint's burst limit.
        time.sleep(0.12)
        return self.request("GET", "/api/v5/market/history-candles", {"instId": inst_id, "bar": bar, "limit": str(min(max(limit, 40), 300))})["data"]

    def candles_ending_at(self, inst_id: str, end_ts: int, limit: int = 100,
                          bar: str = "1m") -> list[list[str]]:
        """Return candles ending at a historical timestamp, safe after downtime.

        OKX names the cursor ``after`` even though it returns records older
        than that timestamp.  Adding one millisecond includes the candle whose
        timestamp equals ``end_ts``.
        """
        time.sleep(0.12)
        return self.request("GET", "/api/v5/market/history-candles", {
            "instId": inst_id, "bar": bar,
            "limit": str(min(max(limit, 40), 300)), "after": str(int(end_ts) + 1),
        })["data"]

    def ticker(self, inst_id: str) -> dict[str, str]:
        return self.request("GET", "/api/v5/market/ticker", {"instId": inst_id})["data"][0]

    def instrument(self, inst_id: str) -> dict[str, str]:
        rows = self.request("GET", "/api/v5/public/instruments", {"instType": "SWAP", "instId": inst_id})["data"]
        if not rows:
            raise RuntimeError(f"instrument not found: {inst_id}")
        return rows[0]

    def positions(self, inst_id: str) -> list[dict[str, str]]:
        # Demo accounts can reject an equity-perpetual instId even though the
        # public market feed is live. Query the account portfolio then filter.
        rows = self.request("GET", "/api/v5/account/positions", private=True)["data"]
        return [row for row in rows if row.get("instId") == inst_id]

    def balance(self) -> dict[str, Any]:
        return self.request("GET", "/api/v5/account/balance", private=True)["data"][0]

    def set_leverage(self, inst_id: str, pos_side: str, leverage: int | None = None) -> None:
        self.request("POST", "/api/v5/account/set-leverage", body={
            "instId": inst_id, "lever": str(leverage or self.cfg.leverage), "mgnMode": "cross", "posSide": pos_side,
        }, private=True)

    def max_size(self, inst_id: str) -> dict[str, Any]:
        return self.request("GET", "/api/v5/account/max-size", {"instId": inst_id, "tdMode": "cross"}, private=True)

    def place_market(self, inst_id: str, side: str, size: str, pos_side: str,
                     stop_px: str | None = None, target_px: str | None = None,
                     reduce_only: bool = False, client_order_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "instId": inst_id, "tdMode": "cross", "side": side, "ordType": "market", "sz": size, "posSide": pos_side,
        }
        if reduce_only:
            body["reduceOnly"] = True
        if client_order_id:
            body["clOrdId"] = client_order_id
        if stop_px and target_px:
            body["attachAlgoOrds"] = [{
                "tpTriggerPx": target_px, "tpOrdPx": "-1", "tpTriggerPxType": "mark",
                "slTriggerPx": stop_px, "slOrdPx": "-1", "slTriggerPxType": "mark",
            }]
        return self.request("POST", "/api/v5/trade/order", body=body, private=True)["data"][0]

    def order(self, inst_id: str, order_id: str) -> dict[str, str]:
        return self.request("GET", "/api/v5/trade/order", {"instId": inst_id, "ordId": order_id}, private=True)["data"][0]

    def order_by_client_id(self, inst_id: str, client_order_id: str) -> dict[str, str] | None:
        """Return an order accepted under clOrdId, or None when it is absent.

        This lookup is the idempotency guard used before a failed market-order
        request is retried.  It prevents a late exchange acceptance from
        becoming a duplicate position.
        """
        result = self.request("GET", "/api/v5/trade/order", {
            "instId": inst_id, "clOrdId": client_order_id,
        }, private=True)
        rows = result.get("data") or []
        return rows[0] if rows else None

    def cancel_order(self, inst_id: str, order_id: str) -> dict[str, Any]:
        """Cancel an unfilled remainder before protecting a partial fill."""
        return self.request("POST", "/api/v5/trade/cancel-order", body={
            "instId": inst_id, "ordId": order_id,
        }, private=True)["data"][0]

    def pending_algos(self, inst_id: str | None = None) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        # Attached TP/SL can be reported as conditional, while a repaired
        # combined TP/SL is an OCO. Query both explicitly so a valid repair is
        # never mistaken for a missing protection order.
        for order_type in ("conditional", "oco"):
            params = {"ordType": order_type}
            if inst_id:
                params["instId"] = inst_id
            rows.extend(self.request("GET", "/api/v5/trade/orders-algo-pending", params, private=True)["data"])
        return rows

    def cancel_algos(self, orders: list[dict[str, str]]) -> list[dict[str, Any]]:
        if not orders:
            return []
        return self.request("POST", "/api/v5/trade/cancel-algos", body=orders, private=True)["data"]

    def place_oco_protection(self, inst_id: str, pos_side: str, size: str,
                             stop_px: str, target_px: str) -> dict[str, Any]:
        """Place a standalone reduce-only OCO only when an attached TP/SL is absent."""
        side = "sell" if pos_side == "long" else "buy"
        return self.request("POST", "/api/v5/trade/order-algo", body={
            "instId": inst_id, "tdMode": "cross", "side": side, "posSide": pos_side,
            "ordType": "oco", "sz": size, "reduceOnly": True,
            "tpTriggerPx": target_px, "tpOrdPx": "-1", "tpTriggerPxType": "mark",
            "slTriggerPx": stop_px, "slOrdPx": "-1", "slTriggerPxType": "mark",
        }, private=True)["data"][0]

    def place_stop_protection(self, inst_id: str, pos_side: str, size: str, stop_px: str) -> dict[str, Any]:
        """Place a standalone reduce-only stop for a runner position."""
        side = "sell" if pos_side == "long" else "buy"
        return self.request("POST", "/api/v5/trade/order-algo", body={
            "instId": inst_id, "tdMode": "cross", "side": side, "posSide": pos_side,
            "ordType": "conditional", "sz": size, "reduceOnly": True,
            "slTriggerPx": stop_px, "slOrdPx": "-1", "slTriggerPxType": "mark",
        }, private=True)["data"][0]

    def amend_algo_stop(self, inst_id: str, algo_id: str, stop_px: str) -> dict[str, Any]:
        return self.request("POST", "/api/v5/trade/amend-algos", body={
            "instId": inst_id, "algoId": algo_id,
            "newSlTriggerPx": stop_px, "newSlOrdPx": "-1", "newSlTriggerPxType": "mark",
        }, private=True)["data"][0]

    def amend_algo_protection(self, inst_id: str, algo_id: str, stop_px: str, target_px: str) -> dict[str, Any]:
        """Rebase a live TP/SL pair after the market order's actual fill price is known."""
        return self.request("POST", "/api/v5/trade/amend-algos", body={
            "instId": inst_id, "algoId": algo_id,
            "newTpTriggerPx": target_px, "newTpOrdPx": "-1", "newTpTriggerPxType": "mark",
            "newSlTriggerPx": stop_px, "newSlOrdPx": "-1", "newSlTriggerPxType": "mark",
        }, private=True)["data"][0]


def demo_instrument_tradeable(client: OKX, inst_id: str) -> bool:
    """Cache whether the current OKX Demo account accepts private trading for an instrument."""
    if inst_id in _DEMO_TRADEABLE_CACHE:
        return _DEMO_TRADEABLE_CACHE[inst_id]
    try:
        client.max_size(inst_id)
        supported = True
    except RuntimeError as exc:
        if "51001" not in str(exc):
            raise
        supported = False
    _DEMO_TRADEABLE_CACHE[inst_id] = supported
    return supported


def ema(values: list[float], period: int) -> float:
    value = values[0]
    alpha = 2 / (period + 1)
    for item in values[1:]:
        value = item * alpha + value * (1 - alpha)
    return value


def rsi(values: list[float], period: int = 14) -> float:
    moves = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = sum(max(x, 0) for x in moves[-period:]) / period
    losses = sum(max(-x, 0) for x in moves[-period:]) / period
    return 100.0 if losses == 0 else 100 - 100 / (1 + gains / losses)


def atr(rows: list[list[str]], period: int = 14) -> float:
    closed = [x for x in rows if x[8] == "1"]
    if len(closed) < period + 1:
        return 0.0
    ranges = []
    for index in range(1, len(closed)):
        high, low, previous_close = float(closed[index][2]), float(closed[index][3]), float(closed[index - 1][4])
        ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return sum(ranges[-period:]) / period


def vwap(rows: list[list[str]], period: int = 20) -> float:
    closed = [x for x in rows if x[8] == "1"][-period:]
    volume = sum(float(x[5]) for x in closed)
    return sum(float(x[4]) * float(x[5]) for x in closed) / volume if volume else float(closed[-1][4])


def confidence(action: str, metrics: dict[str, float]) -> int:
    """A transparent signal-quality score, not a price prediction guarantee."""
    if action == "WAIT":
        return 0
    volume_points = min(20, max(0, (metrics["volume_ratio"] - 1.5) * 12))
    trend_points = min(15, abs(metrics["ema9"] - metrics["ema21"]) / max(metrics["atr14"], 1e-9) * 8)
    rsi_points = 10 if 30 <= metrics["rsi14"] <= 70 else 4
    vwap_points = 10 if (action == "LONG") == (metrics["price"] > metrics["vwap20"]) else 0
    return round(min(85, 45 + volume_points + trend_points + rsi_points + vwap_points))


def aggregate_closed_candles(candles: list[list[str]], minutes: int) -> list[list[str]]:
    """Aggregate newest-first exchange candles into full chronological bars."""
    bucket_ms = minutes * 60_000
    grouped: dict[int, list[list[str]]] = {}
    for row in sorted((item for item in candles if len(item) >= 9 and item[8] == "1"), key=lambda item: int(item[0])):
        grouped.setdefault(int(row[0]) // bucket_ms * bucket_ms, []).append(row)
    result = []
    for bucket, rows in sorted(grouped.items()):
        covered = {int(row[0]) // 300_000 for row in rows}
        if minutes == 10 and len(covered) < 2:
            continue
        result.append([
            str(bucket), rows[0][1], str(max(float(row[2]) for row in rows)),
            str(min(float(row[3]) for row in rows)), rows[-1][4],
            str(sum(float(row[5]) for row in rows)),
            str(sum(float(row[6]) for row in rows)),
            str(sum(float(row[7]) for row in rows)), "1",
        ])
    return result


def signal_rows(rows: list[list[str]], breakout_period: int = 12) -> tuple[str, dict[str, float]]:
    closes = [float(x[4]) for x in rows if x[8] == "1"]
    volumes = [float(x[5]) for x in rows if x[8] == "1"]
    if len(closes) < 40:
        return "WAIT", {"reason": "not enough closed candles"}
    price, fast, slow = closes[-1], ema(closes[-35:], 9), ema(closes[-35:], 21)
    if len(closes) <= breakout_period:
        return "WAIT", {"reason": "not enough breakout history"}
    recent_high = max(closes[-breakout_period - 1:-1])
    recent_low = min(closes[-breakout_period - 1:-1])
    volume_ratio = volumes[-1] / max(sum(volumes[-21:-1]) / 20, 1e-9)
    current_rsi = rsi(closes)
    atr14, vwap20 = atr(rows), vwap(rows)
    metrics = {
        "price": price, "ema9": fast, "ema21": slow, "rsi14": current_rsi,
        "volume_ratio": volume_ratio, "breakout_high": recent_high, "breakout_low": recent_low,
        "atr14": atr14, "vwap20": vwap20,
    }
    if price > recent_high and price > vwap20 and fast > slow and 55 <= current_rsi <= 72 and volume_ratio >= 1.5:
        action = "LONG"
    elif price < recent_low and price < vwap20 and fast < slow and 28 <= current_rsi <= 45 and volume_ratio >= 1.5:
        action = "SHORT"
    else:
        action = "WAIT"
    metrics["confidence"] = confidence(action, metrics)
    return action, metrics


def signal(candles: list[list[str]]) -> tuple[str, dict[str, float]]:
    rows = list(reversed(candles))
    return signal_rows(rows)


def decimal_step(value: float, step: str, rounding: str = "down") -> str:
    quantum = Decimal(str(step))
    mode = ROUND_DOWN if rounding == "down" else ROUND_HALF_UP
    rounded = (Decimal(str(value)) / quantum).to_integral_value(rounding=mode) * quantum
    return format(rounded.normalize(), "f")


def spread_bps(ticker: dict[str, Any]) -> float:
    bid, ask = float(ticker.get("bidPx") or 0), float(ticker.get("askPx") or 0)
    mid = (bid + ask) / 2
    return (ask - bid) / mid * 10_000 if bid > 0 and ask >= bid and mid else float("inf")


def estimated_slippage_bps(book: dict[str, Any], side: str, contracts: float, ct_val: float) -> float:
    levels = book.get("asks" if side == "LONG" else "bids") or []
    remaining, cost, filled = contracts, 0.0, 0.0
    for level in levels:
        price, available = float(level[0]), float(level[1])
        take = min(remaining, available)
        cost += take * price
        filled += take
        remaining -= take
        if remaining <= 1e-12:
            break
    if filled <= 0 or remaining > 1e-12:
        return float("inf")
    average = cost / filled
    best = float(levels[0][0])
    return abs(average - best) / best * 10_000 if best else float("inf")


def risk_sized_order(cfg: Settings, balance: dict[str, Any], instrument: dict[str, str],
                     price: float, atr14: float) -> dict[str, Any]:
    equity = float(balance.get("totalEq") or 0)
    risk_budget = min(cfg.quote_risk_usdt, equity * cfg.risk_fraction)
    stop_distance = max(atr14 * 1.2, price * 0.002)
    raw_notional = risk_budget / max(stop_distance / price, 1e-9)
    # An individual position may use up to 20% of account equity, but the
    # portfolio-level gross-exposure gate still protects concurrent positions.
    notional = min(raw_notional, cfg.max_notional_usdt, equity * 0.2)
    ct_val = float(instrument.get("ctVal") or 0)
    if ct_val <= 0:
        raise RuntimeError("instrument ctVal is unavailable")
    raw_contracts = notional / (price * ct_val)
    lot, minimum = instrument.get("lotSz") or "1", float(instrument.get("minSz") or instrument.get("lotSz") or 1)
    contracts = float(decimal_step(raw_contracts, lot))
    if contracts < minimum:
        contracts = minimum
    actual_notional = contracts * ct_val * price
    if actual_notional > cfg.max_notional_usdt * 1.05:
        raise RuntimeError("minimum contract size exceeds the per-trade notional cap")
    return {
        "contracts": decimal_step(contracts, lot), "contracts_float": contracts,
        "notional": actual_notional, "risk_budget": risk_budget,
        "stop_distance": stop_distance, "ct_val": ct_val,
    }


def kill_switch() -> dict[str, Any]:
    try:
        data = json.loads(KILL_SWITCH_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        data = {"enabled": False, "reason": "", "updated_at": None}
    return {"enabled": bool(data.get("enabled")), "reason": str(data.get("reason") or ""), "updated_at": data.get("updated_at")}


def set_kill_switch(enabled: bool, reason: str = "manual dashboard control") -> dict[str, Any]:
    data = {"enabled": bool(enabled), "reason": reason, "updated_at": datetime.now(UTC).isoformat()}
    KILL_SWITCH_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = KILL_SWITCH_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    temporary.replace(KILL_SWITCH_PATH)
    return data


def monitor_control() -> dict[str, Any]:
    try:
        data = json.loads(MONITOR_CONTROL_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        data = {"enabled": True, "reason": "default enabled", "updated_at": None}
    return {"enabled": bool(data.get("enabled", True)), "reason": str(data.get("reason") or ""), "updated_at": data.get("updated_at")}


def set_monitor_control(enabled: bool, reason: str = "manual dashboard control") -> dict[str, Any]:
    data = {"enabled": bool(enabled), "reason": reason, "updated_at": datetime.now(UTC).isoformat()}
    MONITOR_CONTROL_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = MONITOR_CONTROL_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    temporary.replace(MONITOR_CONTROL_PATH)
    return data


def next_scan_boundary(now: datetime) -> datetime:
    """Return the next wall-clock 10-minute boundary (00/10/.../50)."""
    return now.replace(second=0, microsecond=0) + timedelta(minutes=10 - now.minute % 10)


def in_us_cash_session() -> bool:
    now = datetime.now(NY)
    return now.weekday() < 5 and (now.hour, now.minute) >= (9, 30) and (now.hour, now.minute) < (16, 0)


def notify(cfg: Settings, text: str) -> None:
    if cfg.notify_webhook_url:
        subprocess.run(["skynet-base", "seatalk", "group-message", "--webhook", cfg.notify_webhook_url,
                        "--text", text], check=True, timeout=30)
        return
    if not cfg.notify_group_id:
        LOG.info("SeaTalk not sent (SEATALK_TRADING_GROUP_ID not configured): %s", text)
        return
    env = os.environ.copy()
    subprocess.run(["skynet-base", "seatalk", "group-message", "--group-id", cfg.notify_group_id,
                    "--format", "markdown", "--text", text], env=env, check=True, timeout=30)


def _num(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def position_lines(client: OKX, inst_id: str) -> list[str]:
    instrument = client.instrument(inst_id)
    ct_val = float(instrument.get("ctVal") or 0)
    lines = []
    for pos in client.positions(inst_id):
        quantity = abs(float(pos.get("pos") or 0))
        if quantity == 0:
            continue
        mark = float(pos.get("markPx") or 0)
        notional = quantity * ct_val * mark
        side = "多" if pos.get("posSide") == "long" else "空"
        upl = float(pos.get("upl") or 0)
        pnl_display = f"🔴 +${_num(upl, 4)}" if upl >= 0 else f"🟢 -${_num(abs(upl), 4)}"
        lines.append(
            f"- **{side}仓**：{pos.get('pos')} 张，均价 ${_num(pos.get('avgPx'))}，现价 ${_num(mark)}\n"
            f"  成交名义金额（仓位价值）${_num(notional)}｜实际占用保证金 ${_num(pos.get('imr'))}｜浮盈亏 {pnl_display}｜{pos.get('lever')}x"
        )
    return lines


def account_line(client: OKX) -> str:
    balance = client.balance()
    usdt = next((x for x in balance.get("details", []) if x.get("ccy") == "USDT"), {})
    return (
        f"可用 USDT **${_num(usdt.get('availBal'))}**｜已占用/冻结 ${_num(usdt.get('frozenBal'))}\n"
        f"USDT 余额 ${_num(usdt.get('cashBal'))}｜模拟账户总权益 ${_num(balance.get('totalEq'))}"
    )


def today_pnl_line(client: OKX) -> str:
    """Net closed-position PnL plus current open PnL for trading notifications."""
    try:
        today = datetime.now(NY).date()
        history = client.request(
            "GET", "/api/v5/account/positions-history", {"instType": "SWAP", "limit": "100"}, private=True
        )["data"]
        closed_today = [row for row in history if datetime.fromtimestamp(
            int(row.get("uTime") or 0) / 1000, UTC
        ).astimezone(NY).date() == today]
        realized = sum(float(row.get("realizedPnl") or 0) for row in closed_today)
        positions = client.request("GET", "/api/v5/account/positions", private=True)["data"]
        open_upl = sum(float(row.get("upl") or 0) for row in positions if float(row.get("pos") or 0))
        total = realized + open_upl

        def display(value: float) -> str:
            return f"🔴 +${value:,.4f}" if value >= 0 else f"🟢 -${abs(value):,.4f}"

        return (
            "**今日交易盈亏**\n"
            f"已实现 {display(realized)}｜持仓浮盈亏 {display(open_upl)}｜合计 {display(total)}"
        )
    except Exception as exc:
        LOG.warning("today PnL lookup skipped for notification: %s", exc)
        return "**今日交易盈亏**：暂时无法读取"


def decision_text(item: dict[str, Any]) -> str:
    action = item["action"]
    price, fast, slow = item["price"], item["ema9"], item["ema21"]
    rsi_value, volume = item["rsi14"], item["volume_ratio"]
    if action == "LONG" and item.get("executed"):
        return f"**做多，市价执行**：价格突破 ${item['breakout_high']:.2f}，EMA9 高于 EMA21，RSI {rsi_value:.1f} 未过热，成交量为均量 {volume:.2f} 倍。"
    if action == "SHORT" and item.get("executed"):
        return f"**做空，市价执行**：价格跌破 ${item['breakout_low']:.2f}，EMA9 低于 EMA21，RSI {rsi_value:.1f} 偏弱，成交量为均量 {volume:.2f} 倍。"
    if action in {"LONG", "SHORT"}:
        direction = "做多" if action == "LONG" else "做空"
        reason = item.get("reason") or "模拟自动下单未开启"
        return f"**{direction}信号触发，未下单**：{reason}。"
    trend = "偏弱" if fast < slow else "偏强"
    return (
        f"**观望，不下单**：5 分钟趋势{trend}（EMA9 ${fast:.2f} / EMA21 ${slow:.2f}），"
        f"RSI {rsi_value:.1f} 中性，成交量只有均量 {volume:.2f} 倍，未出现有效突破。\n"
        f"做多触发：站上 ${item['breakout_high']:.2f} 且量比 ≥ 1.50；"
        f"做空触发：跌破 ${item['breakout_low']:.2f} 且量比 ≥ 1.50。"
    )


def _read_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS okx_scan_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, scanned_at TEXT NOT NULL, inst_id TEXT NOT NULL,
            action TEXT NOT NULL, price REAL, rsi REAL, volume_ratio REAL, breakout_high REAL, breakout_low REAL
        );
        CREATE TABLE IF NOT EXISTS okx_order_fills (
            order_id TEXT PRIMARY KEY, fill_time TEXT, inst_id TEXT, side TEXT, pos_side TEXT,
            fill_size REAL, fill_price REAL, fee REAL, realized_pnl REAL
        );
        CREATE TABLE IF NOT EXISTS okx_account_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at TEXT NOT NULL, total_equity REAL,
            usdt_available REAL, usdt_frozen REAL, open_upl REAL
        );
    """)
    return conn


def _persist_database(client: OKX, cfg: Settings, results: list[dict[str, Any]], captured_at: datetime) -> None:
    balance = client.balance()
    usdt = next((x for x in balance.get("details", []) if x.get("ccy") == "USDT"), {})
    positions = [pos for pos in client.request("GET", "/api/v5/account/positions", private=True)["data"] if float(pos.get("pos") or 0)]
    fills = client.request("GET", "/api/v5/trade/fills", {"instType": "SWAP", "limit": "100"}, private=True)["data"]
    with _db() as conn:
        conn.executemany(
            "INSERT INTO okx_scan_runs (scanned_at,inst_id,action,price,rsi,volume_ratio,breakout_high,breakout_low) VALUES (?,?,?,?,?,?,?,?)",
            [(captured_at.isoformat(), x["instId"], x["action"], x.get("price"), x.get("rsi14"), x.get("volume_ratio"), x.get("breakout_high"), x.get("breakout_low")) for x in results],
        )
        conn.execute(
            "INSERT INTO okx_account_snapshots (captured_at,total_equity,usdt_available,usdt_frozen,open_upl) VALUES (?,?,?,?,?)",
            (captured_at.isoformat(), float(balance.get("totalEq") or 0), float(usdt.get("availBal") or 0), float(usdt.get("frozenBal") or 0), sum(float(x.get("upl") or 0) for x in positions)),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO okx_order_fills (order_id,fill_time,inst_id,side,pos_side,fill_size,fill_price,fee,realized_pnl) VALUES (?,?,?,?,?,?,?,?,?)",
            [(x.get("ordId"), x.get("fillTime"), x.get("instId"), x.get("side"), x.get("posSide"), float(x.get("fillSz") or 0), float(x.get("fillPx") or 0), float(x.get("fee") or 0), float(x.get("pnl") or 0)) for x in fills if x.get("ordId")],
        )


def _write_state(cfg: Settings, results: list[dict[str, Any]], scan_at: datetime, client: OKX) -> None:
    previous = _read_state()
    day = scan_at.astimezone(NY).date().isoformat()
    scans = int(previous.get("scan_count_today", 0)) + 1 if previous.get("trade_day") == day else 1
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        event_rows = json.loads(YF_EVENTS_CACHE_PATH.read_text()).get("events", [])
    except (OSError, json.JSONDecodeError):
        event_rows = []
    event_symbols = {
        str(row.get("symbol", "")).upper() for row in event_rows
        if scan_at.astimezone(NY).date().isoformat() in str(row.get("date", ""))
    }
    candidates = []
    unavailable_demo_symbols = []
    for x in results:
        if x.get("action") == "SKIP" or not x.get("price"):
            continue
        long = x.get("ema9", 0) >= x.get("ema21", 0) and x.get("price", 0) >= x.get("vwap20", 0)
        short = x.get("ema9", 0) < x.get("ema21", 0) and x.get("price", 0) < x.get("vwap20", 0)
        if not (long or short) or x.get("volume_ratio", 0) < 1.0:
            continue
        side = "LONG" if long else "SHORT"
        distance = abs(x["price"] - (x["breakout_high"] if long else x["breakout_low"])) / max(x.get("atr14", 1), 1e-9)
        ema_strength = abs(x.get("ema9", 0) - x.get("ema21", 0)) / max(x.get("atr14", 1), 1e-9)
        atr_pct = x.get("atr14", 0) / max(x["price"], 1e-9) * 100
        spread = float(x.get("spread_bps", 9999))
        event_risk = yahoo_symbol(x["instId"]) in event_symbols
        score = 35 + min(24, x["volume_ratio"] * 12) + min(14, ema_strength * 10)
        score += min(12, atr_pct * 8) + max(0, 15 - distance * 10)
        score -= min(20, spread / 2) + (25 if event_risk else 0)
        if spread > cfg.max_spread_bps or event_risk:
            continue
        if not demo_instrument_tradeable(client, x["instId"]):
            unavailable_demo_symbols.append(x["instId"])
            continue
        candidates.append({
            "instId": x["instId"], "side": side, "score": round(max(0, min(100, score)), 1),
            "reason": "10m 趋势与 VWAP 同向，量能/ATR/突破距离/点差通过排序",
            "created_at": scan_at.isoformat(), "expires_at": (scan_at + timedelta(minutes=20)).isoformat(),
            "price": x["price"], "ema9": x["ema9"], "ema21": x["ema21"],
            "vwap20": x["vwap20"], "volume_ratio": x["volume_ratio"], "atr14": x["atr14"],
            "breakout_high": x["breakout_high"], "breakout_low": x["breakout_low"],
            "spread_bps": spread, "event_risk": event_risk,
        })
    candidates = sorted(candidates, key=lambda x: x["score"], reverse=True)[:10]
    STATE_PATH.write_text(json.dumps({
        "trade_day": day,
        "scan_count_today": scans,
        "last_scan_at": scan_at.isoformat(),
        "next_scan_at": next_scan_boundary(scan_at).isoformat(),
        "symbols": list(cfg.symbols),
        "risk_limits": {
            "daily_entry_limit": cfg.max_entries_per_day,
            "max_open_positions": cfg.max_open_positions,
            "cooldown_minutes": cfg.cooldown_minutes,
            "daily_loss_usdt": cfg.max_daily_loss_usdt,
            "leverage": cfg.leverage,
            "max_dynamic_leverage": cfg.max_dynamic_leverage,
            "risk_budget_cap_usdt": cfg.quote_risk_usdt,
            "risk_fraction": cfg.risk_fraction,
            "max_notional_usdt": cfg.max_notional_usdt,
            "max_gross_notional_usdt": cfg.max_gross_notional_usdt,
            "max_spread_bps": cfg.max_spread_bps,
            "max_slippage_bps": cfg.max_slippage_bps,
            "session": cfg.session,
        },
        "candidates": candidates,
        "unavailable_demo_symbols": sorted(set(unavailable_demo_symbols)),
        "last_results": results,
    }, ensure_ascii=False, indent=2))


def backtest(cfg: Settings, inst_id: str, limit: int = 300) -> dict[str, Any]:
    """Closed-candle, next-open simulation; intended for comparison, not performance claims."""
    rows = list(reversed(OKX(cfg).candles(inst_id, limit)))
    rows = [row for row in rows if row[8] == "1"]
    trades: list[dict[str, Any]] = []
    index, cooldown_until = 40, 0
    while index < len(rows) - 2:
        action, metrics = signal_rows(rows[:index + 1])
        if action == "WAIT" or metrics["confidence"] < 60 or index < cooldown_until:
            index += 1
            continue
        entry = float(rows[index + 1][1])  # next candle open: avoids using a close before it exists
        unit_risk = max(metrics["atr14"] * 1.2, entry * 0.002)
        stop = entry - unit_risk if action == "LONG" else entry + unit_risk
        take = entry + unit_risk * 1.8 if action == "LONG" else entry - unit_risk * 1.8
        exit_price, exit_reason, exit_index = float(rows[min(index + 12, len(rows) - 1)][4]), "time", min(index + 12, len(rows) - 1)
        for future in range(index + 1, min(index + 13, len(rows))):
            high, low = float(rows[future][2]), float(rows[future][3])
            # Conservative ordering when a candle crosses both stop and target.
            if (action == "LONG" and low <= stop) or (action == "SHORT" and high >= stop):
                exit_price, exit_reason, exit_index = stop, "stop", future
                break
            if (action == "LONG" and high >= take) or (action == "SHORT" and low <= take):
                exit_price, exit_reason, exit_index = take, "target", future
                break
        pnl_r = (exit_price - entry) / unit_risk if action == "LONG" else (entry - exit_price) / unit_risk
        trades.append({"side": action, "entry": entry, "exit": exit_price, "reason": exit_reason, "r": round(pnl_r, 3), "confidence": metrics["confidence"]})
        cooldown_until = exit_index + 6
        index = exit_index + 1
    wins = sum(1 for trade in trades if trade["r"] > 0)
    return {
        "instId": inst_id, "bars": len(rows), "trades": len(trades),
        "win_rate": round(wins / len(trades) * 100, 2) if trades else 0,
        "net_r": round(sum(trade["r"] for trade in trades), 3),
        "avg_r": round(sum(trade["r"] for trade in trades) / len(trades), 3) if trades else 0,
        "details": trades,
        "note": "5m closed-candle simulation; excludes fees, funding, spread and slippage.",
    }


def dashboard_snapshot() -> dict[str, Any]:
    """Current dashboard data. Credentials stay local; no secret is returned."""
    cfg, client, state = settings(), None, _read_state()
    try:
        client = OKX(cfg)
        balance = client.balance()
        usdt = next((x for x in balance.get("details", []) if x.get("ccy") == "USDT"), {})
        positions = []
        portfolio = client.request("GET", "/api/v5/account/positions", private=True)["data"]
        for pos in portfolio:
            quantity = abs(float(pos.get("pos") or 0))
            if not quantity:
                continue
            inst_id, mark, avg = pos.get("instId", ""), float(pos.get("markPx") or 0), float(pos.get("avgPx") or 0)
            ct_val = float(client.instrument(inst_id).get("ctVal") or 0)
            atr14 = atr(list(reversed(client.candles(inst_id))))
            long = pos.get("posSide") == "long"
            risk = atr14 * 1.2
            positions.append({
                "inst_id": inst_id, "side": pos.get("posSide"), "size": pos.get("pos"),
                "avg_px": pos.get("avgPx"), "mark_px": pos.get("markPx"), "leverage": pos.get("lever"),
                "upl": float(pos.get("upl") or 0), "upl_ratio": float(pos.get("uplRatio") or 0),
                "margin": float(pos.get("imr") or 0), "liq_px": pos.get("liqPx"),
                "opened_at": pos.get("cTime"),
                "entry_notional": quantity * ct_val * avg,
                "entry_margin_estimate": quantity * ct_val * avg / max(float(pos.get("lever") or 1), 1),
                "notional": quantity * ct_val * mark,
                "stop_reference": avg - risk if long else avg + risk,
                "target_reference": avg + risk * 1.8 if long else avg - risk * 1.8,
                "reference_note": "ATR 风险参考，尚未在交易所挂出止损单",
            })
        fills = client.request("GET", "/api/v5/trade/fills", {"instType": "SWAP", "limit": "100"}, private=True)["data"]
        today = datetime.now(NY).date()
        today_fills = [x for x in fills if datetime.fromtimestamp(int(x.get("fillTime", "0")) / 1000, UTC).astimezone(NY).date() == today]
        # `/trade/fills` reports `fillPnl`, not the legacy `pnl` field.  For
        # the dashboard's realised performance use closed-position history:
        # `realizedPnl` is net of trading fee and funding and is one record per
        # completed position rather than one record per partial fill.
        position_history = client.request(
            "GET", "/api/v5/account/positions-history", {"instType": "SWAP", "limit": "100"}, private=True
        )["data"]
        closed_today = [x for x in position_history if datetime.fromtimestamp(
            int(x.get("uTime", "0")) / 1000, UTC
        ).astimezone(NY).date() == today]
        realized = sum(float(x.get("realizedPnl") or 0) for x in closed_today)
        candles = list(reversed(client.candles(cfg.symbols[0])))
        price_chart = [{"ts": x[0], "close": float(x[4])} for x in candles if x[8] == "1"][-60:]
        with _db() as conn:
            equity_rows = conn.execute("SELECT captured_at,total_equity,open_upl FROM okx_account_snapshots ORDER BY id DESC LIMIT 60").fetchall()
            scan_rows = conn.execute("SELECT scanned_at,action,price FROM okx_scan_runs ORDER BY id DESC LIMIT 60").fetchall()
            try:
                micro_row = conn.execute(
                    "SELECT COUNT(*), COUNT(DISTINCT minute_ts), MAX(updated_at) FROM okx_microstructure_minute"
                ).fetchone()
            except sqlite3.OperationalError:
                micro_row = (0, 0, None)
        event_data = yfinance_events(cfg.symbols, [x["inst_id"] for x in positions])
        try: ws = json.loads((ROOT / "data" / "okx_candidate_ws.json").read_text())
        except Exception: ws = {}
        try: execution = json.loads(EXECUTION_STATE_PATH.read_text())
        except Exception: execution = {}
        try:
            execution["micro_executor"] = json.loads(
                (ROOT / "data" / "okx_micro_execution_state.json").read_text()
            )
        except (OSError, json.JSONDecodeError):
            execution["micro_executor"] = {"orders_enabled": False, "gate_reason": "执行器尚未启动"}
        try: algos = client.pending_algos()
        except Exception: algos = []
        protection_by_inst = {}
        for algo in algos:
            protection_by_inst.setdefault(algo.get("instId"), []).append(algo)
        for position in positions:
            protection = protection_by_inst.get(position["inst_id"], [])
            position["protection_orders"] = [{
                "algo_id": row.get("algoId"), "state": row.get("state"),
                "stop": row.get("slTriggerPx"), "target": row.get("tpTriggerPx"),
            } for row in protection]
            if protection:
                position["reference_note"] = "交易所附加止盈止损已生效"
        research = {"microstructure_rows": int(micro_row[0]), "microstructure_minutes": int(micro_row[1]),
                    "microstructure_updated_at": micro_row[2], "deployment_passed": False, "studies": []}
        try:
            micro_runtime = json.loads((ROOT / "data" / "okx_microstructure.json").read_text())
            research["demo_tradeable_count"] = int(micro_runtime.get("demo_tradeable_count") or 0)
            research["demo_tradeable_symbols"] = micro_runtime.get("demo_tradeable_symbols") or []
            fees = micro_runtime.get("taker_fee_bps") or {}
            research["fee_snapshot_at"] = micro_runtime.get("fee_snapshot_at")
            research["fee_covered_tradeable_count"] = sum(
                float(fees.get(symbol) or 0) > 0 for symbol in research["demo_tradeable_symbols"]
            )
            research["taker_fee_bps_values"] = sorted({
                float(fees.get(symbol) or 0) for symbol in research["demo_tradeable_symbols"]
                if float(fees.get(symbol) or 0) > 0
            })
            research["micro_model_eligible_count"] = sum(
                symbol not in {"BTC-USDT-SWAP", "SAMSUNG-USDT-SWAP"}
                for symbol in research["demo_tradeable_symbols"]
            )
        except (OSError, json.JSONDecodeError):
            research["demo_tradeable_count"], research["demo_tradeable_symbols"] = 0, []
            research["micro_model_eligible_count"] = 0
            research["fee_covered_tradeable_count"], research["taker_fee_bps_values"] = 0, []
        try:
            research["shadow_learning"] = json.loads((ROOT / "data" / "okx_shadow_learning.json").read_text())
        except (OSError, json.JSONDecodeError):
            research["shadow_learning"] = {"pending": 0, "all": {"samples": 0}}
        try:
            research["return_shadow"] = json.loads((ROOT / "data" / "okx_return_shadow_state.json").read_text())
        except (OSError, json.JSONDecodeError):
            research["return_shadow"] = {"mode": "not_started", "signals": 0}
        try:
            research["micro_forward"] = json.loads((ROOT / "data" / "okx_microstructure_forward.json").read_text())
        except (OSError, json.JSONDecodeError):
            research["micro_forward"] = {"mode": "not_started", "results": {}}
        try:
            research["micro_model"] = json.loads((ROOT / "data" / "okx_microstructure_model_state.json").read_text())
        except (OSError, json.JSONDecodeError):
            research["micro_model"] = {"research_phase": "not_started", "forward": {"samples": 0}}
        research["micro_executor"] = execution["micro_executor"]
        try:
            research["gap_shadow"] = json.loads((ROOT / "data" / "okx_gap_shadow_state.json").read_text())
        except (OSError, json.JSONDecodeError):
            research["gap_shadow"] = {"mode": "not_started", "signals": 0}
        research["deployment_passed"] = bool(
            research["shadow_learning"].get("passed")
            and research["shadow_learning"].get("execution_ready")
        )
        for name, path in (
            ("90日 ML/横截面", ROOT / "data" / "okx_walkforward_90d.json"),
            ("90日 ORB", ROOT / "data" / "okx_orb_walkforward.json"),
            ("2年小时级状态", ROOT / "data" / "yfinance_regime_walkforward.json"),
            ("部署代码等价回放", ROOT / "data" / "okx_runtime_parity_90d.json"),
            ("回踩/假突破结构", ROOT / "data" / "okx_structural_research_90d.json"),
            ("代币-正股基差", ROOT / "data" / "okx_basis_research_60d.json"),
            ("滚动收益预测模型", ROOT / "data" / "okx_return_model_90d.json"),
            ("执行等价路径模型", ROOT / "data" / "okx_barrier_model_90d.json"),
            ("相对跳空回补", ROOT / "data" / "okx_gap_research_90d.json"),
        ):
            try:
                report = json.loads(path.read_text())
                holdout = report.get("holdout") or report.get("final_diagnostic") or {}
                research["studies"].append({
                    "name": name, "passed": bool(report.get("passed")),
                    "trades": holdout.get("trades"), "profit_factor": holdout.get("profit_factor"),
                    "expectancy_r": holdout.get("expectancy_r", holdout.get("avg_r")),
                })
            except (OSError, json.JSONDecodeError):
                continue
        return {
            "ok": True, "generated_at": datetime.now(UTC).isoformat(), "state": state,
            "funds": {"total_equity": float(balance.get("totalEq") or 0), "usdt_available": float(usdt.get("availBal") or 0), "usdt_frozen": float(usdt.get("frozenBal") or 0), "usdt_balance": float(usdt.get("cashBal") or 0)},
            "positions": positions,
            "today": {
                "fills": len({x.get("ordId") for x in today_fills if x.get("ordId")}),
                "closed_positions": len(closed_today), "realized_pnl": realized,
                "open_upl": sum(x["upl"] for x in positions),
            },
            "events": event_data, "research": research,
            "ws": ws, "execution": execution, "kill_switch": kill_switch(), "monitor_control": monitor_control(),
            "charts": {"price": price_chart, "equity": [dict(row) for row in reversed(equity_rows)], "scans": [dict(row) for row in reversed(scan_rows)]},
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "state": state}


def run_once(cfg: Settings, execute: bool) -> list[dict[str, Any]]:
    client, results = OKX(cfg), []
    portfolio = client.request("GET", "/api/v5/account/positions", private=True)["data"]
    active_positions = [p for p in portfolio if float(p.get("pos") or 0)]
    fills = client.request("GET", "/api/v5/trade/fills", {"instType": "SWAP", "limit": "100"}, private=True)["data"]
    now = datetime.now(UTC)
    today = now.astimezone(NY).date()
    today_fills = [x for x in fills if datetime.fromtimestamp(int(x.get("fillTime", "0")) / 1000, UTC).astimezone(NY).date() == today]
    entry_order_ids = {x.get("ordId") for x in today_fills if x.get("ordId")}
    daily_pnl = sum(float(x.get("pnl") or 0) for x in today_fills) + sum(float(x.get("upl") or 0) for x in active_positions)
    last_fill_at: dict[str, datetime] = {}
    for fill in today_fills:
        inst_id, stamp = fill.get("instId"), fill.get("fillTime")
        if inst_id and stamp:
            filled_at = datetime.fromtimestamp(int(stamp) / 1000, UTC)
            if filled_at > last_fill_at.get(inst_id, datetime.min.replace(tzinfo=UTC)):
                last_fill_at[inst_id] = filled_at
    for inst_id in cfg.symbols:
        try:
            ten_minute_rows = aggregate_closed_candles(client.candles(inst_id, limit=100, bar="5m"), 10)
            action, metrics = signal_rows(ten_minute_rows, breakout_period=6)
        except RuntimeError as exc:
            LOG.warning("skip %s: %s", inst_id, exc)
            results.append({"instId": inst_id, "action": "SKIP", "reason": str(exc), "executed": False})
            continue
        outcome: dict[str, Any] = {"instId": inst_id, "action": action, **metrics, "executed": False}
        try:
            outcome["spread_bps"] = spread_bps(client.ticker(inst_id))
        except RuntimeError:
            outcome["spread_bps"] = 9999.0
        active = [p for p in active_positions if p.get("instId") == inst_id]
        if action != "WAIT" and active:
            outcome["reason"] = "existing position; no pyramiding"
        elif action != "WAIT" and daily_pnl <= -cfg.max_daily_loss_usdt:
            outcome["reason"] = f"daily loss circuit breaker active (${cfg.max_daily_loss_usdt:.2f})"
        elif action != "WAIT" and cfg.max_entries_per_day > 0 and len(entry_order_ids) >= cfg.max_entries_per_day:
            outcome["reason"] = f"daily entry limit reached ({cfg.max_entries_per_day})"
        elif action != "WAIT" and len(active_positions) >= cfg.max_open_positions:
            outcome["reason"] = f"max concurrent positions reached ({cfg.max_open_positions})"
        elif action != "WAIT" and inst_id in last_fill_at and now - last_fill_at[inst_id] < timedelta(minutes=cfg.cooldown_minutes):
            outcome["reason"] = f"cooldown active ({cfg.cooldown_minutes} minutes)"
        elif action != "WAIT" and execute:
            outcome["reason"] = "已进入 10m 候选池，等待 5m 复核和 1m 盘口确认"
        results.append(outcome)
    fill_lines = [
        f"- {x['instId']} {x['action']} | 状态 **{x.get('order_state')}** | 成交 {x.get('fill_size')} 张 @ {_num(x.get('fill_price'))} | 手续费 ${_num(x.get('fee'), 6)}"
        for x in results if x.get("executed") and x.get("order_state") == "filled" and float(x.get("fill_size") or 0) > 0
    ]
    if fill_lines:
        holdings = [line for inst_id in cfg.symbols for line in position_lines(client, inst_id)]
        message = f"📈 **OKX 模拟盘成交**\n\n{today_pnl_line(client)}\n\n**剩余资金**\n{account_line(client)}"
        message += "\n\n✅ **本轮成交**\n" + "\n".join(fill_lines)
        message += "\n\n**当前仓位**\n" + ("\n".join(holdings) if holdings else "- 无持仓")
        notify(cfg, message)
    captured_at = datetime.now(UTC)
    _write_state(cfg, results, captured_at, client)
    try:
        _persist_database(client, cfg, results, captured_at)
    except Exception:
        # A dashboard-history write must never stop the market scanner.
        LOG.exception("dashboard persistence failed; scanner will continue")
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="stage signals for the demo WebSocket executor")
    parser.add_argument("--loop", action="store_true", help="scan every 10 minutes during US cash session")
    parser.add_argument("--force", action="store_true", help="run outside US cash session")
    parser.add_argument("--backtest", action="store_true", help="run a closed-candle, next-open strategy simulation and exit")
    parser.add_argument("--backtest-symbol", default="MRVL-USDT-SWAP", help="instrument used with --backtest")
    parser.add_argument("--backtest-bars", type=int, default=300, help="5m bars used with --backtest (40-300)")
    args = parser.parse_args()
    cfg = settings()
    if args.execute and os.getenv("OKX_INTRADAY_EXECUTE_DEMO") != "1":
        raise SystemExit("set OKX_INTRADAY_EXECUTE_DEMO=1 before enabling demo orders")
    if args.backtest:
        print(json.dumps(backtest(cfg, args.backtest_symbol.upper(), args.backtest_bars), ensure_ascii=False, indent=2))
        return
    while True:
        if args.force or cfg.session == "24x7" or in_us_cash_session():
            try:
                LOG.info("%s", json.dumps(run_once(cfg, args.execute), ensure_ascii=False))
            except Exception:
                LOG.exception("scan failed; retrying at the next interval")
        else:
            LOG.info("outside US cash session; skipped")
        if not args.loop:
            return
        delay = max(1.0, (next_scan_boundary(datetime.now(UTC)) - datetime.now(UTC)).total_seconds())
        time.sleep(delay)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("OKX_INTRADAY_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    main()
