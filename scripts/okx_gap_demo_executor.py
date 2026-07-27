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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_intraday_agent import (  # noqa: E402
    DB_PATH, OKX, decimal_step, estimated_slippage_bps, notify, settings, today_pnl_line,
)
from scripts.okx_microstructure_executor import account_equity, position_notional, risk_size  # noqa: E402

UTC = timezone.utc
STATE_PATH = ROOT / "data" / "okx_gap_demo_execution_state.json"
SHADOW_PATH = ROOT / "data" / "okx_gap_shadow_state.json"
EXPERIMENT_ID = "gap_relative_fade_demo_e150m_stop75_v1"
STOP_BPS = 75.0
LEVERAGE = 3
MAX_POSITIONS = 5
MAX_SIGNAL_AGE_SECONDS = 150
LOG = logging.getLogger("okx-gap-demo-executor")


def enabled() -> bool:
    return os.getenv("OKX_GAP_EXPLORATORY_DEMO") == "1" and settings().profile == "demo"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


def ensure_schema() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS okx_gap_demo_executions (
                signal_key TEXT PRIMARY KEY, inst_id TEXT NOT NULL, side TEXT NOT NULL,
                entry_ts INTEGER NOT NULL, due_ts INTEGER NOT NULL, status TEXT NOT NULL,
                client_order_id TEXT NOT NULL, order_id TEXT, fill_size REAL, fill_price REAL,
                stop_price REAL, algo_id TEXT, close_order_id TEXT, exit_price REAL,
                realized_pnl REAL, close_reason TEXT, error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
        """)


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
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute("SELECT * FROM okx_gap_demo_executions WHERE status='OPEN'").fetchall()

    def _update(self, key: str, **fields: Any) -> None:
        fields["updated_at"] = now_iso()
        values = list(fields.values()) + [key]
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(f"UPDATE okx_gap_demo_executions SET {','.join(f'{name}=?' for name in fields)} WHERE signal_key=?", values)

    def _write_state(self, **extra: Any) -> None:
        try:
            with sqlite3.connect(DB_PATH) as conn:
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
            self._update(row["signal_key"], status="CLOSED", close_order_id=order_id,
                         exit_price=float(detail.get("avgPx") or detail.get("fillPx") or 0),
                         close_reason=reason)
            notify(self.cfg, f"📤 **OKX 跳空策略模拟盘平仓｜{inst_id}**\n\n"
                             f"方向：平{'多' if pos_side == 'long' else '空'}｜市价 ${float(detail.get('avgPx') or detail.get('fillPx') or 0):,.4f}\n"
                             f"原因：{reason}\n\n{today_pnl_line(self.client)}")
        except Exception as exc:
            self._update(row["signal_key"], error=f"平仓失败，下一轮重试：{exc}")
            LOG.exception("gap demo close failed for %s", inst_id)

    def _submit(self, candidate: dict[str, Any], entry_ts: int) -> None:
        inst_id, direction = candidate["inst_id"], candidate["side"]
        key = f"{EXPERIMENT_ID}:{inst_id}:{direction}:{entry_ts}"
        with sqlite3.connect(DB_PATH) as conn:
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
        # Private max-size verifies that the Demo account accepts this equity token.
        try:
            self.client.max_size(inst_id)
        except RuntimeError as exc:
            if "51001" in str(exc):
                return
            raise
        book = (self.client.request("GET", "/api/v5/market/books", {"instId": inst_id, "sz": "5"})["data"] or [{}])[0]
        balance = self.client.balance()
        sizing = risk_size(account_equity(balance), sum(position_notional(item) for item in positions), quote, instrument, STOP_BPS)
        slippage = estimated_slippage_bps(book, direction, sizing["contracts_float"], sizing["ct_val"])
        if not math.isfinite(slippage) or slippage > 5:
            return
        client_id = f"gd{entry_ts % 10**12}{len(positions)}"[-16:]
        due_ts = entry_ts + 150 * 60_000
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""INSERT INTO okx_gap_demo_executions
                (signal_key,inst_id,side,entry_ts,due_ts,status,client_order_id,created_at,updated_at)
                VALUES (?,?,?,?,?,'SUBMITTING',?,?,?)""", (key, inst_id, direction, entry_ts, due_ts, client_id, now_iso(), now_iso()))
        try:
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
            sign = 1 if direction == "LONG" else -1
            stop = decimal_step(fill - sign * fill * STOP_BPS / 10_000, instrument.get("tickSz") or ".0001", "nearest")
            protection = self.client.place_stop_protection(inst_id, pos_side, decimal_step(size, instrument.get("lotSz") or "1"), stop)
            if protection.get("sCode") not in (None, "", "0"):
                raise RuntimeError(f"保护单失败：{protection.get('sCode')} {protection.get('sMsg')}")
            self._update(key, status="OPEN", order_id=order_id, fill_size=size, fill_price=fill,
                         stop_price=float(stop), algo_id=str(protection.get("algoId") or ""))
            notify(self.cfg, f"📈 **OKX 跳空策略模拟成交｜{inst_id}**\n\n"
                             f"方向：{'做多' if direction == 'LONG' else '做空'}｜市价 ${fill:,.4f}\n"
                             f"成交名义金额：${sizing['notional']:,.2f}｜杠杆 {LEVERAGE}x｜风险预算 ${sizing['risk_budget']:,.2f}\n"
                             f"保护：硬止损 ${float(stop):,.4f}｜最晚 150 分钟市价退出\n"
                             f"依据：相对 SPY 跳空 ≥100bp，开盘 5 分钟/前日相对走势出现回补确认\n\n{today_pnl_line(self.client)}")
        except Exception as exc:
            self._update(key, status="FAILED", error=str(exc))
            # A timeout can occur after exchange acceptance.  Check the
            # idempotency key before treating it as an ordinary failure.
            try:
                recovered = self.client.order_by_client_id(inst_id, client_id)
                if recovered and float(recovered.get("accFillSz") or 0) > 0:
                    self._update(key, order_id=str(recovered.get("ordId") or ""),
                                 fill_size=float(recovered.get("accFillSz") or 0),
                                 fill_price=float(recovered.get("avgPx") or recovered.get("fillPx") or quote),
                                 error=f"订单回查到成交，但保护流程异常：{exc}")
            except Exception:
                LOG.exception("gap demo order recovery lookup failed for %s", inst_id)
            position = next((item for item in self._positions() if item.get("instId") == inst_id and item.get("posSide") == pos_side), None)
            if position:
                with sqlite3.connect(DB_PATH) as conn:
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
            for candidate in state.get("candidates") or []:
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
