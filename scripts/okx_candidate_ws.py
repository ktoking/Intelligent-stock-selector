#!/usr/bin/env python3
"""Persistent demo execution engine for the staged 10m -> 5m -> 1m strategy."""
from __future__ import annotations

import json
import logging
import math
import os
import signal as process_signal
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import websocket

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_intraday_agent import (  # noqa: E402
    EXECUTION_STATE_PATH,
    DB_PATH,
    OKX,
    STATE_PATH,
    atr,
    demo_instrument_tradeable,
    decimal_step,
    estimated_slippage_bps,
    in_us_cash_session,
    kill_switch,
    notify,
    position_lines,
    account_line,
    today_pnl_line,
    risk_sized_order,
    settings,
    signal as strategy_signal,
    spread_bps,
)

UTC = timezone.utc
OUT = ROOT / "data" / "okx_candidate_ws.json"
MICROSTRUCTURE_PATH = ROOT / "data" / "okx_microstructure.json"
SHADOW_LEARNING_PATH = ROOT / "data" / "okx_shadow_learning.json"
LOG = logging.getLogger("okx-executor")
TERMINAL = {"EXECUTED", "REJECTED", "EXPIRED", "REMOVED", "CLOSED"}
SCALE_OUT_FRACTION = 0.40
SCALE_OUT_R = 1.5
EXPLORATORY_DEMO_ENV = "OKX_INTRADAY_EXPLORATORY_DEMO"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return fallback


def atomic_json(path: Path, value: Any) -> None:
    def finite(item: Any) -> Any:
        if isinstance(item, float) and not math.isfinite(item):
            return None
        if isinstance(item, dict):
            return {key: finite(child) for key, child in item.items()}
        if isinstance(item, list):
            return [finite(child) for child in item]
        return item
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(finite(value), ensure_ascii=False, indent=2, allow_nan=False))
    temporary.replace(path)


def candidate_map() -> dict[str, dict[str, Any]]:
    state = read_json(STATE_PATH, {})
    return {row["instId"]: row for row in state.get("candidates", []) if row.get("instId")}


def microstructure_confirmation(inst_id: str, side: str) -> dict[str, Any]:
    """Return a non-blocking live-flow confirmation for shadow calibration."""
    snapshot = read_json(MICROSTRUCTURE_PATH, {})
    row = (snapshot.get("data") or {}).get(inst_id) or {}
    try:
        age_seconds = max(0.0, time.time() - float(row["minute_ts"]))
    except (KeyError, TypeError, ValueError):
        age_seconds = float("inf")
    book = float(row.get("book_imbalance") or 0)
    aggressive = float(row.get("aggressive_imbalance") or 0)
    direction = 1 if side == "LONG" else -1
    available = age_seconds <= 120 and int(row.get("trade_count") or 0) > 0
    aligned = available and book * direction > 0 and aggressive * direction > 0
    return {
        "available": available,
        "aligned": aligned,
        "age_seconds": round(age_seconds, 1) if math.isfinite(age_seconds) else None,
        "book_imbalance": book,
        "aggressive_imbalance": aggressive,
        "trade_count": int(row.get("trade_count") or 0),
        "spread_bps": float(row.get("spread_bps") or 0),
        "bid_depth": float(row.get("bid_depth") or 0),
        "ask_depth": float(row.get("ask_depth") or 0),
        "mode": "shadow_only",
    }


def deployment_gate(required_strategies: set[str] | None = None) -> tuple[bool, str]:
    """Hard research gate, optionally scoped to the caller's execution path.

    A strategy passing forward validation must never unlock a different
    strategy's executor.  Read-only/dashboard callers may omit the scope, but
    every order path must supply its own supported strategy identifiers.
    """
    state = read_json(SHADOW_LEARNING_PATH, {})
    if not state.get("passed"):
        return False, "前向验收尚未通过"
    if not state.get("execution_ready"):
        return False, "前向指标已通过但对应执行路径尚未完成审计"
    strategy = state.get("qualified_strategy")
    if strategy not in {"micro_barrier_executable", "gap_fade_executable", "return_model_executable", "rule_micro_aligned"}:
        return False, "合格策略标识缺失或不受支持"
    if required_strategies is not None and strategy not in required_strategies:
        return False, f"合格策略 {strategy} 与当前执行路径不匹配"
    return True, f"研究与执行审计均通过：{strategy}"


def exploratory_demo_enabled(cfg: Any) -> bool:
    """A clearly labelled, bounded Demo-only trial; never a research promotion."""
    return cfg.profile == "demo" and os.getenv(EXPLORATORY_DEMO_ENV) == "1"


def ensure_shadow_schema() -> None:
    """Create and migrate the forward-label store before any worker uses it."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS okx_signal_shadow (
                signal_key TEXT PRIMARY KEY, inst_id TEXT NOT NULL, side TEXT NOT NULL,
                stage TEXT NOT NULL,
                entry_ts INTEGER NOT NULL, due_ts INTEGER NOT NULL, entry_price REAL NOT NULL,
                horizon_minutes INTEGER NOT NULL DEFAULT 60,
                atr14 REAL, score REAL, volume_ratio REAL, spread_bps REAL, slippage_bps REAL,
                book_imbalance REAL, aggressive_imbalance REAL, micro_available INTEGER,
                exit_price REAL, gross_r REAL, net_r REAL, mfe_r REAL, mae_r REAL,
                labeled_at TEXT, created_at TEXT NOT NULL
            )
        """)
        columns = {item[1] for item in conn.execute("PRAGMA table_info(okx_signal_shadow)")}
        if "experiment_id" not in columns:
            conn.execute("ALTER TABLE okx_signal_shadow ADD COLUMN experiment_id TEXT NOT NULL DEFAULT 'rule_v1'")
        if "horizon_minutes" not in columns:
            conn.execute("ALTER TABLE okx_signal_shadow ADD COLUMN horizon_minutes INTEGER NOT NULL DEFAULT 60")


def record_shadow_signal(row: dict[str, Any], candle_ts: int, price: float, stage: str) -> None:
    """Persist confirmed signals for delayed, order-independent outcome labels."""
    ensure_shadow_schema()
    micro = row.get("microstructure") or {}
    with sqlite3.connect(DB_PATH) as conn:
        experiment_id = str(row.get("experiment_id") or "rule_v1")
        horizon_minutes = max(5, min(240, int(row.get("horizon_minutes") or 60)))
        key = f"{row['instId']}:{row['side']}:{candle_ts}:{experiment_id}"
        conn.execute("""
            INSERT OR IGNORE INTO okx_signal_shadow
            (signal_key,inst_id,side,stage,entry_ts,due_ts,entry_price,horizon_minutes,atr14,score,volume_ratio,
             spread_bps,slippage_bps,book_imbalance,aggressive_imbalance,micro_available,created_at,experiment_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            f"{key}:{stage}", row["instId"], row["side"], stage,
            candle_ts, candle_ts + horizon_minutes * 60_000, price, horizon_minutes,
            float(row.get("atr14") or 0), float(row.get("score") or 0),
            float(row.get("volume_ratio") or 0), float(row.get("spread_bps") or 0),
            float(row.get("estimated_slippage_bps") or 0), float(micro.get("book_imbalance") or 0),
            float(micro.get("aggressive_imbalance") or 0), int(bool(micro.get("available"))), now_iso(),
            experiment_id,
        ))


def side_still_valid(side: str, metrics: dict[str, float]) -> tuple[bool, str]:
    long_ok = metrics["ema9"] > metrics["ema21"] and metrics["price"] > metrics["vwap20"]
    short_ok = metrics["ema9"] < metrics["ema21"] and metrics["price"] < metrics["vwap20"]
    trend_ok = long_ok if side == "LONG" else short_ok
    volume_ok = metrics["volume_ratio"] >= 1.0
    if not trend_ok:
        return False, "5m 趋势或 VWAP 已反向"
    if not volume_ok:
        return False, f"5m 量比 {metrics['volume_ratio']:.2f}x 低于 1.00x"
    return True, "5m 趋势、VWAP 与量能继续同向"


def breakout_held(candidate: dict[str, Any], close: float) -> tuple[bool, str]:
    side = candidate["side"]
    level = float(candidate["breakout_high"] if side == "LONG" else candidate["breakout_low"])
    ok = close > level if side == "LONG" else close < level
    direction = "站稳" if side == "LONG" else "跌破"
    return ok, f"1m 收盘 {close:.6g} {'已' if ok else '未'}{direction}突破位 {level:.6g}"


def session_allows_entry(cfg: Any, inst_id: str) -> bool:
    return inst_id == "BTC-USDT-SWAP" or cfg.session == "24x7" or in_us_cash_session()


def in_opening_reversal_window(now: datetime | None = None) -> bool:
    local = (now or datetime.now(cfg_ny())).astimezone(cfg_ny())
    return local.weekday() < 5 and (local.hour, local.minute) >= (9, 30) and (local.hour, local.minute) < (10, 0)


def daily_loss_guard_active(cfg: Any, trading_day: object) -> bool:
    return str(getattr(cfg, "daily_loss_guard_bypass_date", "")) != str(trading_day)


def side_reversed(side: str, metrics: dict[str, float]) -> bool:
    """Exit only when EMA and VWAP both reverse; one weak reading is not enough."""
    if side == "LONG":
        return metrics["ema9"] < metrics["ema21"] and metrics["price"] < metrics["vwap20"]
    return metrics["ema9"] > metrics["ema21"] and metrics["price"] > metrics["vwap20"]


def r_multiple(side: str, entry: float, initial_stop: float, mark: float) -> float:
    risk = abs(entry - initial_stop)
    if risk <= 0:
        return 0.0
    return ((mark - entry) if side == "LONG" else (entry - mark)) / risk


def protection_prices_from_fill(side: str, fill_price: float, stop_distance: float) -> tuple[float, float]:
    """Keep the planned volatility risk distance, anchored to the actual fill."""
    if side == "LONG":
        return fill_price - stop_distance, fill_price + stop_distance * 1.8
    return fill_price + stop_distance, fill_price - stop_distance * 1.8


def exchange_exit_reason(trade: dict[str, Any], close_price: float) -> str:
    """Classify a completed exchange-side OCO without guessing from a fill side."""
    stop, target = float(trade.get("active_stop_px") or 0), float(trade.get("target_px") or 0)
    tick = max(float(trade.get("tick_size") or 0.01), 1e-9)
    if target and abs(close_price - target) <= tick * 2:
        return "交易所 OCO 止盈触发"
    if stop and abs(close_price - stop) <= tick * 2:
        return "交易所 OCO 止损触发"
    return "交易所保护单/外部平仓成交"


def position_notional_usdt(row: dict[str, Any]) -> float:
    """Best-effort absolute USD notional from one OKX position response."""
    direct = abs(float(row.get("notionalUsd") or 0))
    if direct > 0:
        return direct
    return abs(float(row.get("pos") or 0) * float(row.get("markPx") or row.get("avgPx") or 0) * float(row.get("ctVal") or 1))


def select_dynamic_leverage(row: dict[str, Any], base_leverage: int = 3, max_leverage: int = 5) -> tuple[int, str]:
    """Grade leverage by confirmed signal quality, never by directional conviction alone."""
    score = float(row.get("score") or 0)
    volume = float(row.get("volume_ratio") or 0)
    spread_value, slippage_value = row.get("spread_bps"), row.get("estimated_slippage_bps")
    spread = float(spread_value) if spread_value is not None else float("inf")
    slippage = float(slippage_value) if slippage_value is not None else float("inf")
    event_risk = bool(row.get("event_risk"))
    if not event_risk and score >= 85 and volume >= 2 and spread < 2 and slippage < 2:
        return min(5, max_leverage), "S级：高评分、高量比、低点差、低滑点、无事件风险"
    if not event_risk and score >= 75 and volume >= 1.5 and spread < 5 and slippage < 5:
        return min(4, max_leverage), "A级：趋势确认、量能充足、点差与滑点合格"
    return min(base_leverage, max_leverage), "常规级：维持基础杠杆，风险预算不变"


def risk_gate(client: OKX, cfg: Any, inst_id: str, planned_notional: float = 0,
              opening_reversal: bool = False) -> tuple[bool, str]:
    switch = kill_switch()
    if switch["enabled"]:
        return False, f"kill switch 已开启：{switch['reason']}"
    research_allowed, research_reason = deployment_gate({"rule_micro_aligned"})
    trial = exploratory_demo_enabled(cfg)
    if not research_allowed and not trial:
        return False, f"研究硬门已阻止开仓：{research_reason}"
    if not session_allows_entry(cfg, inst_id):
        return False, "等待美股现金交易时段"
    positions = [row for row in client.request("GET", "/api/v5/account/positions", private=True)["data"] if float(row.get("pos") or 0)]
    if any(row.get("instId") == inst_id for row in positions):
        return False, "已有同标的仓位，禁止加仓或反向对冲"
    if len(positions) >= cfg.max_open_positions:
        return False, f"已达到同时持仓上限 {cfg.max_open_positions}"
    gross_notional = sum(position_notional_usdt(row) for row in positions)
    if gross_notional + planned_notional > cfg.max_gross_notional_usdt:
        return False, (
            f"总名义敞口超限：当前 ${gross_notional:,.2f} + 计划 ${planned_notional:,.2f} "
            f"> 上限 ${cfg.max_gross_notional_usdt:,.2f}"
        )
    fills = client.request("GET", "/api/v5/trade/fills", {"instType": "SWAP", "limit": "100"}, private=True)["data"]
    today = datetime.now(UTC).astimezone(cfg_ny()).date()
    today_fills = [row for row in fills if datetime.fromtimestamp(int(row.get("fillTime") or 0) / 1000, UTC).astimezone(cfg_ny()).date() == today]
    # `fills.pnl` is commonly zero.  Completed-position realisedPnl is the
    # exchange's net figure (including fees/funding) used by the dashboard.
    history = client.request("GET", "/api/v5/account/positions-history", {"instType": "SWAP", "limit": "100"}, private=True)["data"]
    closed_today = [row for row in history if datetime.fromtimestamp(
        int(row.get("uTime") or 0) / 1000, UTC
    ).astimezone(cfg_ny()).date() == today]
    pnl = sum(float(row.get("realizedPnl") or 0) for row in closed_today) + sum(float(row.get("upl") or 0) for row in positions)
    if daily_loss_guard_active(cfg, today) and pnl <= -cfg.max_daily_loss_usdt:
        return False, f"日亏损熔断已触发：{pnl:.2f} USDT"
    if cfg.max_entries_per_day > 0 and len({row.get("ordId") for row in today_fills}) >= cfg.max_entries_per_day:
        return False, f"已达到日内入场上限 {cfg.max_entries_per_day}"
    latest = max((int(row.get("fillTime") or 0) for row in today_fills if row.get("instId") == inst_id), default=0)
    if opening_reversal and in_opening_reversal_window():
        return True, "开盘反手候选：已平仓后允许一次反向 1m 确认，不受同标的冷却阻塞"
    if latest and datetime.now(UTC) - datetime.fromtimestamp(latest / 1000, UTC) < timedelta(minutes=cfg.cooldown_minutes):
        return False, f"同标的仍在 {cfg.cooldown_minutes} 分钟冷却期"
    if trial:
        return True, "探索性模拟盘试运行：不计入严格前向验收"
    return True, "账户风控通过"


def cfg_ny():
    from zoneinfo import ZoneInfo
    return ZoneInfo("America/New_York")


class Engine:
    def __init__(self) -> None:
        ensure_shadow_schema()
        self.cfg = settings()
        self.client = OKX(self.cfg)
        self.execute = os.getenv("OKX_INTRADAY_EXECUTE_DEMO") == "1"
        self.runtime = read_json(EXECUTION_STATE_PATH, {"updated_at": None, "engine": {}, "candidates": {}, "history": []})
        self.market = {"quotes": {}, "books": {}, "candles": {}}
        self.last_five: dict[str, str] = {}
        self.last_one: dict[str, str] = {}
        self.subscribed: set[str] = set()
        self.last_periodic_minute = ""
        self.last_position_management_minute = ""
        self.last_save_at = 0.0
        self.save_lock = threading.Lock()
        self.ws: websocket.WebSocketApp | None = None
        self.running = True

    def stop(self) -> None:
        self.running = False
        if self.ws is not None:
            self.ws.close()

    def save(self, force: bool = True) -> None:
        with self.save_lock:
            if not force and time.monotonic() - self.last_save_at < 2:
                return
            self.last_save_at = time.monotonic()
            self.runtime["updated_at"] = now_iso()
            self.runtime["engine"] = {
                "running": True, "demo_execution_enabled": self.execute,
                "exploratory_demo": exploratory_demo_enabled(self.cfg),
                "mode": "10m discovery / REST closed-candle review / WebSocket quote-book execution",
                "pid": os.getpid(), "kill_switch": kill_switch(),
                "deployment_gate": {"allowed": deployment_gate()[0], "reason": deployment_gate()[1]},
            }
            atomic_json(EXECUTION_STATE_PATH, self.runtime)
            atomic_json(OUT, {"updated_at": time.time(), "candidates": list(candidate_map()), **self.market})

    def sync(self) -> bool:
        incoming = candidate_map()
        rows = self.runtime.setdefault("candidates", {})
        changed = set(incoming) != {key for key, row in rows.items() if row.get("status") not in TERMINAL}
        for inst_id, candidate in incoming.items():
            existing = rows.get(inst_id)
            if not existing or existing.get("source_created_at") != candidate.get("created_at"):
                rows[inst_id] = {
                    **candidate, "source_created_at": candidate.get("created_at"),
                    "status": "DISCOVERED", "status_reason": "等待 5m K 线收盘复核",
                    "updated_at": now_iso(), "five_min_confirmed_at": None,
                }
                try:
                    stamp = int(datetime.fromisoformat(candidate["created_at"]).timestamp() * 1000)
                    candidate_with_micro = {
                        **candidate,
                        "microstructure": microstructure_confirmation(inst_id, candidate["side"]),
                    }
                    record_shadow_signal(
                        candidate_with_micro, stamp, float(candidate.get("price") or 0), "TEN_MIN_DISCOVERED"
                    )
                    rows[inst_id]["ten_min_shadow_recorded_at"] = now_iso()
                except (KeyError, TypeError, ValueError):
                    LOG.warning("cannot shadow-record discovered candidate %s", inst_id)
            elif not existing.get("ten_min_shadow_recorded_at"):
                try:
                    stamp = int(datetime.fromisoformat(candidate["created_at"]).timestamp() * 1000)
                    record_shadow_signal(
                        {**candidate, "microstructure": microstructure_confirmation(inst_id, candidate["side"])},
                        stamp, float(candidate.get("price") or 0), "TEN_MIN_DISCOVERED",
                    )
                    existing["ten_min_shadow_recorded_at"] = now_iso()
                except (KeyError, TypeError, ValueError):
                    LOG.warning("cannot backfill discovered candidate %s", inst_id)
        for inst_id, row in list(rows.items()):
            if row.get("source") == "opening_reversal" and row.get("status") not in TERMINAL:
                continue
            if inst_id not in incoming and row.get("status") not in TERMINAL:
                row.update(status="REMOVED", status_reason="下一轮 10m 排名未保留", updated_at=now_iso())
                self.runtime.setdefault("history", []).append(dict(row))
        self.runtime["history"] = self.runtime.get("history", [])[-50:]
        self.save()
        return changed

    def active_candidate_ids(self) -> set[str]:
        return {inst_id for inst_id, row in self.runtime.get("candidates", {}).items() if row.get("status") not in TERMINAL}

    def reject_expired(self) -> None:
        now = datetime.now(UTC)
        for row in self.runtime.get("candidates", {}).values():
            if row.get("status") in TERMINAL:
                continue
            try:
                expired = now >= datetime.fromisoformat(row["expires_at"])
            except (KeyError, ValueError):
                expired = True
            if expired:
                row.update(status="EXPIRED", status_reason="候选超过 20 分钟有效期", updated_at=now_iso())

    @staticmethod
    def _position_size(position: dict[str, Any]) -> float:
        return abs(float(position.get("pos") or 0))

    def _tracked_trade(self, inst_id: str, pos_side: str) -> dict[str, Any] | None:
        """Only manage positions opened by this engine, never unrelated demo orders."""
        for row in reversed(self.runtime.get("history", [])):
            if row.get("instId") == inst_id and row.get("pos_side") == pos_side and row.get("status") == "EXECUTED" and not row.get("closed_at"):
                return row
        return None

    @staticmethod
    def _bot_owned_position(position: dict[str, Any], fills: list[dict[str, Any]]) -> bool:
        """Identify an orphan created by this engine without touching manual demo positions."""
        created = int(position.get("cTime") or 0)
        expected_side = "buy" if position.get("posSide") == "long" else "sell"
        return any(
            str(fill.get("clOrdId") or "").startswith("sa")
            and fill.get("instId") == position.get("instId")
            and fill.get("posSide") == position.get("posSide")
            and fill.get("side") == expected_side
            and abs(int(fill.get("fillTime") or 0) - created) <= 10 * 60_000
            for fill in fills
        )

    def exit_orphan_position(self, position: dict[str, Any]) -> bool:
        """Immediately flatten a bot-owned position that has no local protection state."""
        inst_id, pos_side = position["instId"], position.get("posSide")
        close_side = "sell" if pos_side == "long" else "buy"
        instrument = self.client.instrument(inst_id)
        size = decimal_step(self._position_size(position), instrument.get("lotSz") or "1", "nearest")
        try:
            accepted = self.client.place_market(inst_id, close_side, size, pos_side, reduce_only=True)
            order_id = str(accepted.get("ordId") or "")
            detail: dict[str, Any] = {}
            for _ in range(10):
                time.sleep(0.35)
                detail = self.client.order(inst_id, order_id)
                if detail.get("state") in {"filled", "canceled"}:
                    break
            if float(detail.get("accFillSz") or 0) <= 0:
                raise RuntimeError(f"孤儿仓 reduceOnly 未成交：{detail.get('state') or 'unknown'}")
            notify(self.cfg, (
                f"⚠️ **OKX 模拟盘孤儿仓兜底平仓｜{inst_id}**\n\n"
                f"**方向**：平{'多' if pos_side == 'long' else '空'}｜{float(detail.get('accFillSz') or 0):g} 张\n"
                f"**成交价**：${float(detail.get('avgPx') or detail.get('fillPx') or 0):,.4f}\n"
                f"**原因**：机器人订单已有成交，但本地执行状态或保护单缺失，立即 reduceOnly 退出\n\n"
                f"{today_pnl_line(self.client)}"
            ))
            return True
        except Exception:
            LOG.exception("failed to flatten bot-owned orphan %s", inst_id)
            return False

    @staticmethod
    def _protection_algo(algos: list[dict[str, Any]], pos_side: str) -> dict[str, Any] | None:
        for algo in algos:
            # An attached order can report an empty posSide on some demo
            # accounts; its TP+SL fields still identify it as full protection.
            if algo.get("posSide") not in ("", pos_side):
                continue
            if float(algo.get("slTriggerPx") or 0) > 0 and float(algo.get("tpTriggerPx") or 0) > 0:
                return algo
        return None

    @staticmethod
    def _stop_algo(algos: list[dict[str, Any]], pos_side: str, algo_id: str = "") -> dict[str, Any] | None:
        for algo in algos:
            if algo_id and str(algo.get("algoId") or "") != algo_id:
                continue
            if algo.get("posSide") not in ("", pos_side):
                continue
            if float(algo.get("slTriggerPx") or 0) > 0:
                return algo
        return None

    def _record_closed(self, trade: dict[str, Any], reason: str) -> None:
        trade.update(status="CLOSED", closed_at=now_iso(), close_reason=reason, updated_at=now_iso())
        self.runtime.setdefault("history", []).append(dict(trade))
        self.runtime["history"] = self.runtime["history"][-50:]

    def exit_position(self, trade: dict[str, Any], position: dict[str, Any], reason: str) -> bool:
        inst_id, pos_side = trade["instId"], position.get("posSide")
        close_side = "sell" if pos_side == "long" else "buy"
        instrument = self.client.instrument(inst_id)
        size = decimal_step(self._position_size(position), instrument.get("lotSz") or "1", "nearest")
        if float(size or 0) <= 0:
            return False
        try:
            accepted = self.client.place_market(inst_id, close_side, size, pos_side, reduce_only=True)
            if accepted.get("sCode") not in (None, "", "0"):
                raise RuntimeError(f"{accepted.get('sCode')} {accepted.get('sMsg')}")
            order_id = str(accepted["ordId"])
            detail: dict[str, Any] = {}
            for _ in range(8):
                time.sleep(0.35)
                detail = self.client.order(inst_id, order_id)
                if detail.get("state") in {"filled", "canceled"}:
                    break
            if detail.get("state") != "filled" or float(detail.get("accFillSz") or 0) <= 0:
                raise RuntimeError(f"reduceOnly 平仓未完全成交：{detail.get('state') or 'unknown'}")
            self._record_closed(trade, reason)
            notify(self.cfg, (
                f"📤 **OKX 模拟盘平仓｜{inst_id}**\n\n"
                f"**方向**：平{'多' if pos_side == 'long' else '空'}｜市价成交 {float(detail.get('accFillSz') or 0):g} 张\n"
                f"**成交价**：${float(detail.get('avgPx') or detail.get('fillPx') or 0):,.4f}\n"
                f"**原因**：{reason}\n\n"
                f"{today_pnl_line(self.client)}\n\n"
                f"**剩余资金**\n{account_line(self.client)}"
            ))
            return True
        except Exception as exc:
            trade.update(status_reason=f"请求市价平仓失败，下一分钟重试：{exc}", updated_at=now_iso())
            LOG.exception("failed to close %s for %s", inst_id, reason)
            return False

    def stage_opening_reversal(self, trade: dict[str, Any], metrics: dict[str, float]) -> None:
        """After a confirmed opening reversal exit, require only a fresh 1m breakout to flip."""
        if not in_opening_reversal_window() or metrics.get("volume_ratio", 0) < 1.0:
            return
        inst_id = trade["instId"]
        side = "SHORT" if trade["side"] == "LONG" else "LONG"
        now = datetime.now(UTC)
        self.runtime.setdefault("candidates", {})[inst_id] = {
            "instId": inst_id, "side": side, "score": 78.0,
            "reason": "开盘 EMA/VWAP 反转后的受控反手候选",
            "source": "opening_reversal", "reversal_of": trade.get("order_id"),
            "created_at": now.isoformat(), "source_created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=8)).isoformat(),
            "price": metrics["price"], "ema9": metrics["ema9"], "ema21": metrics["ema21"],
            "vwap20": metrics["vwap20"], "volume_ratio": metrics["volume_ratio"], "atr14": metrics["atr14"],
            "breakout_high": metrics["breakout_high"], "breakout_low": metrics["breakout_low"],
            "spread_bps": float("inf"), "event_risk": False,
            "status": "FIVE_MIN_CONFIRMED",
            "status_reason": "开盘反手：原仓已按 5m EMA/VWAP 反转退出，等待 1m 新突破确认",
            "five_min_confirmed_at": now_iso(), "updated_at": now_iso(),
        }
        self.save()

    def ensure_protection(self, trade: dict[str, Any], position: dict[str, Any]) -> bool:
        """Confirm TP+SL after fill. Missing protection gets one OCO repair attempt, then exits."""
        inst_id, pos_side = trade["instId"], position.get("posSide")
        algos = self.client.pending_algos(inst_id)
        protection = self._protection_algo(algos, pos_side)
        if protection:
            trade.update(protection_algo_id=str(protection.get("algoId") or ""), protection_verified_at=now_iso())
            return True
        try:
            instrument = self.client.instrument(inst_id)
            repaired = self.client.place_oco_protection(
                inst_id, pos_side, decimal_step(self._position_size(position), instrument.get("lotSz") or "1", "nearest"),
                decimal_step(float(trade["active_stop_px"]), str(trade["tick_size"]), "nearest"),
                decimal_step(float(trade["target_px"]), str(trade["tick_size"]), "nearest"),
            )
            if repaired.get("sCode") not in (None, "", "0"):
                raise RuntimeError(f"{repaired.get('sCode')} {repaired.get('sMsg')}")
            for _ in range(6):
                time.sleep(0.5)
                protection = self._protection_algo(self.client.pending_algos(inst_id), pos_side)
                if protection:
                    trade.update(protection_algo_id=str(protection.get("algoId") or ""), protection_verified_at=now_iso(), protection_repaired_at=now_iso())
                    return True
            raise RuntimeError("补挂 OCO 后未找到完整 TP/SL")
        except Exception as exc:
            trade.update(status_reason=f"保护单缺失且补挂失败：{exc}", updated_at=now_iso())
            LOG.error("protection unavailable for %s; reduce-only exit", inst_id)
            self.exit_position(trade, position, "保护单缺失，补挂失败，reduceOnly 兜底退出")
            return False

    def ensure_runner_stop(self, trade: dict[str, Any], position: dict[str, Any]) -> bool:
        """Keep a full-size stop active while the profit target is managed locally."""
        inst_id, pos_side = trade["instId"], position.get("posSide")
        instrument = self.client.instrument(inst_id)
        expected_size = self._position_size(position)
        algos = self.client.pending_algos(inst_id)
        stop = self._stop_algo(algos, pos_side, str(trade.get("runner_stop_algo_id") or ""))
        if stop:
            lot = float(instrument.get("lotSz") or 1)
            if abs(float(stop.get("sz") or 0) - expected_size) < lot / 2:
                trade.update(runner_stop_algo_id=str(stop.get("algoId") or ""), protection_verified_at=now_iso())
                return True
            self.client.cancel_algos([{"instId": inst_id, "algoId": str(stop.get("algoId") or "")}])
            trade.update(runner_stop_algo_id="", status_reason="持仓数量变化，重建全量 runner 止损")
        try:
            size = decimal_step(self._position_size(position), instrument.get("lotSz") or "1", "nearest")
            created = self.client.place_stop_protection(
                inst_id, pos_side, size,
                decimal_step(float(trade["active_stop_px"]), str(trade["tick_size"]), "nearest"),
            )
            if created.get("sCode") not in (None, "", "0"):
                raise RuntimeError(f"{created.get('sCode')} {created.get('sMsg')}")
            for _ in range(6):
                time.sleep(0.5)
                stop = self._stop_algo(self.client.pending_algos(inst_id), pos_side, str(created.get("algoId") or ""))
                if stop:
                    trade.update(runner_stop_algo_id=str(stop.get("algoId") or ""), protection_verified_at=now_iso())
                    return True
            raise RuntimeError("仅止损单创建后未找到")
        except Exception as exc:
            trade.update(status_reason=f"runner 止损缺失且补挂失败：{exc}", updated_at=now_iso())
            LOG.error("runner stop unavailable for %s; reduce-only exit", inst_id)
            self.exit_position(trade, position, "runner 止损缺失，reduceOnly 兜底退出")
            return False

    def scale_out_runner(self, trade: dict[str, Any], position: dict[str, Any]) -> bool:
        """Realize 40% at 1.5R, then resize the runner stop to the remaining position."""
        inst_id, pos_side = trade["instId"], position.get("posSide")
        instrument = self.client.instrument(inst_id)
        original_size = self._position_size(position)
        scale_size = decimal_step(original_size * SCALE_OUT_FRACTION, instrument.get("lotSz") or "1", "floor")
        if float(scale_size or 0) <= 0 or float(scale_size) >= original_size:
            trade.update(scale_out_done=True, scale_out_reason="仓位小于可分批合约单位，全部作为 runner", updated_at=now_iso())
            return True
        close_side = "sell" if pos_side == "long" else "buy"
        try:
            accepted = self.client.place_market(inst_id, close_side, scale_size, pos_side, reduce_only=True)
            if accepted.get("sCode") not in (None, "", "0"):
                raise RuntimeError(f"{accepted.get('sCode')} {accepted.get('sMsg')}")
            order_id = str(accepted["ordId"])
            detail: dict[str, Any] = {}
            for _ in range(8):
                time.sleep(0.35)
                detail = self.client.order(inst_id, order_id)
                if detail.get("state") in {"filled", "canceled"}:
                    break
            if detail.get("state") != "filled" or float(detail.get("accFillSz") or 0) <= 0:
                raise RuntimeError(f"分批止盈未完全成交：{detail.get('state') or 'unknown'}")
            remaining = next((item for item in self.client.positions(inst_id)
                              if item.get("posSide") == pos_side and self._position_size(item) > 0), None)
            if not remaining:
                self._record_closed(trade, "1.5R 分批止盈后仓位已全部成交")
                return True
            old_algo_id = str(trade.get("runner_stop_algo_id") or "")
            if old_algo_id:
                self.client.cancel_algos([{"instId": inst_id, "algoId": old_algo_id}])
                for _ in range(5):
                    time.sleep(0.3)
                    if not self._stop_algo(self.client.pending_algos(inst_id), pos_side, old_algo_id):
                        break
                else:
                    raise RuntimeError("旧 runner 止损未取消，不能安全重设剩余仓位止损")
            trade.update(
                scale_out_done=True, scale_order_id=order_id,
                scale_fill_size=float(detail.get("accFillSz") or 0),
                scale_fill_price=float(detail.get("avgPx") or detail.get("fillPx") or 0),
                runner_stop_algo_id="", updated_at=now_iso(),
            )
            if not self.ensure_runner_stop(trade, remaining):
                return False
            notify(self.cfg, (
                f"💰 **OKX 模拟盘分批止盈｜{inst_id}**\n\n"
                f"**已兑现**：{trade['scale_fill_size']:g} 张 @ ${trade['scale_fill_price']:,.4f}｜达到 {SCALE_OUT_R:g}R\n"
                f"**继续持有**：{self._position_size(remaining):g} 张 runner｜止损继续跟踪\n"
                f"**规则**：40% 落袋，60% 让利润奔跑\n\n"
                f"{today_pnl_line(self.client)}"
            ))
            return True
        except Exception as exc:
            trade.update(status_reason=f"1.5R 分批止盈失败，下分钟重试：{exc}", updated_at=now_iso())
            LOG.exception("scale-out failed for %s", inst_id)
            return False

    def rebase_protection_to_fill(self, trade: dict[str, Any], position: dict[str, Any]) -> bool:
        """Make 1R/1.8R reflect the average fill, not a stale pre-trade quote."""
        if trade.get("protection_rebased_at"):
            return True
        algo_id = str(trade.get("protection_algo_id") or "")
        if not algo_id:
            return False
        distance = float(trade.get("planned_stop_distance") or (trade.get("sizing") or {}).get("stop_distance") or 0)
        if distance <= 0:
            return False
        fill_price = float(trade["fill_price"])
        stop, target = protection_prices_from_fill(trade["side"], fill_price, distance)
        tick = str(trade["tick_size"])
        stop_text, target_text = decimal_step(stop, tick, "nearest"), decimal_step(target, tick, "nearest")
        try:
            response = self.client.amend_algo_protection(trade["instId"], algo_id, stop_text, target_text)
            if response.get("sCode") not in (None, "", "0"):
                raise RuntimeError(f"{response.get('sCode')} {response.get('sMsg')}")
            protection = self._protection_algo(self.client.pending_algos(trade["instId"]), position.get("posSide"))
            if not protection:
                raise RuntimeError("改价请求后未找到完整 TP/SL")
            if abs(float(protection.get("slTriggerPx") or 0) - float(stop_text)) > float(tick) * 2 or abs(float(protection.get("tpTriggerPx") or 0) - float(target_text)) > float(tick) * 2:
                raise RuntimeError("交易所保护单价格未按实际成交价更新")
            trade.update(
                initial_stop_px=float(stop_text), active_stop_px=float(stop_text), target_px=float(target_text),
                stop_px=float(stop_text), protection_rebased_at=now_iso(), protection_rebased_from="actual_fill",
            )
            return True
        except Exception as exc:
            # Never cancel a known-good protective order merely to rebase it.
            trade.update(status_reason=f"保护单已保留；按实际成交价重算失败，下分钟重试：{exc}", updated_at=now_iso())
            LOG.warning("protection rebase failed for %s: %s", trade["instId"], exc)
            return False

    def reconcile_exchange_closures(self, positions: list[dict[str, Any]]) -> None:
        """Notify when an OKX TP/SL closes a bot position outside our market-order path."""
        open_keys = {(row.get("instId"), row.get("posSide")) for row in positions}
        candidates: list[dict[str, Any]] = []
        seen_orders: set[str] = set()
        for trade in reversed(self.runtime.get("history", [])):
            order_id = str(trade.get("order_id") or "")
            if not order_id or order_id in seen_orders:
                continue
            seen_orders.add(order_id)
            if trade.get("status") == "EXECUTED" and not trade.get("closed_at") and (trade.get("instId"), trade.get("pos_side")) not in open_keys:
                candidates.append(trade)
        if not candidates:
            return
        history = self.client.request(
            "GET", "/api/v5/account/positions-history", {"instType": "SWAP", "limit": "100"}, private=True
        )["data"]
        for trade in candidates:
            filled_at = datetime.fromisoformat(trade["filled_at"]).timestamp() * 1000
            matches = [row for row in history if row.get("instId") == trade["instId"]
                       and row.get("posSide") == trade.get("pos_side")
                       and int(row.get("uTime") or 0) >= filled_at]
            if not matches:
                continue  # Exchange history can lag the position update briefly.
            closed = max(matches, key=lambda row: int(row.get("uTime") or 0))
            close_price = float(closed.get("closeAvgPx") or 0)
            reason = exchange_exit_reason(trade, close_price)
            trade.update(
                close_order_source="exchange", close_price=close_price,
                realized_pnl=float(closed.get("realizedPnl") or 0), close_fee=float(closed.get("fee") or 0),
            )
            self._record_closed(trade, reason)
            notify(self.cfg, (
                f"📤 **OKX 模拟盘平仓｜{trade['instId']}**\n\n"
                f"**方向**：平{'多' if trade.get('pos_side') == 'long' else '空'}｜交易所自动成交\n"
                f"**成交**：{float(closed.get('closeTotalPos') or 0):g} 张 @ ${close_price:,.4f}\n"
                f"**净已实现盈亏**：{'🔴 +' if float(closed.get('realizedPnl') or 0) >= 0 else '🟢 -'}${abs(float(closed.get('realizedPnl') or 0)):,.4f}\n"
                f"**原因**：{reason}\n\n"
                f"{today_pnl_line(self.client)}\n\n"
                f"**剩余资金**\n{account_line(self.client)}"
            ))

    def manage_positions(self) -> None:
        """Live exit manager: protection, time stop, 5m reversal, breakeven and ATR trail."""
        positions = [row for row in self.client.request("GET", "/api/v5/account/positions", private=True)["data"] if self._position_size(row) > 0]
        self.reconcile_exchange_closures(positions)
        now = datetime.now(UTC)
        fills: list[dict[str, Any]] | None = None
        for position in positions:
            inst_id, pos_side = position.get("instId"), position.get("posSide")
            trade = self._tracked_trade(inst_id, pos_side)
            if not trade:
                if fills is None:
                    fills = self.client.request(
                        "GET", "/api/v5/trade/fills", {"instType": "SWAP", "limit": "100"}, private=True
                    )["data"]
                if self._bot_owned_position(position, fills):
                    self.exit_orphan_position(position)
                continue
            try:
                runner_plan = trade.get("exit_plan") == "SCALE_RUNNER"
                if runner_plan:
                    if not self.ensure_runner_stop(trade, position):
                        continue
                else:
                    if not self.ensure_protection(trade, position):
                        continue
                    if not self.rebase_protection_to_fill(trade, position):
                        continue
                filled_at = datetime.fromisoformat(trade["filled_at"])
                if now - filled_at >= timedelta(minutes=self.cfg.max_holding_minutes):
                    self.exit_position(trade, position, f"持仓超过 {self.cfg.max_holding_minutes} 分钟的时间止损")
                    continue
                candles = self.client.candles(inst_id, limit=80, bar="5m")
                _, metrics = strategy_signal(candles)
                if side_reversed(trade["side"], metrics):
                    if self.exit_position(trade, position, "5 分钟 EMA 与 VWAP 同时反转"):
                        self.stage_opening_reversal(trade, metrics)
                    continue
                entry = float(trade["fill_price"])
                initial_stop = float(trade["initial_stop_px"])
                mark = float(position.get("markPx") or metrics["price"])
                multiple = r_multiple(trade["side"], entry, initial_stop, mark)
                trade.update(last_mark_px=mark, r_multiple=multiple, updated_at=now_iso())
                if runner_plan and not trade.get("scale_out_done") and multiple >= float(trade.get("scale_out_r") or SCALE_OUT_R):
                    # Resize the stop after the partial fill, then resume
                    # trailing management from the next minute.
                    self.scale_out_runner(trade, position)
                    continue
                desired_stop = float(trade["active_stop_px"])
                if multiple >= 1.2 and metrics["atr14"] > 0:
                    trail = metrics["atr14"] * 1.2
                    desired_stop = max(desired_stop, mark - trail) if trade["side"] == "LONG" else min(desired_stop, mark + trail)
                elif multiple >= 1.0:
                    desired_stop = max(desired_stop, entry) if trade["side"] == "LONG" else min(desired_stop, entry)
                tick = float(trade["tick_size"])
                if abs(desired_stop - float(trade["active_stop_px"])) >= tick:
                    algo_id = str((trade.get("runner_stop_algo_id") if runner_plan else trade.get("protection_algo_id")) or "")
                    if not algo_id:
                        raise RuntimeError("保护单缺少 algoId，不能安全修改止损")
                    response = self.client.amend_algo_stop(inst_id, algo_id, decimal_step(desired_stop, str(trade["tick_size"]), "nearest"))
                    if response.get("sCode") not in (None, "", "0"):
                        raise RuntimeError(f"{response.get('sCode')} {response.get('sMsg')}")
                    trade.update(active_stop_px=desired_stop, stop_mode="ATR_TRAIL" if multiple >= 1.2 else "BREAKEVEN", stop_updated_at=now_iso())
            except Exception as exc:
                trade.update(status_reason=f"持仓管理失败，下分钟重试：{exc}", updated_at=now_iso())
                LOG.exception("position management failed for %s", inst_id)

    def manage_positions_on_minute(self) -> None:
        """Keep time/stop supervision responsive without polling private APIs every second."""
        minute = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M")
        if minute == self.last_position_management_minute:
            return
        self.last_position_management_minute = minute
        self.manage_positions()

    def five_minute_review(self, inst_id: str, candle: list[str]) -> None:
        if len(candle) < 9 or candle[8] != "1" or self.last_five.get(inst_id) == candle[0]:
            return
        self.last_five[inst_id] = candle[0]
        row = self.runtime.get("candidates", {}).get(inst_id)
        if not row or row.get("status") in TERMINAL:
            return
        try:
            _, metrics = strategy_signal(self.client.candles(inst_id))
            valid, reason = side_still_valid(row["side"], metrics)
        except Exception as exc:
            row.update(status_reason=f"5m 数据复核失败，保留至下根 K 线：{exc}", updated_at=now_iso())
            return
        row.update(five_min_metrics=metrics, updated_at=now_iso())
        shadow_row = {
            **row, "atr14": metrics.get("atr14"), "volume_ratio": metrics.get("volume_ratio"),
            "microstructure": microstructure_confirmation(inst_id, row["side"]),
        }
        if not row.get("five_min_shadow_recorded_at"):
            record_shadow_signal(
                shadow_row, int(candle[0]), float(metrics.get("price") or candle[4]),
                "FIVE_MIN_PASS" if valid else "FIVE_MIN_FAIL",
            )
            row["five_min_shadow_recorded_at"] = now_iso()
        if valid:
            row.update(status="FIVE_MIN_CONFIRMED", status_reason=reason, five_min_confirmed_at=now_iso())
        else:
            row.update(status="REJECTED", status_reason=reason)
        self.save()

    def one_minute_confirm(self, inst_id: str, candle: list[str]) -> None:
        if len(candle) < 9 or candle[8] != "1" or self.last_one.get(inst_id) == candle[0]:
            return
        self.last_one[inst_id] = candle[0]
        row = self.runtime.get("candidates", {}).get(inst_id)
        if not row or row.get("status") not in {"FIVE_MIN_CONFIRMED", "READY"}:
            return
        close = float(candle[4])
        held, reason = breakout_held(row, close)
        microstructure = microstructure_confirmation(inst_id, row["side"])
        row.update(
            one_min_close=close, one_min_checked_at=now_iso(), status_reason=reason,
            microstructure=microstructure,
        )
        if not held:
            if not row.get("one_min_fail_shadow_recorded_at"):
                record_shadow_signal(row, int(candle[0]), close, "ONE_MIN_FAIL")
                row["one_min_fail_shadow_recorded_at"] = now_iso()
            self.save()
            return
        if not demo_instrument_tradeable(self.client, inst_id):
            row.update(status="REJECTED", status_reason="OKX Demo 不支持该美股代币私有交易，已在下单前剔除")
            self.save()
            return
        ticker, book = self.market["quotes"].get(inst_id, {}), self.market["books"].get(inst_id, {})
        current_spread = spread_bps(ticker)
        if not math.isfinite(current_spread):
            row.update(status_reason="等待完整 bid/ask 与五档盘口")
            self.save()
            return
        if current_spread > self.cfg.max_spread_bps:
            row.update(status_reason=f"1m 突破成立，但点差 {current_spread:.1f} bps 超限", spread_bps=current_spread)
            self.save()
            return
        try:
            instrument = self.client.instrument(inst_id)
            sizing = risk_sized_order(self.cfg, self.client.balance(), instrument, close, float(row.get("atr14") or 0))
            slippage = estimated_slippage_bps(book, row["side"], sizing["contracts_float"], sizing["ct_val"])
        except Exception as exc:
            row.update(status_reason=f"仓位/盘口校验失败：{exc}")
            self.save()
            return
        row.update(spread_bps=current_spread, estimated_slippage_bps=slippage, sizing=sizing)
        if not math.isfinite(slippage):
            row.update(status_reason="盘口深度不足，无法覆盖计划仓位")
            self.save()
            return
        if slippage > self.cfg.max_slippage_bps:
            row.update(status_reason=f"预估滑点 {slippage:.1f} bps 超限")
            self.save()
            return
        if not row.get("one_min_pass_shadow_recorded_at"):
            record_shadow_signal(row, int(candle[0]), close, "ONE_MIN_PASS")
            row["one_min_pass_shadow_recorded_at"] = now_iso()
        allowed, gate_reason = risk_gate(
            self.client, self.cfg, inst_id, sizing["notional"],
            opening_reversal=row.get("source") == "opening_reversal",
        )
        if not allowed:
            row.update(status="READY", status_reason=gate_reason)
            self.save()
            return
        if not self.execute:
            row.update(status="READY", status_reason="全部确认通过；模拟自动下单开关尚未开启")
            self.save()
            return
        self.execute_order(row, close, instrument, sizing)

    def execute_order(self, row: dict[str, Any], price: float, instrument: dict[str, str], sizing: dict[str, Any]) -> None:
        inst_id, direction = row["instId"], row["side"]
        pos_side, order_side = ("long", "buy") if direction == "LONG" else ("short", "sell")
        distance = float(sizing["stop_distance"])
        try:
            selected_leverage, leverage_reason = select_dynamic_leverage(
                row, base_leverage=self.cfg.leverage, max_leverage=self.cfg.max_dynamic_leverage,
            )
            try:
                self.client.set_leverage(inst_id, pos_side, selected_leverage)
            except Exception as leverage_exc:
                if selected_leverage == self.cfg.leverage:
                    raise
                LOG.warning("%s leverage %sx rejected; falling back to %sx: %s", inst_id, selected_leverage, self.cfg.leverage, leverage_exc)
                self.client.set_leverage(inst_id, pos_side, self.cfg.leverage)
                selected_leverage = self.cfg.leverage
                leverage_reason = f"交易所未接受高档杠杆，回退基础 {selected_leverage}x"
            # A single clOrdId is intentionally reused across retries.  If OKX
            # accepted the first request but its response was lost/ambiguous,
            # querying this id recovers that order instead of opening another.
            client_order_id = f"sa{uuid.uuid4().hex[:24]}"
            accepted: dict[str, Any] | None = None
            order_errors: list[str] = []
            for attempt in range(1, 4):
                try:
                    accepted = self.client.place_market(
                        inst_id, order_side, sizing["contracts"], pos_side,
                        client_order_id=client_order_id,
                    )
                    if accepted.get("sCode") not in (None, "", "0"):
                        raise RuntimeError(f"{accepted.get('sCode')} {accepted.get('sMsg')}")
                    break
                except Exception as order_exc:
                    order_errors.append(str(order_exc))
                    # Allow a delayed acceptance to reach the order-query API
                    # before sending the idempotent retry.
                    for _ in range(3):
                        time.sleep(0.4)
                        try:
                            accepted = self.client.order_by_client_id(inst_id, client_order_id)
                        except Exception:
                            accepted = None
                        if accepted:
                            break
                    if accepted:
                        break
                    if attempt == 3:
                        raise RuntimeError(
                            f"市价单连续 {attempt} 次失败；clOrdId={client_order_id}；"
                            f"交易所原因：{order_errors[-1]}"
                        ) from order_exc
                    LOG.warning("demo order attempt %s/3 failed for %s; retrying safely: %s", attempt, inst_id, order_exc)
                    time.sleep(float(attempt))
            if not accepted:
                raise RuntimeError(f"市价单未被交易所接受；clOrdId={client_order_id}")
            if accepted.get("sCode") not in (None, "", "0"):
                raise RuntimeError(f"{accepted.get('sCode')} {accepted.get('sMsg')}")
            order_id = str(accepted["ordId"])
            detail = {}
            for _ in range(5):
                time.sleep(0.4)
                detail = self.client.order(inst_id, order_id)
                if detail.get("state") in {"filled", "canceled"}:
                    break
            partial_fill_recovered = detail.get("state") == "partially_filled" and float(detail.get("accFillSz") or 0) > 0
            partial_cancel_error = ""
            if partial_fill_recovered:
                try:
                    self.client.cancel_order(inst_id, order_id)
                    for _ in range(6):
                        time.sleep(0.35)
                        detail = self.client.order(inst_id, order_id)
                        if detail.get("state") in {"filled", "canceled"}:
                            break
                except Exception as cancel_exc:
                    # The filled position must still be tracked and protected.
                    # ensure_runner_stop verifies its live size every minute.
                    partial_cancel_error = str(cancel_exc)
                    LOG.warning("partial remainder cancellation failed for %s: %s", inst_id, cancel_exc)
            if float(detail.get("accFillSz") or 0) <= 0:
                raise RuntimeError(f"订单没有任何成交：{detail.get('state') or 'unknown'}")
            fill_price = float(detail.get("avgPx") or detail.get("fillPx") or price)
            # Do not attach TP/SL to the initial market request.  Between the
            # 1m confirmation and exchange acceptance, a fast move can put a
            # precomputed target on the wrong side of the primary order (OKX
            # 51052).  Build OCO from the *actual* fill instead.
            actual_stop = fill_price - distance if direction == "LONG" else fill_price + distance
            actual_target = fill_price + distance * SCALE_OUT_R if direction == "LONG" else fill_price - distance * SCALE_OUT_R
            actual_stop_text = decimal_step(actual_stop, instrument.get("tickSz") or "0.0001", "nearest")
            actual_target_text = decimal_step(actual_target, instrument.get("tickSz") or "0.0001", "nearest")
            row.update(
                status="EXECUTED", status_reason="1m 突破、点差与滑点确认通过，模拟市价成交",
                updated_at=now_iso(), order_id=order_id, order_state=detail.get("state"),
                client_order_id=client_order_id, order_attempts=len(order_errors) + 1,
                fill_price=fill_price, fill_size=float(detail.get("accFillSz") or 0),
                stop_px=float(actual_stop_text), target_px=float(actual_target_text),
                initial_stop_px=float(actual_stop_text), active_stop_px=float(actual_stop_text),
                tick_size=instrument.get("tickSz") or "0.0001", pos_side=pos_side,
                filled_at=now_iso(), planned_stop_distance=distance, leverage=selected_leverage,
                leverage_reason=leverage_reason,
                partial_fill_recovered=partial_fill_recovered,
                partial_cancel_error=partial_cancel_error,
                exit_plan="SCALE_RUNNER", scale_out_fraction=SCALE_OUT_FRACTION,
                scale_out_r=SCALE_OUT_R, scale_out_done=False,
            )
            self.runtime.setdefault("history", []).append(dict(row))
            # The full position is protected by a stop.  Take-profit is
            # intentionally managed by the engine at 1.5R so the remaining
            # runner is not capped by a fixed exchange target.
            position = next((item for item in self.client.positions(inst_id)
                             if item.get("posSide") == pos_side and self._position_size(item) > 0), None)
            if not position or not self.ensure_runner_stop(self.runtime["history"][-1], position):
                self.save()
                return
            self.save()
            message = (
                f"📈 **OKX 模拟盘成交｜{inst_id}**\n\n"
                f"**方向**：{'做多' if direction == 'LONG' else '做空'}｜市价成交\n"
                f"**成交**：{row['fill_size']:g} 张 @ ${fill_price:,.4f}\n"
                f"**成交名义金额（仓位价值）**：${sizing['notional']:,.2f}\n"
                f"**实际占用保证金**：${float(position.get('imr') or 0):,.2f}｜杠杆 {selected_leverage}x｜风险预算 ${sizing['risk_budget']:,.2f}\n"
                f"**杠杆依据**：{leverage_reason}\n"
                f"**保护**：止损 ${float(actual_stop_text):,.4f}｜分批止盈 ${float(actual_target_text):,.4f}（40% @ {SCALE_OUT_R:g}R）\n"
                f"**依据**：10m 排序 → 5m 趋势/VWAP/量能复核 → 1m 突破/盘口确认\n\n"
                f"{today_pnl_line(self.client)}\n\n"
                f"**剩余资金**\n{account_line(self.client)}\n\n"
                f"**当前仓位**\n" + "\n".join(position_lines(self.client, inst_id))
            )
            notify(self.cfg, message)
        except Exception as exc:
            row.update(status="REJECTED", status_reason=f"模拟下单失败且未建立新仓：{exc}", updated_at=now_iso())
            LOG.exception("demo order failed for %s", inst_id)
            self.save()

    def on_message(self, ws: websocket.WebSocketApp, message: str) -> None:
        try:
            packet = json.loads(message)
            arg, rows = packet.get("arg", {}), packet.get("data") or []
            inst_id, channel = arg.get("instId"), arg.get("channel")
            if packet.get("event") == "error":
                LOG.warning("subscription rejected: %s %s", packet.get("code"), packet.get("msg"))
                return
            if not inst_id or not rows:
                return
            data = rows[0]
            if channel == "tickers":
                self.market["quotes"][inst_id] = data
            elif channel == "books5":
                self.market["books"][inst_id] = data
            elif channel in {"candle1m", "candle5m"}:
                self.market["candles"].setdefault(inst_id, {})[channel] = data
                if channel == "candle5m":
                    self.five_minute_review(inst_id, data)
                else:
                    self.one_minute_confirm(inst_id, data)
            self.reject_expired()
            self.save(force=False)
            if self.active_candidate_ids() != self.subscribed:
                ws.close()
        except Exception:
            LOG.exception("WebSocket message handling failed")

    def periodic_rest_candles(self) -> None:
        """Equity perpetuals expose real-time quote/book WS but not candle WS."""
        minute = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M")
        if minute == self.last_periodic_minute:
            return
        self.last_periodic_minute = minute
        for inst_id, row in list(self.runtime.get("candidates", {}).items()):
            if row.get("status") in TERMINAL:
                continue
            try:
                if row.get("status") == "DISCOVERED" or datetime.now(UTC).minute % 5 == 0:
                    five_rows = self.client.candles(inst_id, limit=80, bar="5m")
                    five_closed = next((item for item in five_rows if len(item) >= 9 and item[8] == "1"), None)
                    if five_closed:
                        self.market["candles"].setdefault(inst_id, {})["candle5m"] = five_closed
                        self.five_minute_review(inst_id, five_closed)
                if row.get("status") in {"FIVE_MIN_CONFIRMED", "READY"}:
                    one_rows = self.client.candles(inst_id, limit=40, bar="1m")
                    one_closed = next((item for item in one_rows if len(item) >= 9 and item[8] == "1"), None)
                    if one_closed:
                        self.market["candles"].setdefault(inst_id, {})["candle1m"] = one_closed
                        self.one_minute_confirm(inst_id, one_closed)
            except Exception as exc:
                row.update(status_reason=f"REST K 线复核暂时失败，下分钟重试：{exc}", updated_at=now_iso())

    def periodic_worker(self) -> None:
        while self.running:
            try:
                self.reject_expired()
                self.periodic_rest_candles()
                self.manage_positions_on_minute()
                self.save()
            except Exception:
                LOG.exception("periodic closed-candle review failed")
            time.sleep(1)

    def run(self) -> None:
        threading.Thread(target=self.periodic_worker, name="okx-closed-candles", daemon=True).start()
        while self.running:
            self.sync()
            inst_ids = list(self.active_candidate_ids())
            if not inst_ids:
                time.sleep(10)
                continue
            self.market = {"quotes": {}, "books": {}, "candles": {}}
            self.subscribed = set(inst_ids)

            def on_open(ws: websocket.WebSocketApp) -> None:
                args = [
                    {"channel": channel, "instId": inst_id}
                    for inst_id in inst_ids for channel in ("tickers", "books5")
                ]
                ws.send(json.dumps({"op": "subscribe", "args": args}))
                LOG.info("subscribed to %s staged candidates", len(inst_ids))

            proxy = urlparse(self.cfg.proxy_url or "")
            options: dict[str, Any] = {"ping_interval": 20, "ping_timeout": 8}
            if proxy.hostname:
                options.update(http_proxy_host=proxy.hostname, http_proxy_port=proxy.port or 80, proxy_type="http")
            try:
                self.ws = websocket.WebSocketApp(
                    "wss://ws.okx.com:8443/ws/v5/public",
                    on_open=on_open, on_message=self.on_message,
                    on_error=lambda _ws, error: LOG.warning("WebSocket error: %s", error),
                )
                self.ws.run_forever(**options)
            except Exception:
                LOG.exception("WebSocket loop failed")
            time.sleep(3)


def main() -> None:
    engine = Engine()
    process_signal.signal(process_signal.SIGTERM, lambda *_: engine.stop())
    process_signal.signal(process_signal.SIGINT, lambda *_: engine.stop())
    engine.run()


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("OKX_INTRADAY_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    main()
