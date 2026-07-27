#!/usr/bin/env python3
"""Demo-only executor for the frozen micro-barrier strategy.

The worker is safe to supervise before promotion: until fresh forward results
pass the research gate and its read-only execution audit matches the frozen
artifact, it cannot submit an order.
"""
from __future__ import annotations

import hashlib
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

import joblib

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_candidate_ws import deployment_gate, read_json  # noqa: E402
from scripts.okx_intraday_agent import (  # noqa: E402
    DB_PATH, OKX, decimal_step, estimated_slippage_bps, kill_switch, notify,
    set_kill_switch, settings, today_pnl_line,
)
from scripts.okx_microstructure_model import (  # noqa: E402
    ARTIFACT_PATH, IMPACT_BUFFER_BPS, STATE_PATH as MODEL_STATE_PATH, WARMUP_DEMO_ENV, artifact_compatible,
)

UTC = timezone.utc
LOG = logging.getLogger("okx-micro-executor")
AUDIT_PATH = ROOT / "data" / "okx_micro_execution_audit.json"
EXECUTOR_STATE_PATH = ROOT / "data" / "okx_micro_execution_state.json"
ADAPTER_VERSION = "micro_demo_executor_fee_guard_v2"
MAX_SIGNAL_AGE_SECONDS = 180
MAX_POSITIONS = 5
LEVERAGE = 3
RISK_FRACTION = .0035
MAX_POSITION_EQUITY_FRACTION = .20
MAX_GROSS_EQUITY_FRACTION = .80


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


def config_signature(artifact: dict[str, Any]) -> str:
    payload = {
        "experiment_id": artifact.get("experiment_id"),
        "features": list(artifact.get("features") or []),
        "config": {key: artifact.get("config", {}).get(key) for key in
                   ("stop_bps", "target_r", "horizon", "model", "threshold")},
        "adapter_version": ADAPTER_VERSION,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def ensure_schema() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS okx_micro_executions (
                signal_key TEXT PRIMARY KEY, experiment_id TEXT NOT NULL,
                inst_id TEXT NOT NULL, side TEXT NOT NULL,
                entry_minute INTEGER NOT NULL, due_minute INTEGER NOT NULL,
                status TEXT NOT NULL, client_order_id TEXT NOT NULL,
                order_id TEXT, fill_size REAL, fill_price REAL,
                stop_price REAL, target_price REAL, algo_id TEXT,
                exit_order_id TEXT, error TEXT,
                exit_price REAL, realized_pnl REAL, exit_reason TEXT, closed_at TEXT,
                planned_quote REAL, decision_drift_bps REAL,
                estimated_slippage_bps REAL, fill_slippage_bps REAL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
        """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(okx_micro_executions)")}
        for column, declaration in (("exit_price", "REAL"), ("realized_pnl", "REAL"),
                                    ("exit_reason", "TEXT"), ("closed_at", "TEXT"),
                                    ("planned_quote", "REAL"), ("decision_drift_bps", "REAL"),
                                    ("estimated_slippage_bps", "REAL"), ("fill_slippage_bps", "REAL")):
            if column not in columns:
                conn.execute(f"ALTER TABLE okx_micro_executions ADD COLUMN {column} {declaration}")


def account_equity(balance: dict[str, Any]) -> float:
    return float(balance.get("totalEq") or balance.get("adjEq") or 0)


def position_notional(position: dict[str, Any]) -> float:
    direct = abs(float(position.get("notionalUsd") or 0))
    if direct:
        return direct
    return abs(float(position.get("pos") or 0) * float(position.get("markPx") or position.get("avgPx") or 0)
               * float(position.get("ctVal") or 1))


def risk_size(equity: float, gross_notional: float, entry: float, instrument: dict[str, Any],
              stop_bps: float) -> dict[str, Any]:
    if equity <= 0 or entry <= 0 or stop_bps <= 0:
        raise RuntimeError("账户权益、入场价或止损距离无效")
    ct_val = float(instrument.get("ctVal") or 0)
    if ct_val <= 0:
        raise RuntimeError("合约 ctVal 缺失")
    risk_budget = equity * RISK_FRACTION
    risk_fraction = stop_bps / 10_000
    gross_room = max(0.0, equity * MAX_GROSS_EQUITY_FRACTION - gross_notional)
    notional = min(risk_budget / risk_fraction, equity * MAX_POSITION_EQUITY_FRACTION, gross_room)
    if notional <= 0:
        raise RuntimeError("组合名义敞口已满")
    lot = str(instrument.get("lotSz") or "1")
    minimum = float(instrument.get("minSz") or lot)
    contracts = float(decimal_step(notional / (entry * ct_val), lot))
    if contracts < minimum:
        contracts = minimum
    actual_notional = contracts * entry * ct_val
    if actual_notional > min(equity * MAX_POSITION_EQUITY_FRACTION, gross_room) * 1.05:
        raise RuntimeError("最小下单量超过仓位或总敞口上限")
    return {
        "contracts": decimal_step(contracts, lot), "contracts_float": contracts,
        "notional": actual_notional, "risk_budget": risk_budget, "ct_val": ct_val,
    }


class Executor:
    def __init__(self, client: OKX | None = None) -> None:
        ensure_schema()
        self.cfg = settings()
        self.client = client or OKX(self.cfg)
        self.running = True

    def stop(self) -> None:
        self.running = False

    def current_taker_fee_bps(self) -> float:
        """Read and briefly cache the Demo account's current generic SWAP taker fee."""
        now = time.time()
        cached_at = float(getattr(self, "_fee_cached_at", 0.0))
        cached = float(getattr(self, "_fee_cached_bps", 0.0))
        if cached > 0 and now - cached_at < 300:
            return cached
        payload = self.client.request(
            "GET", "/api/v5/account/trade-fee", {"instType": "SWAP"}, private=True,
        ).get("data") or []
        fee = abs(float((payload[0] if payload else {}).get("taker") or 0)) * 10_000
        if fee <= 0:
            raise RuntimeError("当前账户 SWAP taker 费率缺失")
        self._fee_cached_bps, self._fee_cached_at = fee, now
        return fee

    def _artifact(self) -> dict[str, Any] | None:
        try:
            return joblib.load(ARTIFACT_PATH)
        except Exception:
            return None

    def warmup_allowed(self, artifact: dict[str, Any] | None = None) -> bool:
        return bool(os.getenv(WARMUP_DEMO_ENV) == "1" and self.cfg.profile == "demo"
                    and artifact and artifact.get("warmup_demo"))

    def audit(self) -> dict[str, Any]:
        """Run a read-only demo-account compatibility audit after research passes."""
        model_state = read_json(MODEL_STATE_PATH, {})
        artifact = self._artifact()
        base = {
            "adapter_version": ADAPTER_VERSION, "updated_at": now_iso(), "passed": False,
            "experiment_id": model_state.get("experiment_id"),
            "reason": "等待微观结构策略通过严格前向验收",
        }
        if self.warmup_allowed(artifact):
            try:
                fee = self.current_taker_fee_bps()
                previous = read_json(AUDIT_PATH, {})
                signature = config_signature(artifact)
                activated_at = (previous.get("activated_at")
                                if previous.get("signature") == signature else now_iso())
                base.update(passed=True, warmup_demo=True, taker_fee_bps=fee,
                            signature=signature, activated_at=activated_at,
                            reason="v13 warmup Demo-only execution audit")
                atomic_json(AUDIT_PATH, base)
                return base
            except Exception as exc:
                base["reason"] = f"v13 warmup 私有接口审计失败：{exc}"
                atomic_json(AUDIT_PATH, base)
                return base
        if not model_state.get("passed") or not artifact:
            atomic_json(AUDIT_PATH, base)
            return base
        if artifact.get("experiment_id") != model_state.get("experiment_id"):
            base["reason"] = "模型状态与冻结 artifact 的 experiment_id 不一致"
            atomic_json(AUDIT_PATH, base)
            return base
        if not artifact_compatible(artifact):
            base["reason"] = "artifact 研究版本、特征或训练数据指纹不匹配"
            atomic_json(AUDIT_PATH, base)
            return base
        required = {"stop_bps", "target_r", "horizon", "model", "threshold"}
        if not required.issubset(artifact.get("config") or {}):
            base["reason"] = "冻结配置不完整"
            atomic_json(AUDIT_PATH, base)
            return base
        try:
            balance = self.client.balance()
            account = self.client.request("GET", "/api/v5/account/config", private=True)["data"][0]
            if account_equity(balance) <= 0:
                raise RuntimeError("模拟账户权益为零")
            if account.get("posMode") != "long_short_mode":
                raise RuntimeError(f"账户持仓模式必须是 long_short_mode，当前为 {account.get('posMode')}")
        except Exception as exc:
            base["reason"] = f"OKX Demo 只读预检失败：{exc}"
            atomic_json(AUDIT_PATH, base)
            return base
        previous = read_json(AUDIT_PATH, {})
        signature = config_signature(artifact)
        activated_at = previous.get("activated_at") if previous.get("signature") == signature else now_iso()
        result = {**base, "passed": True, "reason": "冻结配置、Demo 鉴权、账户模式与权益预检通过",
                  "experiment_id": artifact["experiment_id"], "signature": signature,
                  "activated_at": activated_at, "config": artifact["config"]}
        atomic_json(AUDIT_PATH, result)
        return result

    def _positions(self) -> list[dict[str, Any]]:
        return [row for row in self.client.request("GET", "/api/v5/account/positions", private=True)["data"]
                if abs(float(row.get("pos") or 0)) > 0]

    def _order_with_recovery(self, inst_id: str, side: str, size: str, pos_side: str,
                             client_order_id: str, reduce_only: bool = False) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                result = self.client.place_market(inst_id, side, size, pos_side,
                                                  reduce_only=reduce_only, client_order_id=client_order_id)
                if result.get("sCode") not in (None, "", "0"):
                    raise RuntimeError(f"{result.get('sCode')} {result.get('sMsg')}")
                return result
            except Exception as exc:
                last_error = exc
                for _ in range(3):
                    time.sleep(.35)
                    try:
                        recovered = self.client.order_by_client_id(inst_id, client_order_id)
                    except Exception:
                        recovered = None
                    if recovered:
                        return recovered
                if attempt < 2:
                    time.sleep(attempt + 1)
        raise RuntimeError(f"市价单三次失败且无法按 clOrdId 回查：{last_error}")

    def _filled_order(self, inst_id: str, order_id: str) -> dict[str, Any]:
        detail: dict[str, Any] = {}
        for _ in range(10):
            time.sleep(.3)
            detail = self.client.order(inst_id, order_id)
            if detail.get("state") in {"filled", "canceled"}:
                break
        if detail.get("state") == "partially_filled":
            self.client.cancel_order(inst_id, order_id)
            detail = self.client.order(inst_id, order_id)
        if float(detail.get("accFillSz") or 0) <= 0:
            raise RuntimeError(f"订单无成交，状态 {detail.get('state')}")
        return detail

    def _update(self, signal_key: str, **values: Any) -> None:
        values["updated_at"] = now_iso()
        clause = ",".join(f"{key}=?" for key in values)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(f"UPDATE okx_micro_executions SET {clause} WHERE signal_key=?",
                         (*values.values(), signal_key))

    def _emergency_close(self, signal_key: str, inst_id: str, pos_side: str, size: str,
                         reason: str) -> None:
        side = "sell" if pos_side == "long" else "buy"
        close_id = "mx" + hashlib.sha256((signal_key + ":emergency").encode()).hexdigest()[:28]
        try:
            order = self._order_with_recovery(inst_id, side, size, pos_side, close_id, reduce_only=True)
            detail = self._filled_order(inst_id, str(order["ordId"]))
            self._update(signal_key, status="EMERGENCY_EXIT", exit_order_id=order.get("ordId"), error=reason,
                         exit_price=float(detail.get("avgPx") or detail.get("fillPx") or 0),
                         exit_reason=reason, closed_at=now_iso())
            notify(self.cfg, f"📤 **OKX 微观结构模拟平仓｜{inst_id}**\n\n原因：{reason}\n{today_pnl_line(self.client)}")
        except Exception as exc:
            self._update(signal_key, status="CRITICAL", error=f"{reason}；紧急平仓也失败：{exc}")
            raise

    def execute_signal(self, row: sqlite3.Row, artifact: dict[str, Any]) -> None:
        signal_key, inst_id, direction = row["signal_key"], row["inst_id"], row["side"]
        pos_side, side = (("long", "buy") if direction == "LONG" else ("short", "sell"))
        client_id = "me" + hashlib.sha256(signal_key.encode()).hexdigest()[:28]
        created = now_iso()
        with sqlite3.connect(DB_PATH) as conn:
            before = conn.total_changes
            conn.execute("""INSERT OR IGNORE INTO okx_micro_executions
                (signal_key,experiment_id,inst_id,side,entry_minute,due_minute,status,client_order_id,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (signal_key, row["experiment_id"], inst_id, direction, row["entry_minute"], row["due_minute"],
                 "SUBMITTING", client_id, created, created))
            if conn.total_changes == before:
                return
        positions = self._positions()
        if len(positions) >= MAX_POSITIONS or any(item.get("instId") == inst_id for item in positions):
            self._update(signal_key, status="SKIPPED", error="持仓上限或同标的仓位冲突")
            return
        try:
            instrument = self.client.instrument(inst_id)
            ticker = self.client.ticker(inst_id)
            bid, ask = float(ticker.get("bidPx") or 0), float(ticker.get("askPx") or 0)
            mid = (bid + ask) / 2
            if bid <= 0 or ask <= bid or not mid or (ask - bid) / mid * 10_000 > 5:
                self._update(signal_key, status="SKIPPED", error="执行时点差超过 5 bps 或报价无效")
                return
            quote = ask if direction == "LONG" else bid
            frozen_cost_bps = float(row["cost_bps"] or 0)
            current_cost_bps = 2 * self.current_taker_fee_bps() + IMPACT_BUFFER_BPS
            if frozen_cost_bps <= 0 or current_cost_bps > frozen_cost_bps + 1e-9:
                self._update(
                    signal_key, status="SKIPPED", planned_quote=quote,
                    error=(f"当前账户交易成本 {current_cost_bps:.2f} bps 高于模型冻结成本 "
                           f"{frozen_cost_bps:.2f} bps"),
                )
                return
            shadow_entry = float(row["entry_price"] or 0)
            drift_bps = abs(quote / shadow_entry - 1) * 10_000 if shadow_entry > 0 else math.inf
            if drift_bps > 5:
                self._update(signal_key, status="SKIPPED", planned_quote=quote,
                             decision_drift_bps=drift_bps,
                             error=f"实际报价相对影子入场漂移 {drift_bps:.2f} bps，超过 5 bps")
                return
            balance = self.client.balance()
            sizing = risk_size(account_equity(balance), sum(position_notional(x) for x in positions), quote,
                               instrument, float(artifact["config"]["stop_bps"]))
            book_rows = self.client.request("GET", "/api/v5/market/books", {
                "instId": inst_id, "sz": "5",
            })["data"]
            book = book_rows[0] if book_rows else {}
            slippage = estimated_slippage_bps(
                book, direction, sizing["contracts_float"], sizing["ct_val"],
            )
            if not math.isfinite(slippage) or slippage > 5:
                self._update(signal_key, status="SKIPPED", planned_quote=quote,
                             decision_drift_bps=drift_bps, estimated_slippage_bps=slippage if math.isfinite(slippage) else None,
                             error="五档预估滑点超过 5 bps 或深度不足")
                return
            self._update(signal_key, planned_quote=quote, decision_drift_bps=drift_bps,
                         estimated_slippage_bps=slippage)
            self.client.set_leverage(inst_id, pos_side, LEVERAGE)
            accepted = self._order_with_recovery(inst_id, side, sizing["contracts"], pos_side, client_id)
            detail = self._filled_order(inst_id, str(accepted["ordId"]))
            fill = float(detail.get("avgPx") or detail.get("fillPx") or quote)
            fill_size = float(detail.get("accFillSz") or 0)
            fill_slippage = abs(fill / quote - 1) * 10_000 if quote else math.inf
            risk = fill * float(artifact["config"]["stop_bps"]) / 10_000
            sign = 1 if direction == "LONG" else -1
            stop = decimal_step(fill - sign * risk, instrument.get("tickSz") or ".0001", "nearest")
            target = decimal_step(fill + sign * risk * float(artifact["config"]["target_r"]),
                                  instrument.get("tickSz") or ".0001", "nearest")
            self._update(signal_key, status="FILLED_UNPROTECTED", order_id=accepted["ordId"],
                fill_size=fill_size, fill_price=fill, stop_price=float(stop), target_price=float(target))
            self._update(signal_key, fill_slippage_bps=fill_slippage)
            try:
                protection = self.client.place_oco_protection(inst_id, pos_side,
                                                              decimal_step(fill_size, instrument.get("lotSz") or "1"),
                                                              stop, target)
                if protection.get("sCode") not in (None, "", "0"):
                    raise RuntimeError(f"{protection.get('sCode')} {protection.get('sMsg')}")
            except Exception as exc:
                self._emergency_close(signal_key, inst_id, pos_side,
                                      decimal_step(fill_size, instrument.get("lotSz") or "1"),
                                      f"保护单挂单失败：{exc}")
                return
            self._update(signal_key, status="OPEN", algo_id=protection.get("algoId"))
            notify(self.cfg, (
                f"📈 **OKX 微观结构模拟成交｜{inst_id}**\n\n"
                f"方向：{'做多' if direction == 'LONG' else '做空'}｜市价 ${fill:,.4f}\n"
                f"仓位：{fill_size:g} 张｜名义金额 ${sizing['notional']:,.2f}｜杠杆 {LEVERAGE}x\n"
                f"风险预算：${sizing['risk_budget']:,.2f}｜止损 ${float(stop):,.4f}｜止盈 ${float(target):,.4f}\n"
                f"模型预测：{float(row['predicted_r']):.3f}R｜最长持仓 {artifact['config']['horizon']} 分钟"
            ))
        except Exception as exc:
            self._update(signal_key, status="FAILED", error=str(exc))
            LOG.exception("micro demo execution failed for %s", signal_key)

    def manage_open(self) -> None:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            trades = conn.execute("SELECT * FROM okx_micro_executions WHERE status='OPEN'").fetchall()
        if not trades:
            return
        positions = self._positions()
        now_minute = int(time.time() // 60 * 60)
        for trade in trades:
            pos_side = "long" if trade["side"] == "LONG" else "short"
            position = next((row for row in positions if row.get("instId") == trade["inst_id"]
                             and row.get("posSide") == pos_side and abs(float(row.get("pos") or 0)) > 0), None)
            if not position:
                fill = self._latest_close_fill(trade)
                exit_price = float((fill or {}).get("fillPx") or 0)
                realized = float((fill or {}).get("pnl") or 0) + float((fill or {}).get("fee") or 0)
                reason = "交易所止损/止盈或外部 reduceOnly 已完成"
                self._update(trade["signal_key"], status="CLOSED", exit_price=exit_price,
                             realized_pnl=realized, exit_reason=reason, closed_at=now_iso())
                notify(self.cfg, (
                    f"📤 **OKX 微观结构模拟平仓｜{trade['inst_id']}**\n\n"
                    f"方向：{'平多' if trade['side']=='LONG' else '平空'}｜成交 ${exit_price:,.4f}\n"
                    f"本笔已实现（含该成交手续费）：{'🔴 +' if realized >= 0 else '🟢 -'}${abs(realized):,.4f}\n"
                    f"原因：{reason}\n{today_pnl_line(self.client)}"
                ))
                continue
            pending = [row for row in self.client.pending_algos(trade["inst_id"])
                       if row.get("posSide") == pos_side]
            if not pending:
                instrument = self.client.instrument(trade["inst_id"])
                try:
                    protection = self.client.place_oco_protection(
                        trade["inst_id"], pos_side,
                        decimal_step(abs(float(position["pos"])), instrument.get("lotSz") or "1"),
                        str(trade["stop_price"]), str(trade["target_price"]),
                    )
                    self._update(trade["signal_key"], algo_id=protection.get("algoId"))
                except Exception as exc:
                    self._emergency_close(trade["signal_key"], trade["inst_id"], pos_side,
                                          decimal_step(abs(float(position["pos"])), instrument.get("lotSz") or "1"),
                                          f"持仓保护单缺失且补挂失败：{exc}")
                    continue
            if now_minute >= int(trade["due_minute"]):
                try:
                    self.client.cancel_algos([{"instId": item["instId"], "algoId": item["algoId"]}
                                              for item in pending if item.get("algoId")])
                except Exception:
                    LOG.exception("failed to cancel protection before time exit")
                instrument = self.client.instrument(trade["inst_id"])
                self._emergency_close(trade["signal_key"], trade["inst_id"], pos_side,
                                      decimal_step(abs(float(position["pos"])), instrument.get("lotSz") or "1"),
                                      "达到冻结策略的最长持仓时间")

    def reconcile_incomplete(self) -> None:
        """Fail closed after crashes between market acceptance and protection."""
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            trades = conn.execute("""SELECT * FROM okx_micro_executions
                WHERE status IN ('SUBMITTING','FILLED_UNPROTECTED','FAILED','CRITICAL')""").fetchall()
        if not trades:
            return
        positions = self._positions()
        for trade in trades:
            pos_side = "long" if trade["side"] == "LONG" else "short"
            position = next((row for row in positions if row.get("instId") == trade["inst_id"]
                             and row.get("posSide") == pos_side and abs(float(row.get("pos") or 0)) > 0), None)
            detail = None
            try:
                if trade["order_id"]:
                    detail = self.client.order(trade["inst_id"], trade["order_id"])
                else:
                    detail = self.client.order_by_client_id(trade["inst_id"], trade["client_order_id"])
            except Exception:
                detail = None
            filled = float((detail or {}).get("accFillSz") or trade["fill_size"] or 0) > 0
            if not filled and not (trade["status"] in {"FILLED_UNPROTECTED", "CRITICAL"} and position):
                try:
                    age = datetime.now(UTC).timestamp() - datetime.fromisoformat(
                        trade["created_at"].replace("Z", "+00:00")
                    ).timestamp()
                except (TypeError, ValueError):
                    age = 999
                if trade["status"] == "SUBMITTING" and age > 60:
                    self._update(trade["signal_key"], status="FAILED",
                                 error="重启回查未发现交易所已接受或成交的订单")
                continue
            if not position:
                self._update(trade["signal_key"], status="CLOSED", closed_at=now_iso(),
                             exit_reason="重启回查时仓位已不存在")
                continue
            instrument = self.client.instrument(trade["inst_id"])
            self._emergency_close(
                trade["signal_key"], trade["inst_id"], pos_side,
                decimal_step(abs(float(position["pos"])), instrument.get("lotSz") or "1"),
                f"执行器重启回查发现 {trade['status']}，保护状态不确定",
            )

    def _latest_close_fill(self, trade: sqlite3.Row) -> dict[str, Any] | None:
        """Find the latest closing fill after this strategy entry for reporting."""
        try:
            rows = self.client.request("GET", "/api/v5/trade/fills", {
                "instType": "SWAP", "instId": trade["inst_id"], "limit": "100",
            }, private=True)["data"]
            entry_ms = int(datetime.fromisoformat(trade["created_at"].replace("Z", "+00:00")).timestamp() * 1000)
            closing_side = "sell" if trade["side"] == "LONG" else "buy"
            matches = [row for row in rows if row.get("side") == closing_side
                       and int(row.get("fillTime") or 0) >= entry_ms]
            if not matches:
                return None
            total_size = sum(float(row.get("fillSz") or 0) for row in matches)
            weighted_price = (sum(float(row.get("fillPx") or 0) * float(row.get("fillSz") or 0)
                                  for row in matches) / total_size if total_size else 0)
            return {
                "fillPx": weighted_price,
                "pnl": sum(float(row.get("pnl") or 0) for row in matches),
                "fee": sum(float(row.get("fee") or 0) for row in matches),
            }
        except Exception:
            LOG.exception("cannot identify closing fill for %s", trade["signal_key"])
            return None

    def once(self) -> dict[str, Any]:
        audit = self.audit()
        # Existing exposure is reconciled and managed regardless of whether
        # the research gate currently permits *new* entries.
        self.reconcile_incomplete()
        self.manage_open()
        artifact = self._artifact()
        warmup = self.warmup_allowed(artifact)
        allowed, reason = deployment_gate({"micro_barrier_executable"})
        if warmup:
            allowed, reason = True, "v13 warmup 模拟盘试运行：不计入严格前向验收"
        status = {"updated_at": now_iso(), "pid": __import__("os").getpid(), "audit": audit,
                  "gate_allowed": allowed, "gate_reason": reason, "orders_enabled": False}
        if not allowed:
            atomic_json(EXECUTOR_STATE_PATH, status)
            return status
        switch = kill_switch()
        if switch["enabled"] and switch["reason"].startswith("sealed validation failed:"):
            switch = set_kill_switch(False, "micro-barrier fresh forward gate and execution audit passed")
        if switch["enabled"]:
            status["gate_reason"] = f"kill switch 已开启：{switch['reason']}"
            atomic_json(EXECUTOR_STATE_PATH, status)
            return status
        if not artifact or config_signature(artifact) != audit.get("signature"):
            status["gate_reason"] = "artifact 与执行审计签名不一致"
            atomic_json(EXECUTOR_STATE_PATH, status)
            return status
        status["orders_enabled"] = True
        activated = int(datetime.fromisoformat(audit["activated_at"].replace("Z", "+00:00")).timestamp())
        # The strict path must ignore signals that predate the audited frozen
        # artifact.  A Demo warmup audit is refreshed on a cadence, however,
        # so its safe lower bound is the isolated experiment start rather than
        # the most recent audit heartbeat.
        if warmup:
            activated = int(datetime.fromisoformat(
                artifact["experiment_started_at"].replace("Z", "+00:00")
            ).timestamp())
        now = int(time.time())
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            pending = conn.execute("""SELECT s.* FROM okx_micro_model_signals s
                LEFT JOIN okx_micro_executions e ON e.signal_key=s.signal_key
                WHERE s.experiment_id=? AND s.entry_minute>=? AND s.entry_minute>=?
                  AND e.signal_key IS NULL ORDER BY s.entry_minute,s.predicted_r DESC""",
                (artifact["experiment_id"], activated, now - MAX_SIGNAL_AGE_SECONDS)).fetchall()
        for row in pending:
            self.execute_signal(row, artifact)
        status["signals_considered"] = len(pending)
        atomic_json(EXECUTOR_STATE_PATH, status)
        return status

    def run(self) -> None:
        while self.running:
            try:
                self.once()
            except Exception:
                LOG.exception("micro executor cycle failed")
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
