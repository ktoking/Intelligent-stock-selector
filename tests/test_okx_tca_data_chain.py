from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from scripts import okx_candidate_ws, okx_gap_demo_executor
from scripts.okx_gap_shadow import (gap_decision_book_snapshot, gap_decision_mark_prices,
                                    mark_snapshot_for_symbol)


def _book() -> dict:
    return {
        "ts": "1770000000123",
        "bids": [["100.99", "10", "0", "2"], ["100.98", "7", "0", "1"]],
        "asks": [["101", "10", "0", "3"], ["101.01", "8", "0", "2"]],
    }


def test_shadow_signal_persists_real_books5_depth_and_sized_slippage(tmp_path, monkeypatch):
    database = tmp_path / "shadow.db"
    monkeypatch.setattr(okx_candidate_ws, "DB_PATH", database)
    snapshot = okx_candidate_ws.build_book_tca_snapshot(
        _book(), "LONG", 1.0, reference_notional_usdt=None, reference_contracts=15,
    )
    snapshot.update(decision_mark_px=100.95, mark_exchange_ts=1770000000456,
                    mark_snapshot_status="available", mark_snapshot_error=None)
    row = {
        "instId": "NVDA-USDT-SWAP", "side": "LONG", "experiment_id": "gap-v5",
        "horizon_minutes": 60, "decision_book_snapshot": snapshot, "microstructure": {},
    }

    okx_candidate_ws.record_shadow_signal(row, 1_000, 101.0, "GAP_FADE_V5_FORWARD")

    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        stored = conn.execute("SELECT * FROM okx_signal_shadow").fetchone()
    assert stored["book_snapshot_status"] == "available"
    assert stored["decision_price"] == 101.0
    assert stored["decision_bid_px"] == 100.99
    assert stored["decision_ask_px"] == 101.0
    assert stored["decision_mark_px"] == 100.95
    assert stored["mark_snapshot_status"] == "available"
    assert stored["bid_depth_contracts"] == 17.0
    assert stored["ask_depth_contracts"] == 18.0
    assert stored["slippage_reference_contracts"] == 15.0
    assert stored["slippage_reference_notional_usdt"] == pytest.approx(1515.0)
    assert stored["tca_model_version"] == "books5_same_side_impact_v1"
    assert stored["slippage_bps"] == pytest.approx((101.00333333333333 / 101 - 1) * 10_000)
    assert json.loads(stored["books5_json"])["asks"][0] == [101.0, 10.0, 3]


def test_shadow_signal_marks_missing_book_without_zero_or_fake_levels(tmp_path, monkeypatch):
    database = tmp_path / "shadow.db"
    monkeypatch.setattr(okx_candidate_ws, "DB_PATH", database)
    snapshot = okx_candidate_ws.unavailable_book_tca_snapshot("network timeout")
    snapshot.update(decision_mark_px=None, mark_exchange_ts=None,
                    mark_snapshot_status="snapshot_unavailable",
                    mark_snapshot_error="mark endpoint timeout")
    row = {
        "instId": "NVDA-USDT-SWAP", "side": "SHORT", "experiment_id": "gap-strict",
        "decision_book_snapshot": snapshot, "microstructure": {},
    }

    okx_candidate_ws.record_shadow_signal(
        row, 1_000, 100.99, "GAP_FADE_V5_STRICT_CONFIRM_FORWARD",
    )

    with sqlite3.connect(database) as conn:
        stored = conn.execute(
            "SELECT book_snapshot_status,book_snapshot_error,books5_json,slippage_bps,"
            "mark_snapshot_status,mark_snapshot_error,decision_mark_px "
            "FROM okx_signal_shadow"
        ).fetchone()
    assert stored == ("snapshot_unavailable", "network timeout", None, None,
                      "snapshot_unavailable", "mark endpoint timeout", None)


def test_shadow_schema_migrates_tca_columns_and_marks_legacy_rows(tmp_path, monkeypatch):
    database = tmp_path / "legacy-shadow.db"
    monkeypatch.setattr(okx_candidate_ws, "DB_PATH", database)
    with sqlite3.connect(database) as conn:
        conn.execute("""CREATE TABLE okx_signal_shadow (
            signal_key TEXT PRIMARY KEY, inst_id TEXT NOT NULL, side TEXT NOT NULL,
            stage TEXT NOT NULL, entry_ts INTEGER NOT NULL, due_ts INTEGER NOT NULL,
            entry_price REAL NOT NULL, horizon_minutes INTEGER NOT NULL DEFAULT 60,
            atr14 REAL, score REAL, volume_ratio REAL, spread_bps REAL, slippage_bps REAL,
            book_imbalance REAL, aggressive_imbalance REAL, micro_available INTEGER,
            exit_price REAL, gross_r REAL, net_r REAL, mfe_r REAL, mae_r REAL,
            labeled_at TEXT, created_at TEXT NOT NULL
        )""")
        conn.execute("""INSERT INTO okx_signal_shadow
            (signal_key,inst_id,side,stage,entry_ts,due_ts,entry_price,created_at)
            VALUES ('old','NVDA-USDT-SWAP','LONG','GAP_FADE_PASS',1,2,100,'old')""")

    okx_candidate_ws.ensure_shadow_schema()

    with sqlite3.connect(database) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(okx_signal_shadow)")}
        status = conn.execute(
            "SELECT book_snapshot_status,mark_snapshot_status FROM okx_signal_shadow"
        ).fetchone()
    assert {"books5_json", "decision_bid_px", "decision_mark_px", "slippage_status",
            "tca_model_version"}.issubset(columns)
    assert status == ("not_captured", "not_captured")


@pytest.mark.parametrize("fails", [False, True])
def test_gap_snapshot_makes_one_books5_request_and_never_fabricates(fails):
    class Client:
        calls = 0

        def request(self, *_args, **_kwargs):
            self.calls += 1
            if fails:
                raise RuntimeError("proxy down")
            return {"data": [_book()]}

    client = Client()
    snapshot = gap_decision_book_snapshot(client, "NVDA-USDT-SWAP", "LONG", 1.0)

    assert client.calls == 1
    assert snapshot["book_snapshot_status"] == ("snapshot_unavailable" if fails else "available")
    if fails:
        assert snapshot["estimated_slippage_bps"] is None
        assert "proxy down" in snapshot["book_snapshot_error"]


def test_gap_mark_price_uses_one_batch_and_marks_missing_symbol():
    class Client:
        calls = 0

        def request(self, *_args, **_kwargs):
            self.calls += 1
            return {"data": [{
                "instId": "NVDA-USDT-SWAP", "markPx": "101.25", "ts": "1770000000456",
            }]}

    client = Client()
    marks, error = gap_decision_mark_prices(client)

    assert client.calls == 1
    assert error is None
    assert mark_snapshot_for_symbol(marks, error, "NVDA-USDT-SWAP") == {
        "decision_mark_px": 101.25, "mark_exchange_ts": 1770000000456,
        "mark_snapshot_status": "available", "mark_snapshot_error": None,
    }
    missing = mark_snapshot_for_symbol(marks, error, "TSLA-USDT-SWAP")
    assert missing["decision_mark_px"] is None
    assert missing["mark_snapshot_status"] == "snapshot_unavailable"
    assert "TSLA-USDT-SWAP" in missing["mark_snapshot_error"]


class _DemoClient:
    def __init__(self) -> None:
        self.market_orders: list[dict] = []

    def request(self, method, path, params=None, private=False, **_kwargs):
        if path == "/api/v5/account/positions":
            return {"data": []}
        if path == "/api/v5/market/books":
            return {"data": [_book()]}
        if path == "/api/v5/trade/fills":
            order_id = str((params or {}).get("ordId") or "")
            return {"data": [{
                "ordId": order_id, "fillPx": "101.01", "fillSz": "5",
                "fee": "-0.12", "feeCcy": "USDT", "pnl": "0",
            }]}
        raise AssertionError((method, path, params, private))

    def ticker(self, _inst_id):
        return {"bidPx": "100.99", "askPx": "101"}

    def instrument(self, _inst_id):
        return {"ctVal": "1", "lotSz": "1", "minSz": "1", "tickSz": ".01"}

    def max_size(self, _inst_id):
        return {"maxBuy": "100"}

    def balance(self):
        return {"totalEq": "10000"}

    def set_leverage(self, *_args):
        return None

    def place_market(self, inst_id, side, size, pos_side, **kwargs):
        self.market_orders.append({"inst_id": inst_id, "side": side, "size": size,
                                   "pos_side": pos_side, **kwargs})
        return {"ordId": "entry-1", "sCode": "0"}

    def order(self, _inst_id, _order_id):
        return {"state": "filled", "avgPx": "101.01", "accFillSz": "5", "fee": "-9"}

    def place_stop_protection(self, *_args):
        return {"algoId": "protect-1", "sCode": "0"}

    def order_by_client_id(self, *_args):
        return None


def test_gap_demo_entry_persists_decision_fill_slippage_and_exchange_fee(tmp_path, monkeypatch):
    database = tmp_path / "demo.db"
    monkeypatch.setattr(okx_gap_demo_executor, "DB_PATH", database)
    monkeypatch.setattr(okx_gap_demo_executor, "enabled", lambda: True)
    monkeypatch.setattr(okx_gap_demo_executor.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(okx_gap_demo_executor, "notify", lambda *_args: None)
    monkeypatch.setattr(okx_gap_demo_executor, "today_pnl_line", lambda *_args: "pnl")
    monkeypatch.setattr(okx_gap_demo_executor, "risk_size", lambda *_args: {
        "contracts": "5", "contracts_float": 5.0, "ct_val": 1.0,
        "notional": 505.0, "risk_budget": 35.0,
    })
    okx_gap_demo_executor.ensure_schema()
    executor = okx_gap_demo_executor.Executor.__new__(okx_gap_demo_executor.Executor)
    executor.cfg = SimpleNamespace(profile="demo")
    executor.client = _DemoClient()

    executor._submit({
        "inst_id": "NVDA-USDT-SWAP", "side": "LONG", "liquidity_qualified": True,
        "atr_bps": 100, "decision_mark_px": 100.95,
        "decision_mark_exchange_ts": 1770000000456, "mark_snapshot_status": "available",
    }, 1_000)

    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        stored = conn.execute("SELECT * FROM okx_gap_demo_executions").fetchone()
    assert stored["status"] == "OPEN"
    assert stored["decision_price"] == 101.0
    assert stored["decision_bid_px"] == 100.99
    assert stored["decision_mark_px"] == 100.95
    assert stored["decision_mark_snapshot_status"] == "available"
    assert json.loads(stored["decision_books5_json"])["bids"][0] == [100.99, 10.0, 2]
    assert stored["estimated_slippage_bps"] == 0.0
    assert stored["decision_tca_model_version"] == "books5_same_side_impact_v1"
    assert stored["fill_price"] == 101.01
    assert stored["realized_slippage_bps"] == pytest.approx((101.01 / 101 - 1) * 10_000)
    assert stored["entry_fee"] == -0.12
    assert stored["entry_fee_ccy"] == "USDT"
    assert stored["entry_fee_status"] == "fills_endpoint"
    assert len(executor.client.market_orders) == 1


def test_gap_demo_schema_migrates_tca_columns(tmp_path, monkeypatch):
    database = tmp_path / "old.db"
    monkeypatch.setattr(okx_gap_demo_executor, "DB_PATH", database)
    with sqlite3.connect(database) as conn:
        conn.execute("""CREATE TABLE okx_gap_demo_executions (
            signal_key TEXT PRIMARY KEY, inst_id TEXT NOT NULL, side TEXT NOT NULL,
            entry_ts INTEGER NOT NULL, due_ts INTEGER NOT NULL, status TEXT NOT NULL,
            client_order_id TEXT NOT NULL, order_id TEXT, fill_size REAL, fill_price REAL,
            stop_price REAL, algo_id TEXT, close_order_id TEXT, exit_price REAL,
            realized_pnl REAL, close_reason TEXT, error TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")

    okx_gap_demo_executor.ensure_schema()

    with sqlite3.connect(database) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(okx_gap_demo_executions)")}
    assert {"decision_books5_json", "estimated_slippage_bps", "realized_slippage_bps",
            "decision_mark_px", "decision_tca_model_version", "entry_fee",
            "exit_fill_slippage_bps", "close_fee"}.issubset(columns)


def test_gap_demo_close_persists_exit_quote_slippage_and_fee(tmp_path, monkeypatch):
    database = tmp_path / "close.db"
    monkeypatch.setattr(okx_gap_demo_executor, "DB_PATH", database)
    monkeypatch.setattr(okx_gap_demo_executor.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(okx_gap_demo_executor, "notify", lambda *_args: None)
    monkeypatch.setattr(okx_gap_demo_executor, "today_pnl_line", lambda *_args: "pnl")
    okx_gap_demo_executor.ensure_schema()
    with sqlite3.connect(database) as conn:
        conn.execute("""INSERT INTO okx_gap_demo_executions
            (signal_key,inst_id,side,entry_ts,due_ts,status,client_order_id,created_at,updated_at)
            VALUES ('close-me','NVDA-USDT-SWAP','LONG',1,2,'OPEN','client','now','now')""")
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM okx_gap_demo_executions").fetchone()

    class CloseClient(_DemoClient):
        def place_market(self, *_args, **_kwargs):
            return {"ordId": "close-1", "sCode": "0"}

        def order(self, _inst_id, _order_id):
            return {"state": "filled", "avgPx": "100.98", "accFillSz": "5"}

    executor = okx_gap_demo_executor.Executor.__new__(okx_gap_demo_executor.Executor)
    executor.cfg = SimpleNamespace(profile="demo")
    executor.client = CloseClient()

    executor._close(row, {"posSide": "long", "pos": "5"}, "test exit")

    with sqlite3.connect(database) as conn:
        stored = conn.execute(
            "SELECT status,exit_decision_price,exit_price,exit_fill_slippage_bps,"
            "close_fee,close_fee_status FROM okx_gap_demo_executions"
        ).fetchone()
    assert stored[0] == "CLOSED"
    assert stored[1] == 100.99
    assert stored[2] == 100.98
    assert stored[3] == pytest.approx((100.98 / 100.99 - 1) * -10_000)
    assert stored[4:] == (-0.12, "fills_endpoint")
