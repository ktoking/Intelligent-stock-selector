#!/usr/bin/env python3
"""Bounded, explicitly opted-in OKX Demo executor for the gap shadow rule.

This is an exploratory Demo experiment, not a promoted strategy.  It consumes
only the 150-minute baseline candidate produced by ``okx_gap_shadow.py`` and
fails closed: no protection means an immediate reduce-only exit.
"""
from __future__ import annotations

import json
import logging
import math
import os
import signal
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_intraday_agent import (  # noqa: E402
    DB_PATH, OKX, decimal_step, kill_switch, notify, settings, today_pnl_line,
)
from scripts.okx_candidate_ws import build_book_tca_snapshot  # noqa: E402
from scripts.okx_microstructure_executor import account_equity, position_notional, risk_size  # noqa: E402

UTC = timezone.utc
STATE_PATH = ROOT / "data" / "okx_gap_demo_execution_state.json"
SHADOW_PATH = ROOT / "data" / "okx_gap_shadow_state.json"
EXPERIMENT_ID = "gap_relative_fade_demo_e150m_stop75_v1"
STOP_BPS = 75.0
MIN_DYNAMIC_STOP_BPS = 75.0
MAX_DYNAMIC_STOP_BPS = 300.0
ATR_STOP_MULTIPLE = 1.2
LEVERAGE = 3
MAX_POSITIONS = 5
MAX_SIGNAL_AGE_SECONDS = 150
LOG = logging.getLogger("okx-gap-demo-executor")


@contextmanager
def db_connection():
    """Use a short-lived SQLite connection and close it deterministically."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def candidate_stop_bps(candidate: dict[str, Any], default: float = STOP_BPS) -> float:
    """Use a bounded ATR stop supplied by the shadow signal when available."""
    atr_bps = float(candidate.get("atr_bps") or 0.0)
    if atr_bps <= 0:
        return float(default)
    return max(MIN_DYNAMIC_STOP_BPS, min(MAX_DYNAMIC_STOP_BPS, atr_bps * ATR_STOP_MULTIPLE))


def execution_candidates(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Prefer the separately ranked Demo lane; preserve old state compatibility."""
    if "demo_candidates" in state:
        return list(state.get("demo_candidates") or [])
    return list(state.get("candidates") or [])


def enabled() -> bool:
    return (
        os.getenv("OKX_GAP_EXPLORATORY_DEMO") == "1"
        and settings().profile == "demo"
        and not kill_switch().get("enabled")
    )


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def realized_slippage_bps(order_side: str, decision_price: float, fill_price: float) -> float | None:
    """Return signed implementation shortfall; positive is adverse execution."""
    if decision_price <= 0 or fill_price <= 0:
        return None
    direction = 1 if str(order_side).lower() in {"buy", "long"} else -1
    return (fill_price / decision_price - 1) * 10_000 * direction


def order_fill_summary(client: Any, inst_id: str, order_id: str,
                       detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read exchange fills without allowing a TCA outage to affect protection."""
    error = None
    try:
        rows = client.request("GET", "/api/v5/trade/fills", {
            "instType": "SWAP", "instId": inst_id, "ordId": order_id, "limit": "100",
        }, private=True).get("data") or []
        matches = [row for row in rows if str(row.get("ordId") or "") == str(order_id)]
        if matches:
            currencies = sorted({str(row.get("feeCcy") or "") for row in matches if row.get("feeCcy")})
            if len(currencies) > 1:
                return {
                    "fee": None, "fee_ccy": "MIXED", "fee_status": "mixed_fee_currency",
                    "fee_error": "multiple fee currencies require conversion before aggregation",
                    "pnl": sum(float(row.get("pnl") or 0) for row in matches),
                }
            return {
                "fee": sum(float(row.get("fee") or 0) for row in matches),
                "fee_ccy": currencies[0] if len(currencies) == 1 else "MIXED" if currencies else None,
                "fee_status": "fills_endpoint",
                "fee_error": None,
                "pnl": sum(float(row.get("pnl") or 0) for row in matches),
            }
        error = "fills endpoint returned no matching rows"
    except Exception as exc:
        error = str(exc)
    detail = detail or {}
    if detail.get("fee") not in (None, ""):
        return {
            "fee": float(detail["fee"]), "fee_ccy": detail.get("feeCcy") or None,
            "fee_status": "order_detail_fallback", "fee_error": error,
            "pnl": float(detail.get("pnl") or 0),
        }
    return {
        "fee": None, "fee_ccy": None, "fee_status": "snapshot_unavailable",
        "fee_error": str(error or "exchange fill fee unavailable")[:500], "pnl": None,
    }


def _book_json(snapshot: dict[str, Any]) -> str | None:
    levels = {"bids": snapshot.get("bids") or [], "asks": snapshot.get("asks") or []}
    return json.dumps(levels, ensure_ascii=False, separators=(",", ":")) if any(levels.values()) else None


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


def ensure_schema() -> None:
    with db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS okx_gap_demo_executions (
                signal_key TEXT PRIMARY KEY, inst_id TEXT NOT NULL, side TEXT NOT NULL,
                entry_ts INTEGER NOT NULL, due_ts INTEGER NOT NULL, status TEXT NOT NULL,
                client_order_id TEXT NOT NULL, order_id TEXT, fill_size REAL, fill_price REAL,
                stop_price REAL, algo_id TEXT, close_order_id TEXT, exit_price REAL,
                realized_pnl REAL, close_reason TEXT, error TEXT,
                decision_at TEXT, decision_price REAL, decision_bid_px REAL, decision_ask_px REAL,
                decision_spread_bps REAL, decision_book_exchange_ts INTEGER,
                decision_mark_px REAL, decision_mark_exchange_ts INTEGER,
                decision_mark_snapshot_status TEXT, decision_mark_snapshot_error TEXT,
                decision_books5_json TEXT, decision_book_snapshot_status TEXT,
                decision_book_snapshot_error TEXT,
                decision_bid_depth_contracts REAL, decision_ask_depth_contracts REAL,
                decision_bid_depth_notional_usdt REAL, decision_ask_depth_notional_usdt REAL,
                decision_ct_val REAL, estimated_slippage_bps REAL, decision_tca_model_version TEXT,
                realized_slippage_bps REAL,
                entry_fee REAL, entry_fee_ccy TEXT, entry_fee_status TEXT, entry_fee_error TEXT,
                exit_decision_price REAL, exit_fill_slippage_bps REAL,
                close_fee REAL, close_fee_ccy TEXT, close_fee_status TEXT, close_fee_error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
        """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(okx_gap_demo_executions)")}
        for column, declaration in (
            ("decision_at", "TEXT"), ("decision_price", "REAL"),
            ("decision_bid_px", "REAL"), ("decision_ask_px", "REAL"),
            ("decision_spread_bps", "REAL"), ("decision_book_exchange_ts", "INTEGER"),
            ("decision_mark_px", "REAL"), ("decision_mark_exchange_ts", "INTEGER"),
            ("decision_mark_snapshot_status", "TEXT"), ("decision_mark_snapshot_error", "TEXT"),
            ("decision_books5_json", "TEXT"), ("decision_book_snapshot_status", "TEXT"),
            ("decision_book_snapshot_error", "TEXT"),
            ("decision_bid_depth_contracts", "REAL"), ("decision_ask_depth_contracts", "REAL"),
            ("decision_bid_depth_notional_usdt", "REAL"),
            ("decision_ask_depth_notional_usdt", "REAL"), ("decision_ct_val", "REAL"),
            ("estimated_slippage_bps", "REAL"), ("decision_tca_model_version", "TEXT"),
            ("realized_slippage_bps", "REAL"),
            ("entry_fee", "REAL"), ("entry_fee_ccy", "TEXT"),
            ("entry_fee_status", "TEXT"), ("entry_fee_error", "TEXT"),
            ("exit_decision_price", "REAL"), ("exit_fill_slippage_bps", "REAL"),
            ("close_fee", "REAL"), ("close_fee_ccy", "TEXT"),
            ("close_fee_status", "TEXT"), ("close_fee_error", "TEXT"),
        ):
            if column not in columns:
                conn.execute(f"ALTER TABLE okx_gap_demo_executions ADD COLUMN {column} {declaration}")


class Executor:
    def __init__(self) -> None:
        ensure_schema()
        self.cfg = settings()
        self.client = OKX(self.cfg)
        self.running = True

    def stop(self) -> None:
        self.running = False

    @staticmethod
    def _size(position: dict[str, Any]) -> float:
        return abs(float(position.get("pos") or 0))

    def _open_rows(self) -> list[sqlite3.Row]:
        with db_connection() as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute("SELECT * FROM okx_gap_demo_executions WHERE status='OPEN'").fetchall()

    def _update(self, key: str, **fields: Any) -> None:
        fields["updated_at"] = now_iso()
        values = list(fields.values()) + [key]
        with db_connection() as conn:
            conn.execute(f"UPDATE okx_gap_demo_executions SET {','.join(f'{name}=?' for name in fields)} WHERE signal_key=?", values)

    def _write_state(self, **extra: Any) -> None:
        try:
            with db_connection() as conn:
                rows = conn.execute("SELECT status,COUNT(*) FROM okx_gap_demo_executions GROUP BY status").fetchall()
            counts = {status: count for status, count in rows}
        except sqlite3.Error:
            counts = {}
        atomic_json(STATE_PATH, {"updated_at": now_iso(), "experiment_id": EXPERIMENT_ID,
                                 "enabled": enabled(), "mode": "exploratory_demo_only",
                                 "stop_bps": STOP_BPS, "horizon_minutes": 150, "leverage": LEVERAGE,
                                 "counts": counts, **extra})

    def _positions(self) -> list[dict[str, Any]]:
        return [item for item in self.client.request("GET", "/api/v5/account/positions", private=True)["data"]
                if abs(float(item.get("pos") or 0)) > 0]

    def _close(self, row: sqlite3.Row, position: dict[str, Any], reason: str) -> None:
        inst_id, pos_side = row["inst_id"], position.get("posSide")
        instrument = self.client.instrument(inst_id)
        size = decimal_step(self._size(position), instrument.get("lotSz") or "1", "nearest")
        side = "sell" if pos_side == "long" else "buy"
        exit_decision_price = None
        try:
            ticker = self.client.ticker(inst_id)
            exit_decision_price = float(ticker.get("bidPx") if side == "sell" else ticker.get("askPx") or 0)
        except Exception:
            LOG.exception("cannot capture exit decision quote for %s", inst_id)
        try:
            accepted = self.client.place_market(inst_id, side, size, pos_side, reduce_only=True,
                                                client_order_id=f"gd{int(time.time() * 1000) % 10**15}")
            order_id = str(accepted.get("ordId") or "")
            detail: dict[str, Any] = {}
            for _ in range(10):
                time.sleep(.35)
                detail = self.client.order(inst_id, order_id)
                if detail.get("state") in {"filled", "canceled"}:
                    break
            if detail.get("state") != "filled" or float(detail.get("accFillSz") or 0) <= 0:
                raise RuntimeError(f"reduceOnly 未成交：{detail.get('state') or 'unknown'}")
            exit_price = float(detail.get("avgPx") or detail.get("fillPx") or 0)
            fill_summary = order_fill_summary(self.client, inst_id, order_id, detail)
            self._update(row["signal_key"], status="CLOSED", close_order_id=order_id,
                         exit_price=exit_price, exit_decision_price=exit_decision_price,
                         exit_fill_slippage_bps=(realized_slippage_bps(side, exit_decision_price, exit_price)
                                                if exit_decision_price else None),
                         close_fee=fill_summary["fee"], close_fee_ccy=fill_summary["fee_ccy"],
                         close_fee_status=fill_summary["fee_status"], close_fee_error=fill_summary["fee_error"],
                         close_reason=reason)
            notify(self.cfg, f"📤 **OKX 跳空策略模拟盘平仓｜{inst_id}**\n\n"
                             f"方向：平{'多' if pos_side == 'long' else '空'}｜市价 ${float(detail.get('avgPx') or detail.get('fillPx') or 0):,.4f}\n"
                             f"原因：{reason}\n\n{today_pnl_line(self.client)}")
        except Exception as exc:
            self._update(row["signal_key"], error=f"平仓失败，下一轮重试：{exc}")
            LOG.exception("gap demo close failed for %s", inst_id)

    def _submit(self, candidate: dict[str, Any], entry_ts: int) -> None:
        # The supervisor also removes this worker when the kill switch is on,
        # but keep an in-process guard for the interval before termination and
        # for direct/manual invocations of this script.
        if not enabled():
            return
        inst_id, direction = candidate["inst_id"], candidate["side"]
        key = f"{EXPERIMENT_ID}:{inst_id}:{direction}:{entry_ts}"
        with db_connection() as conn:
            if conn.execute("SELECT 1 FROM okx_gap_demo_executions WHERE signal_key=?", (key,)).fetchone():
                return
        if not candidate.get("liquidity_qualified"):
            return
        positions = self._positions()
        if len(positions) >= MAX_POSITIONS:
            return
        ticker = self.client.ticker(inst_id)
        bid, ask = float(ticker.get("bidPx") or 0), float(ticker.get("askPx") or 0)
        mid = (bid + ask) / 2
        spread = (ask - bid) / mid * 10_000 if bid > 0 and ask > bid else math.inf
        if not math.isfinite(spread) or spread > 5:
            return
        pos_side = "long" if direction == "LONG" else "short"
        side, quote = ("buy", ask) if direction == "LONG" else ("sell", bid)
        instrument = self.client.instrument(inst_id)
        stop_bps = candidate_stop_bps(candidate)
        # Private max-size verifies that the Demo account accepts this equity token.
        try:
            self.client.max_size(inst_id)
        except RuntimeError as exc:
            if "51001" in str(exc):
                return
            raise
        book = (self.client.request("GET", "/api/v5/market/books", {"instId": inst_id, "sz": "5"})["data"] or [{}])[0]
        balance = self.client.balance()
        sizing = risk_size(account_equity(balance), sum(position_notional(item) for item in positions), quote, instrument, stop_bps)
        decision_book = build_book_tca_snapshot(
            book, direction, sizing["ct_val"], reference_notional_usdt=None,
            reference_contracts=sizing["contracts_float"],
        )
        slippage = decision_book.get("estimated_slippage_bps")
        if slippage is None:
            return
        if not math.isfinite(slippage) or slippage > 5:
            return
        client_id = f"gd{entry_ts % 10**12}{len(positions)}"[-16:]
        due_ts = entry_ts + 150 * 60_000
        decision_at = now_iso()
        with db_connection() as conn:
            conn.execute("""INSERT INTO okx_gap_demo_executions
                (signal_key,inst_id,side,entry_ts,due_ts,status,client_order_id,
                 decision_at,decision_price,decision_bid_px,decision_ask_px,decision_spread_bps,
                 decision_book_exchange_ts,decision_books5_json,decision_book_snapshot_status,
                 decision_mark_px,decision_mark_exchange_ts,decision_mark_snapshot_status,
                 decision_mark_snapshot_error,
                 decision_book_snapshot_error,decision_bid_depth_contracts,decision_ask_depth_contracts,
                 decision_bid_depth_notional_usdt,decision_ask_depth_notional_usdt,decision_ct_val,
                 estimated_slippage_bps,decision_tca_model_version,created_at,updated_at)
                VALUES (?,?,?,?,?,'SUBMITTING',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    key, inst_id, direction, entry_ts, due_ts, client_id,
                    decision_at, quote, decision_book.get("bid_px"), decision_book.get("ask_px"),
                    decision_book.get("spread_bps"), decision_book.get("exchange_ts"),
                    _book_json(decision_book), decision_book.get("book_snapshot_status"),
                    candidate.get("decision_mark_px"), candidate.get("decision_mark_exchange_ts"),
                    candidate.get("mark_snapshot_status") or "not_captured",
                    candidate.get("mark_snapshot_error"),
                    decision_book.get("book_snapshot_error"), decision_book.get("bid_depth_contracts"),
                    decision_book.get("ask_depth_contracts"), decision_book.get("bid_depth_notional_usdt"),
                    decision_book.get("ask_depth_notional_usdt"), decision_book.get("ct_val"),
                    slippage, decision_book.get("tca_model_version"), decision_at, decision_at,
                ))
        try:
            if not enabled():
                self._update(key, status="SKIPPED", error="紧急熔断已开启，取消新入场")
                return
            self.client.set_leverage(inst_id, pos_side, LEVERAGE)
            accepted = self.client.place_market(inst_id, side, sizing["contracts"], pos_side, client_order_id=client_id)
            order_id = str(accepted.get("ordId") or "")
            if accepted.get("sCode") not in (None, "", "0"):
                raise RuntimeError(f"市价单被拒绝：{accepted.get('sCode')} {accepted.get('sMsg')}")
            if not order_id:
                recovered = self.client.order_by_client_id(inst_id, client_id)
                if not recovered:
                    raise RuntimeError("市价单响应缺少 ordId，且未能按 clOrdId 回查")
                order_id = str(recovered.get("ordId") or "")
            detail: dict[str, Any] = {}
            for _ in range(10):
                time.sleep(.35)
                detail = self.client.order(inst_id, order_id)
                if detail.get("state") in {"filled", "canceled"}:
                    break
            fill, size = float(detail.get("avgPx") or detail.get("fillPx") or quote), float(detail.get("accFillSz") or 0)
            if detail.get("state") != "filled" or size <= 0:
                raise RuntimeError(f"市价单未成交：{detail.get('state') or 'unknown'}")
            entry_fill = order_fill_summary(self.client, inst_id, order_id, detail)
            self._update(
                key, order_id=order_id, fill_size=size, fill_price=fill,
                realized_slippage_bps=realized_slippage_bps(side, quote, fill),
                entry_fee=entry_fill["fee"], entry_fee_ccy=entry_fill["fee_ccy"],
                entry_fee_status=entry_fill["fee_status"], entry_fee_error=entry_fill["fee_error"],
            )
            sign = 1 if direction == "LONG" else -1
            stop = decimal_step(fill - sign * fill * stop_bps / 10_000, instrument.get("tickSz") or ".0001", "nearest")
            protection = self.client.place_stop_protection(inst_id, pos_side, decimal_step(size, instrument.get("lotSz") or "1"), stop)
            if protection.get("sCode") not in (None, "", "0"):
                raise RuntimeError(f"保护单失败：{protection.get('sCode')} {protection.get('sMsg')}")
            self._update(key, status="OPEN", stop_price=float(stop),
                         algo_id=str(protection.get("algoId") or ""))
            notify(self.cfg, f"📈 **OKX 跳空策略模拟成交｜{inst_id}**\n\n"
                             f"方向：{'做多' if direction == 'LONG' else '做空'}｜市价 ${fill:,.4f}\n"
                             f"成交名义金额：${sizing['notional']:,.2f}｜杠杆 {LEVERAGE}x｜风险预算 ${sizing['risk_budget']:,.2f}\n"
                             f"保护：ATR 动态止损 {stop_bps:.1f}bp / ${float(stop):,.4f}｜最晚 150 分钟市价退出\n"
                             f"依据：相对 SPY 跳空 ≥100bp，开盘 5 分钟/前日相对走势出现回补确认\n\n{today_pnl_line(self.client)}")
        except Exception as exc:
            self._update(key, status="FAILED", error=str(exc))
            # A timeout can occur after exchange acceptance.  Check the
            # idempotency key before treating it as an ordinary failure.
            try:
                recovered = self.client.order_by_client_id(inst_id, client_id)
                if recovered and float(recovered.get("accFillSz") or 0) > 0:
                    recovered_order_id = str(recovered.get("ordId") or "")
                    recovered_fill_price = float(recovered.get("avgPx") or recovered.get("fillPx") or quote)
                    entry_fill = order_fill_summary(self.client, inst_id, recovered_order_id, recovered)
                    self._update(key, order_id=recovered_order_id,
                                 fill_size=float(recovered.get("accFillSz") or 0),
                                 fill_price=recovered_fill_price,
                                 realized_slippage_bps=realized_slippage_bps(side, quote, recovered_fill_price),
                                 entry_fee=entry_fill["fee"], entry_fee_ccy=entry_fill["fee_ccy"],
                                 entry_fee_status=entry_fill["fee_status"], entry_fee_error=entry_fill["fee_error"],
                                 error=f"订单回查到成交，但保护流程异常：{exc}")
            except Exception:
                LOG.exception("gap demo order recovery lookup failed for %s", inst_id)
            position = next((item for item in self._positions() if item.get("instId") == inst_id and item.get("posSide") == pos_side), None)
            if position:
                with db_connection() as conn:
                    conn.row_factory = sqlite3.Row
                    row = conn.execute("SELECT * FROM okx_gap_demo_executions WHERE signal_key=?", (key,)).fetchone()
                if row:
                    self._close(row, position, "开仓后保护失败，reduceOnly 兜底")
            LOG.exception("gap demo submit failed for %s", inst_id)

    def process_entries(self) -> None:
        if not enabled():
            return
        try:
            state = json.loads(SHADOW_PATH.read_text())
            entry_ts = int(state.get("entry_ts") or 0)
            age = time.time() - entry_ts / 1000
            if state.get("experiment_id") != "gap_relative_fade_confirm_e0936_quote_h150_t100_cost14_v3" or not 0 <= age <= MAX_SIGNAL_AGE_SECONDS:
                return
            for candidate in execution_candidates(state):
                self._submit(candidate, entry_ts)
        except Exception:
            LOG.exception("gap demo entry evaluation failed")

    def manage_open(self) -> None:
        positions = self._positions()
        now_ms = int(time.time() * 1000)
        for row in self._open_rows():
            pos_side = "long" if row["side"] == "LONG" else "short"
            position = next((item for item in positions if item.get("instId") == row["inst_id"] and item.get("posSide") == pos_side), None)
            if not position:
                self._update(row["signal_key"], status="CLOSED", close_reason="交易所止损或外部平仓")
                continue
            algos = self.client.pending_algos(row["inst_id"])
            if not any(str(item.get("algoId") or "") == str(row["algo_id"] or "") for item in algos):
                self._close(row, position, "保护单缺失，reduceOnly 兜底")
            elif now_ms >= int(row["due_ts"]):
                self.client.cancel_algos([{"instId": row["inst_id"], "algoId": row["algo_id"]}])
                self._close(row, position, "达到 150 分钟固定持有期")

    def run(self) -> None:
        while self.running:
            try:
                self.manage_open()
                self.process_entries()
                self._write_state()
            except Exception:
                LOG.exception("gap demo executor loop failed")
            for _ in range(10):
                if not self.running:
                    break
                time.sleep(1)


def main() -> None:
    executor = Executor()
    signal.signal(signal.SIGTERM, lambda *_: executor.stop())
    signal.signal(signal.SIGINT, lambda *_: executor.stop())
    executor.run()


if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")
    main()
