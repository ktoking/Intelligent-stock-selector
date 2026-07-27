#!/usr/bin/env python3
"""Collect minute-level OKX order-book and aggressive-flow features for shadow training."""
from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import websocket

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_intraday_agent import (  # noqa: E402
    DB_PATH, DEFAULT_SCAN_SYMBOLS, OKX, demo_instrument_tradeable, settings,
)
from scripts.okx_research_universe import load_symbols  # noqa: E402

UTC = timezone.utc
STATE_PATH = ROOT / "data" / "okx_microstructure.json"
LOG = logging.getLogger("okx-microstructure")


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class Collector:
    def __init__(self) -> None:
        self.cfg = settings()
        self.execution_symbols = self.cfg.symbols or tuple(item.strip() for item in DEFAULT_SCAN_SYMBOLS.split(","))
        observe_research = os.getenv("OKX_RESEARCH_OBSERVE_UNIVERSE", "1") == "1"
        research_symbols = load_symbols("forward_observation") if observe_research else ()
        # This is a market-data-only expansion.  demo_tradeable is persisted as
        # metadata, but the collector has no order method or execution path.
        self.symbols = list(dict.fromkeys((*self.execution_symbols, *research_symbols)))
        self.running = True
        self.lock = threading.Lock()
        self.minute = int(time.time() // 60 * 60)
        self.flow: dict[str, dict[str, float]] = {}
        self.books: dict[str, dict[str, float]] = {}
        self.last_message_at = ""
        self.parent_pid = os.getppid()
        self.ws: websocket.WebSocketApp | None = None
        self.connected_at = 0.0
        self.contract_values = self._contract_values()
        self.demo_tradeable = self._demo_tradeability()
        self.taker_fee_bps = self._taker_fee_bps()
        self.fee_snapshot_at = time.time()
        self._init_db()

    def _contract_values(self) -> dict[str, float]:
        try:
            rows = OKX(self.cfg).request("GET", "/api/v5/public/instruments", {"instType": "SWAP"})["data"]
            values = {row.get("instId"): _float(row.get("ctVal")) for row in rows}
            return {symbol: values.get(symbol, 0.0) for symbol in self.symbols}
        except Exception:
            LOG.exception("cannot load contract values; executable depth rows will remain disabled")
            return {symbol: 0.0 for symbol in self.symbols}

    def _demo_tradeability(self) -> dict[str, bool]:
        client = OKX(self.cfg)
        # Expanded research symbols must not cause private account fan-out.
        # They are public-feed observations only, never execution candidates.
        result = {symbol: False for symbol in self.symbols}
        for symbol in self.execution_symbols:
            try:
                result[symbol] = demo_instrument_tradeable(client, symbol)
            except Exception:
                LOG.exception("cannot verify Demo private-trading support for %s", symbol)
                result[symbol] = False
        return result

    def _taker_fee_bps(self) -> dict[str, float]:
        """Snapshot the Demo account's current SWAP taker fee for each instrument."""
        result = {symbol: 0.0 for symbol in self.symbols}
        try:
            client = OKX(self.cfg)
            instruments = client.request(
                "GET", "/api/v5/account/instruments", {"instType": "SWAP"}, private=True
            ).get("data") or []
            groups = {
                row.get("instId"): str(row.get("groupId") or "")
                for row in instruments if row.get("instId")
            }
            payload = client.request(
                "GET", "/api/v5/account/trade-fee", {"instType": "SWAP"}, private=True,
            ).get("data") or []
            root = payload[0] if payload else {}
            generic_fee_bps = abs(_float(root.get("taker"))) * 10_000
            fee_by_group = {
                str(row.get("groupId") or ""): abs(_float(row.get("taker"))) * 10_000
                for row in (root.get("feeGroup") or []) if row.get("groupId")
            }
            for symbol in self.symbols:
                if self.demo_tradeable.get(symbol):
                    result[symbol] = fee_by_group.get(groups.get(symbol, ""), generic_fee_bps)
        except Exception:
            LOG.exception("cannot snapshot Demo taker fees; affected rows will remain disabled")
        return result

    def _init_db(self) -> None:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS okx_microstructure_minute (
                    minute_ts INTEGER NOT NULL,
                    inst_id TEXT NOT NULL,
                    bid_px REAL, ask_px REAL, spread_bps REAL,
                    min_bid_px REAL, max_bid_px REAL, min_ask_px REAL, max_ask_px REAL,
                    bid_depth REAL, ask_depth REAL, ct_val REAL,
                    demo_tradeable INTEGER, taker_fee_bps REAL,
                    book_age_seconds REAL, capture_complete INTEGER,
                    book_imbalance REAL,
                    microprice REAL, buy_volume REAL, sell_volume REAL,
                    trade_count INTEGER, aggressive_imbalance REAL,
                    order_flow_imbalance REAL, depth_normalized_ofi REAL,
                    quote_updates INTEGER,
                    forward_5m_bps REAL, forward_15m_bps REAL, forward_60m_bps REAL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (minute_ts, inst_id)
                )
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(okx_microstructure_minute)")}
            for column in ("forward_5m_bps", "forward_15m_bps", "forward_60m_bps"):
                if column not in columns:
                    conn.execute(f"ALTER TABLE okx_microstructure_minute ADD COLUMN {column} REAL")
            for column, declaration in (
                ("ct_val", "REAL"), ("book_age_seconds", "REAL"), ("capture_complete", "INTEGER"),
                ("demo_tradeable", "INTEGER"),
                ("taker_fee_bps", "REAL"),
                ("min_bid_px", "REAL"), ("max_bid_px", "REAL"),
                ("min_ask_px", "REAL"), ("max_ask_px", "REAL"),
                ("order_flow_imbalance", "REAL"), ("depth_normalized_ofi", "REAL"),
                ("quote_updates", "INTEGER"),
            ):
                if column not in columns:
                    conn.execute(f"ALTER TABLE okx_microstructure_minute ADD COLUMN {column} {declaration}")
            for horizon, column in ((5, "forward_5m_bps"), (15, "forward_15m_bps"), (60, "forward_60m_bps")):
                conn.execute(f"""
                    UPDATE okx_microstructure_minute AS old
                    SET {column}=(
                        SELECT (((future.bid_px+future.ask_px)/2.0) /
                                ((old.bid_px+old.ask_px)/2.0) - 1.0) * 10000
                        FROM okx_microstructure_minute AS future
                        WHERE future.inst_id=old.inst_id
                          AND future.minute_ts=old.minute_ts+?
                          AND future.bid_px>0 AND future.ask_px>0
                    )
                    WHERE old.{column} IS NULL AND old.bid_px>0 AND old.ask_px>0
                      AND EXISTS (
                        SELECT 1 FROM okx_microstructure_minute AS future
                        WHERE future.inst_id=old.inst_id
                          AND future.minute_ts=old.minute_ts+?
                          AND future.bid_px>0 AND future.ask_px>0
                      )
                """, (horizon * 60, horizon * 60))

    def stop(self) -> None:
        self.running = False
        if self.ws is not None:
            self.ws.close()

    def _book(self, data: dict[str, Any]) -> dict[str, float]:
        bids, asks = data.get("bids") or [], data.get("asks") or []
        bid_px = _float(bids[0][0]) if bids else 0.0
        ask_px = _float(asks[0][0]) if asks else 0.0
        bid_depth = sum(_float(row[1]) for row in bids[:5])
        ask_depth = sum(_float(row[1]) for row in asks[:5])
        total = bid_depth + ask_depth
        imbalance = (bid_depth - ask_depth) / total if total else 0.0
        mid = (bid_px + ask_px) / 2 if bid_px and ask_px else bid_px or ask_px
        spread = (ask_px - bid_px) / mid * 10_000 if mid else 0.0
        best_bid_size = _float(bids[0][1]) if bids else 0.0
        best_ask_size = _float(asks[0][1]) if asks else 0.0
        best_total = best_bid_size + best_ask_size
        microprice = (
            (ask_px * best_bid_size + bid_px * best_ask_size) / best_total
            if best_total else mid
        )
        return {
            "bid_px": bid_px, "ask_px": ask_px, "spread_bps": spread,
            "bid_depth": bid_depth, "ask_depth": ask_depth,
            "best_bid_size": best_bid_size, "best_ask_size": best_ask_size,
            "book_imbalance": imbalance, "microprice": microprice,
        }

    @staticmethod
    def _ofi(previous: dict[str, float], current: dict[str, float]) -> float:
        """Cont-Kukanov-Stoikov best-quote order-flow imbalance event."""
        if not previous.get("bid_px") or not previous.get("ask_px"):
            return 0.0
        if current["bid_px"] > previous["bid_px"]:
            bid_event = current["best_bid_size"]
        elif current["bid_px"] == previous["bid_px"]:
            bid_event = current["best_bid_size"] - previous.get("best_bid_size", 0.0)
        else:
            bid_event = -previous.get("best_bid_size", 0.0)
        if current["ask_px"] < previous["ask_px"]:
            ask_event = current["best_ask_size"]
        elif current["ask_px"] == previous["ask_px"]:
            ask_event = current["best_ask_size"] - previous.get("best_ask_size", 0.0)
        else:
            ask_event = -previous.get("best_ask_size", 0.0)
        return bid_event - ask_event

    def on_message(self, _ws: websocket.WebSocketApp, message: str) -> None:
        try:
            packet = json.loads(message)
            if packet.get("event") == "error":
                LOG.warning("subscription rejected: %s %s", packet.get("code"), packet.get("msg"))
                return
            arg, rows = packet.get("arg") or {}, packet.get("data") or []
            inst_id, channel = arg.get("instId"), arg.get("channel")
            if not inst_id or not rows:
                return
            with self.lock:
                self.last_message_at = datetime.now(UTC).isoformat()
                if channel == "books5":
                    book = self._book(rows[0])
                    previous = self.books.get(inst_id) or {}
                    flow = self.flow.setdefault(inst_id, {
                        "buy_volume": 0.0, "sell_volume": 0.0, "trade_count": 0.0,
                        "order_flow_imbalance": 0.0, "top_depth_sum": 0.0, "quote_updates": 0.0,
                    })
                    if previous:
                        flow["order_flow_imbalance"] += self._ofi(previous, book)
                        flow["top_depth_sum"] += book["best_bid_size"] + book["best_ask_size"]
                        flow["quote_updates"] += 1
                    for key, value, choose in (
                        ("min_bid_px", book["bid_px"], min),
                        ("max_bid_px", book["bid_px"], max),
                        ("min_ask_px", book["ask_px"], min),
                        ("max_ask_px", book["ask_px"], max),
                    ):
                        prior = _float(flow.get(key))
                        if value > 0:
                            flow[key] = choose(prior, value) if prior > 0 else value
                    self.books[inst_id] = {**book, "_received_at": time.time()}
                elif channel == "trades":
                    flow = self.flow.setdefault(inst_id, {
                        "buy_volume": 0.0, "sell_volume": 0.0, "trade_count": 0.0,
                        "order_flow_imbalance": 0.0, "top_depth_sum": 0.0, "quote_updates": 0.0,
                    })
                    for row in rows:
                        side = "buy_volume" if row.get("side") == "buy" else "sell_volume"
                        flow[side] += _float(row.get("sz"))
                        flow["trade_count"] += 1
        except Exception:
            LOG.exception("microstructure message failed")

    def flush(self) -> None:
        current = int(time.time() // 60 * 60)
        with self.lock:
            if current == self.minute:
                return
            minute, books, flow = self.minute, dict(self.books), dict(self.flow)
            self.minute = current
            self.flow = {}
        if time.time() - getattr(self, "fee_snapshot_at", time.time()) >= 3600:
            refreshed = self._taker_fee_bps()
            missing = [symbol for symbol in self.symbols
                       if self.demo_tradeable.get(symbol) and refreshed.get(symbol, 0) <= 0]
            if not missing:
                self.taker_fee_bps = refreshed
                self.fee_snapshot_at = time.time()
            else:
                LOG.error("fee refresh incomplete for %s; retaining previous snapshot", missing)
                self.fee_snapshot_at = time.time() - 3540  # retry after roughly one minute
        now = datetime.now(UTC).isoformat()
        rows = []
        snapshot = {}
        for inst_id in self.symbols:
            book = books.get(inst_id, {})
            trades = flow.get(inst_id, {})
            buy, sell = _float(trades.get("buy_volume")), _float(trades.get("sell_volume"))
            aggressive = (buy - sell) / (buy + sell) if buy + sell else 0.0
            quote_updates = int(trades.get("quote_updates") or 0)
            ofi = _float(trades.get("order_flow_imbalance"))
            average_top_depth = _float(trades.get("top_depth_sum")) / quote_updates if quote_updates else 0.0
            normalized_ofi = ofi / average_top_depth if average_top_depth else 0.0
            book_age = max(0.0, time.time() - _float(book.get("_received_at"))) if book.get("_received_at") else 999.0
            row = {
                "minute_ts": minute, "inst_id": inst_id,
                **{key: _float(book.get(key)) for key in (
                    "bid_px", "ask_px", "spread_bps", "bid_depth", "ask_depth",
                    "book_imbalance", "microprice",
                )},
                "ct_val": _float(self.contract_values.get(inst_id)),
                "demo_tradeable": int(bool(self.demo_tradeable.get(inst_id))),
                "taker_fee_bps": _float(self.taker_fee_bps.get(inst_id)),
                "min_bid_px": _float(trades.get("min_bid_px")) or _float(book.get("bid_px")),
                "max_bid_px": _float(trades.get("max_bid_px")) or _float(book.get("bid_px")),
                "min_ask_px": _float(trades.get("min_ask_px")) or _float(book.get("ask_px")),
                "max_ask_px": _float(trades.get("max_ask_px")) or _float(book.get("ask_px")),
                "book_age_seconds": book_age,
                "capture_complete": int(self.connected_at > 0 and self.connected_at <= minute and book_age <= 10),
                "buy_volume": buy, "sell_volume": sell,
                "trade_count": int(trades.get("trade_count") or 0),
                "aggressive_imbalance": aggressive,
                "order_flow_imbalance": ofi, "depth_normalized_ofi": normalized_ofi,
                "quote_updates": quote_updates, "updated_at": now,
            }
            rows.append(row)
            snapshot[inst_id] = row
        with sqlite3.connect(DB_PATH) as conn:
            conn.executemany("""
                INSERT INTO okx_microstructure_minute
                (minute_ts,inst_id,bid_px,ask_px,min_bid_px,max_bid_px,min_ask_px,max_ask_px,spread_bps,bid_depth,ask_depth,
                 ct_val,demo_tradeable,taker_fee_bps,book_age_seconds,capture_complete,book_imbalance,microprice,buy_volume,sell_volume,trade_count,
                 aggressive_imbalance,order_flow_imbalance,depth_normalized_ofi,quote_updates,updated_at)
                VALUES (:minute_ts,:inst_id,:bid_px,:ask_px,:min_bid_px,:max_bid_px,:min_ask_px,:max_ask_px,:spread_bps,:bid_depth,:ask_depth,
                        :ct_val,:demo_tradeable,:taker_fee_bps,:book_age_seconds,:capture_complete,
                        :book_imbalance,:microprice,:buy_volume,:sell_volume,:trade_count,
                        :aggressive_imbalance,:order_flow_imbalance,:depth_normalized_ofi,:quote_updates,:updated_at)
                ON CONFLICT(minute_ts,inst_id) DO UPDATE SET
                  bid_px=excluded.bid_px,ask_px=excluded.ask_px,spread_bps=excluded.spread_bps,
                  min_bid_px=excluded.min_bid_px,max_bid_px=excluded.max_bid_px,
                  min_ask_px=excluded.min_ask_px,max_ask_px=excluded.max_ask_px,
                  bid_depth=excluded.bid_depth,ask_depth=excluded.ask_depth,
                  ct_val=excluded.ct_val,demo_tradeable=excluded.demo_tradeable,taker_fee_bps=excluded.taker_fee_bps,
                  book_age_seconds=excluded.book_age_seconds,
                  capture_complete=excluded.capture_complete,
                  book_imbalance=excluded.book_imbalance,microprice=excluded.microprice,
                  buy_volume=excluded.buy_volume,sell_volume=excluded.sell_volume,
                  trade_count=excluded.trade_count,aggressive_imbalance=excluded.aggressive_imbalance,
                  order_flow_imbalance=excluded.order_flow_imbalance,
                  depth_normalized_ofi=excluded.depth_normalized_ofi,quote_updates=excluded.quote_updates,
                  updated_at=excluded.updated_at
            """, rows)
            # Label the exact feature row only after its horizon has elapsed.
            # This is causal, restart-safe and never uses an unfinished candle.
            for horizon, column in ((5, "forward_5m_bps"), (15, "forward_15m_bps"), (60, "forward_60m_bps")):
                outcomes = []
                for row in rows:
                    mid = (row["bid_px"] + row["ask_px"]) / 2 if row["bid_px"] and row["ask_px"] else 0
                    if mid:
                        outcomes.append((mid, minute - horizon * 60, row["inst_id"]))
                conn.executemany(f"""
                    UPDATE okx_microstructure_minute
                    SET {column}=CASE WHEN bid_px>0 AND ask_px>0
                        THEN (? / ((bid_px+ask_px)/2.0) - 1.0) * 10000 END
                    WHERE minute_ts=? AND inst_id=? AND {column} IS NULL
                """, outcomes)
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps({
            "updated_at": now, "last_message_at": self.last_message_at,
            "minute_ts": minute, "symbols": len(self.symbols),
            "demo_tradeable_symbols": [symbol for symbol in self.symbols if self.demo_tradeable.get(symbol)],
            "demo_tradeable_count": sum(self.demo_tradeable.values()), "data": snapshot,
            "taker_fee_bps": self.taker_fee_bps,
            "fee_snapshot_at": datetime.fromtimestamp(
                getattr(self, "fee_snapshot_at", time.time()), UTC
            ).isoformat(),
        }, ensure_ascii=False, indent=2))

    def periodic(self) -> None:
        while self.running:
            if self.parent_pid != 1 and os.getppid() == 1:
                LOG.warning("runtime parent exited; stopping orphan collector")
                self.stop()
                break
            try:
                self.flush()
            except Exception:
                LOG.exception("microstructure flush failed")
            time.sleep(1)

    def run(self) -> None:
        threading.Thread(target=self.periodic, daemon=True).start()
        while self.running:
            def on_open(ws: websocket.WebSocketApp) -> None:
                self.connected_at = time.time()
                args = [
                    {"channel": channel, "instId": inst_id}
                    for inst_id in self.symbols for channel in ("books5", "trades")
                ]
                ws.send(json.dumps({"op": "subscribe", "args": args}))
                LOG.info("subscribed books5+trades for %s symbols", len(self.symbols))

            proxy = urlparse(self.cfg.proxy_url or "")
            options: dict[str, Any] = {"ping_interval": 20, "ping_timeout": 8}
            if proxy.hostname:
                options.update(http_proxy_host=proxy.hostname, http_proxy_port=proxy.port or 80, proxy_type="http")
            ws = websocket.WebSocketApp(
                "wss://ws.okx.com:8443/ws/v5/public", on_open=on_open,
                on_message=self.on_message,
                on_error=lambda _ws, error: LOG.warning("microstructure WS error: %s", error),
            )
            self.ws = ws
            ws.run_forever(**options)
            self.ws = None
            if self.running:
                time.sleep(3)


def main() -> None:
    collector = Collector()
    signal.signal(signal.SIGTERM, lambda *_: collector.stop())
    signal.signal(signal.SIGINT, lambda *_: collector.stop())
    collector.run()


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("OKX_INTRADAY_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    main()
