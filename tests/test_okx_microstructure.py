import json
import sqlite3

import pytest

from scripts import okx_candidate_ws
from scripts import okx_shadow_labeler as shadow_labeler
from scripts.okx_microstructure_collector import Collector
from scripts.okx_shadow_labeler import (Labeler, _stats, _v5_forward_gate_checks,
                                        _v5_symbol_edge_sizing_stats)
from scripts.okx_return_shadow import score_rows
from scripts import okx_intraday_agent
from scripts.okx_intraday_agent import OKX
from scripts.okx_gap_shadow import (EXECUTION_EQUIVALENT, EXPERIMENT_ID, V5_FROZEN_AT,
                                    V5_STRICT_CONFIRM_FROZEN_AT, gap_bps, gap_context,
                                    next_evaluation_at, shadow_market_data_symbols, top_ranked,
                                    strategy_lanes, v5_ranked_candidates, v5_strict_first5_subset)
from scripts.okx_barrier_model_research import barrier_targets
from scripts.okx_microstructure_report import executable_values
from scripts.okx_microstructure_model import barrier_outcome, try_freeze
from scripts import okx_microstructure_model as micro_model
from scripts import okx_microstructure_executor as micro_executor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pandas as pd


def test_shadow_stats_excludes_non_finite_outcomes():
    value = _stats([{"net_r": 1.0}, {"net_r": float("-inf")}])
    assert value["samples"] == 1
    assert value["expectancy_r"] == 1.0
    assert value["invalid_samples"] == 1


def test_dashboard_json_finite_sanitizes_nested_non_finite_values():
    assert okx_intraday_agent._json_finite({"x": [float("inf"), 1.0]}) == {"x": [None, 1.0]}


def test_micro_executor_risk_size_is_equity_and_portfolio_capped():
    sizing = micro_executor.risk_size(
        equity=10_000, gross_notional=6_000, entry=100,
        instrument={"ctVal": "1", "lotSz": ".01", "minSz": ".01"}, stop_bps=50,
    )
    assert sizing["notional"] == 2000
    assert sizing["risk_budget"] == 35


def test_micro_executor_protection_failure_immediately_reduce_only_exits(tmp_path, monkeypatch):
    database = tmp_path / "execution.db"
    monkeypatch.setattr(micro_executor, "DB_PATH", database)
    monkeypatch.setattr(micro_executor, "notify", lambda *_: None)
    micro_executor.ensure_schema()
    with sqlite3.connect(database) as conn:
        conn.execute("""CREATE TABLE okx_micro_model_signals (
            signal_key TEXT PRIMARY KEY, experiment_id TEXT, inst_id TEXT, side TEXT,
            entry_minute INTEGER, due_minute INTEGER, entry_price REAL, predicted_r REAL,
            cost_bps REAL)""")
        conn.execute("INSERT INTO okx_micro_model_signals VALUES (?,?,?,?,?,?,?,?,?)",
                     ("sig-1", "exp-1", "NVDA-USDT-SWAP", "LONG", 1000, 1900, 100, .3, 14))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM okx_micro_model_signals").fetchone()

    class Client:
        def __init__(self):
            self.market_calls = []
            self.live_positions = []
        def request(self, *_args, **_kwargs):
            if len(_args) > 1 and _args[1] == "/api/v5/market/books":
                return {"data": [{"bids": [["99.99", "100"]], "asks": [["100", "100"]]}]}
            if len(_args) > 1 and _args[1] == "/api/v5/account/positions":
                return {"data": self.live_positions}
            if len(_args) > 1 and _args[1] == "/api/v5/account/trade-fee":
                return {"data": [{"taker": "-0.0005"}]}
            return {"data": []}
        def instrument(self, _inst):
            return {"ctVal": "1", "lotSz": ".01", "minSz": ".01", "tickSz": ".01"}
        def ticker(self, _inst):
            return {"askPx": "100", "bidPx": "99.99"}
        def balance(self):
            return {"totalEq": "10000"}
        def set_leverage(self, *_args):
            return None
        def place_market(self, _inst, side, size, _pos_side, **kwargs):
            self.market_calls.append((side, size, kwargs.get("reduce_only")))
            return {"ordId": "close-1" if kwargs.get("reduce_only") else "open-1", "sCode": "0"}
        def order(self, _inst, order_id):
            return {"state": "filled", "accFillSz": "20", "avgPx": "100", "ordId": order_id}
        def order_by_client_id(self, *_args):
            return None
        def place_oco_protection(self, *_args):
            raise RuntimeError("injected protection failure")

    executor = micro_executor.Executor.__new__(micro_executor.Executor)
    executor.client, executor.cfg = Client(), object()
    executor.execute_signal(row, {"experiment_id": "exp-1", "config": {
        "stop_bps": 50, "target_r": 1.5, "horizon": 15,
    }})

    with sqlite3.connect(database) as conn:
        status, error = conn.execute(
            "SELECT status,error FROM okx_micro_executions WHERE signal_key='sig-1'"
        ).fetchone()
    assert status == "EMERGENCY_EXIT"
    assert "保护单挂单失败" in error
    assert executor.client.market_calls == [("buy", "20", False), ("sell", "20", True)]

    with sqlite3.connect(database) as conn:
        conn.execute("INSERT INTO okx_micro_model_signals VALUES (?,?,?,?,?,?,?,?,?)",
                     ("sig-2", "exp-1", "TSLA-USDT-SWAP", "LONG", 2000, 2900, 90, .3, 14))
        conn.row_factory = sqlite3.Row
        drifted = conn.execute("SELECT * FROM okx_micro_model_signals WHERE signal_key='sig-2'").fetchone()
    executor.execute_signal(drifted, {"experiment_id": "exp-1", "config": {
        "stop_bps": 50, "target_r": 1.5, "horizon": 15,
    }})
    with sqlite3.connect(database) as conn:
        status, drift = conn.execute(
            "SELECT status,decision_drift_bps FROM okx_micro_executions WHERE signal_key='sig-2'"
        ).fetchone()
    assert status == "SKIPPED" and drift > 5
    assert executor.client.market_calls == [("buy", "20", False), ("sell", "20", True)]

    with sqlite3.connect(database) as conn:
        conn.execute("INSERT INTO okx_micro_model_signals VALUES (?,?,?,?,?,?,?,?,?)",
                     ("sig-3", "exp-1", "GOOGL-USDT-SWAP", "LONG", 3000, 3900, 100, .3, 14))
        conn.row_factory = sqlite3.Row
        expensive = conn.execute(
            "SELECT * FROM okx_micro_model_signals WHERE signal_key='sig-3'"
        ).fetchone()
    executor._fee_cached_bps = 10
    executor._fee_cached_at = __import__("time").time()
    executor.execute_signal(expensive, {"experiment_id": "exp-1", "config": {
        "stop_bps": 50, "target_r": 1.5, "horizon": 15,
    }})
    with sqlite3.connect(database) as conn:
        status, error = conn.execute(
            "SELECT status,error FROM okx_micro_executions WHERE signal_key='sig-3'"
        ).fetchone()
    assert status == "SKIPPED" and "高于模型冻结成本" in error
    assert executor.client.market_calls == [("buy", "20", False), ("sell", "20", True)]

    with sqlite3.connect(database) as conn:
        conn.execute("""INSERT INTO okx_micro_executions
            (signal_key,experiment_id,inst_id,side,entry_minute,due_minute,status,
             client_order_id,order_id,fill_size,created_at,updated_at)
            VALUES ('recover','exp-1','MSFT-USDT-SWAP','LONG',3000,3900,
                    'FILLED_UNPROTECTED','recover-id','recover-order',5,'now','now')""")
    executor.client.live_positions = [{"instId": "MSFT-USDT-SWAP", "posSide": "long", "pos": "5"}]
    executor.reconcile_incomplete()
    with sqlite3.connect(database) as conn:
        status = conn.execute(
            "SELECT status FROM okx_micro_executions WHERE signal_key='recover'"
        ).fetchone()[0]
    assert status == "EMERGENCY_EXIT"
    assert executor.client.market_calls[-1] == ("sell", "5", True)


def test_book_features_use_five_levels_and_microprice():
    collector = Collector.__new__(Collector)
    result = collector._book({
        "bids": [["100.0", "4"], ["99.9", "3"]],
        "asks": [["100.2", "1"], ["100.3", "2"]],
    })

    assert result["bid_px"] == 100.0
    assert result["ask_px"] == 100.2
    assert result["bid_depth"] == 7.0
    assert result["ask_depth"] == 3.0
    assert abs(result["book_imbalance"] - 0.4) < 1e-9
    assert abs(result["spread_bps"] - (0.2 / 100.1 * 10_000)) < 1e-9
    assert abs(result["microprice"] - 100.16) < 1e-9


def test_book_features_handle_empty_book():
    collector = Collector.__new__(Collector)
    result = collector._book({})

    assert all(value == 0.0 for value in result.values())


def test_order_flow_imbalance_counts_quote_add_cancel_and_price_moves():
    previous = {"bid_px": 100, "ask_px": 101, "best_bid_size": 5, "best_ask_size": 4}
    same_prices = {"bid_px": 100, "ask_px": 101, "best_bid_size": 8, "best_ask_size": 2}
    assert Collector._ofi(previous, same_prices) == 5  # +3 bid and -2 ask
    bid_up = {"bid_px": 100.5, "ask_px": 101, "best_bid_size": 6, "best_ask_size": 2}
    assert Collector._ofi(same_prices, bid_up) == 6


def test_collector_tracks_intraminute_executable_quote_extrema():
    collector = Collector.__new__(Collector)
    collector.lock = __import__("threading").Lock()
    collector.flow, collector.books = {}, {}
    collector.last_message_at = ""
    for bid, ask in (("100", "100.2"), ("99.5", "101.0"), ("100.4", "100.6")):
        collector.on_message(None, json.dumps({
            "arg": {"instId": "NVDA-USDT-SWAP", "channel": "books5"},
            "data": [{"bids": [[bid, "10"]], "asks": [[ask, "10"]]}],
        }))
    flow = collector.flow["NVDA-USDT-SWAP"]
    assert flow["min_bid_px"] == 99.5 and flow["max_bid_px"] == 100.4
    assert flow["min_ask_px"] == 100.2 and flow["max_ask_px"] == 101.0


def test_microstructure_flush_backfills_forward_returns(tmp_path, monkeypatch):
    from scripts import okx_microstructure_collector as module
    monkeypatch.setattr(module, "DB_PATH", tmp_path / "micro.db")
    monkeypatch.setattr(module, "STATE_PATH", tmp_path / "micro.json")
    collector = Collector.__new__(Collector)
    collector.symbols = ["NVDA-USDT-SWAP"]
    collector.lock = __import__("threading").Lock()
    collector.books = {"NVDA-USDT-SWAP": {"bid_px": 99.9, "ask_px": 100.1}}
    collector.flow = {}
    collector.last_message_at = ""
    collector.connected_at = 1
    collector.contract_values = {"NVDA-USDT-SWAP": 1}
    collector.demo_tradeable = {"NVDA-USDT-SWAP": True}
    collector.taker_fee_bps = {"NVDA-USDT-SWAP": 5}
    collector._init_db()
    clock = [1_000_080]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    collector.minute = 1_000_020
    collector.flush()
    collector.books = {"NVDA-USDT-SWAP": {"bid_px": 100.9, "ask_px": 101.1}}
    collector.minute = 1_000_320
    clock[0] = 1_000_380
    collector.flush()

    with sqlite3.connect(module.DB_PATH) as conn:
        value = conn.execute("SELECT forward_5m_bps FROM okx_microstructure_minute ORDER BY minute_ts LIMIT 1").fetchone()[0]
    assert round(value, 6) == 100.0


def test_taker_fee_snapshot_uses_private_account_rate_and_generic_fallback(monkeypatch):
    from scripts import okx_microstructure_collector as module
    calls = []

    class Client:
        def __init__(self, _cfg):
            pass

        def request(self, method, path, params, private=False):
            calls.append((method, path, params, private))
            if path.endswith("/instruments"):
                return {"data": [{"instId": "NVDA-USDT-SWAP", "groupId": "7"}]}
            return {"data": [{"taker": "-0.0005", "feeGroup": [
                {"groupId": "7", "taker": "-0.0006"},
            ]}]}

    monkeypatch.setattr(module, "OKX", Client)
    collector = Collector.__new__(Collector)
    collector.cfg = object()
    collector.symbols = ["NVDA-USDT-SWAP", "TSLA-USDT-SWAP", "QQQ-USDT-SWAP"]
    collector.demo_tradeable = {
        "NVDA-USDT-SWAP": True, "TSLA-USDT-SWAP": True, "QQQ-USDT-SWAP": False,
    }
    fees = collector._taker_fee_bps()
    assert {symbol: round(value, 6) for symbol, value in fees.items()} == {
        "NVDA-USDT-SWAP": 6.0, "TSLA-USDT-SWAP": 5.0, "QQQ-USDT-SWAP": 0.0,
    }
    assert len(calls) == 2 and all(call[3] is True for call in calls)


def test_micro_report_uses_contract_depth_and_max_five_concurrent_rows():
    rows = []
    for index in range(7):
        rows.append({
            "minute_ts": 1_000_200, "inst_id": f"S{index}", "trade_count": 10,
            "capture_complete": 1, "book_age_seconds": 1, "book_imbalance": .4,
            "aggressive_imbalance": .3, "ask_depth": 100, "bid_depth": 100,
            "bid_px": 99.9, "ask_px": 100.1, "spread_bps": 2,
            "ct_val": 1 if index < 6 else .01, "forward_5m_bps": 30,
        })
    values, opportunities, trading_days = executable_values(rows, 5, lambda _: True)
    assert len(values) == 5
    assert opportunities == 1
    assert trading_days == 1


def test_micro_report_tolerates_rows_created_before_schema_migration():
    values, opportunities, trading_days = executable_values([{"forward_5m_bps": 1}], 5, lambda _: True)
    assert (values, opportunities, trading_days) == ([], 0, 0)


def test_micro_barrier_uses_executable_quotes_and_real_stop_path():
    row = {"inst_id": "NVDA-USDT-SWAP", "minute_ts": 1_000_200,
           "bid_px": 99.9, "ask_px": 100.1, "taker_fee_bps": 5}
    quotes = {("NVDA-USDT-SWAP", 1_000_260): (99.0, 99.2)}
    value = barrier_outcome(row, quotes, "LONG", stop_bps=50, target_r=1.5, horizon=1)
    assert value is not None and value < -1


def test_micro_barrier_cost_uses_frozen_account_fee_snapshot():
    row = {"inst_id": "NVDA-USDT-SWAP", "minute_ts": 1_000_200,
           "bid_px": 99.9, "ask_px": 100.1, "taker_fee_bps": 5}
    quotes = {("NVDA-USDT-SWAP", 1_000_260): (99.0, 99.2)}
    low_fee = barrier_outcome(row, quotes, "LONG", stop_bps=50, target_r=1.5, horizon=1)
    high_fee = barrier_outcome({**row, "taker_fee_bps": 10}, quotes, "LONG",
                               stop_bps=50, target_r=1.5, horizon=1)
    assert low_fee is not None and high_fee is not None
    assert round(low_fee - high_fee, 6) == .2


def test_micro_barrier_uses_intraminute_extrema_and_stop_first_when_ambiguous():
    row = {"inst_id": "NVDA-USDT-SWAP", "minute_ts": 1_000_200,
           "bid_px": 99.9, "ask_px": 100.1, "taker_fee_bps": 5}
    # The minute closes near entry, but both stop and target were touched intraminute.
    quotes = {("NVDA-USDT-SWAP", 1_000_260): (100.0, 100.2, 99.5, 101.0, 99.7, 101.2)}
    value = barrier_outcome(row, quotes, "LONG", stop_bps=50, target_r=1.5, horizon=1)
    assert value is not None and value < -1


def test_micro_features_exclude_public_but_demo_untradeable_symbols():
    ny = ZoneInfo("America/New_York")
    start = int(datetime(2026, 7, 20, 9, 30, tzinfo=ny).timestamp())
    rows = []
    for symbol, tradeable in (("NVDA-USDT-SWAP", 1), ("QQQ-USDT-SWAP", 0),
                              ("SAMSUNG-USDT-SWAP", 1)):
        for offset in (0, 60):
            rows.append({"minute_ts": start + offset, "inst_id": symbol,
                         "bid_px": 99.99, "ask_px": 100.01, "ct_val": 1,
                         "taker_fee_bps": 5,
                         "demo_tradeable": tradeable, "capture_complete": 1,
                         "book_age_seconds": 1, "trade_count": 10, "spread_bps": 2,
                         "bid_depth": 100, "ask_depth": 100, "book_imbalance": .3,
                         "aggressive_imbalance": .2, "microprice": 100,
                         "buy_volume": 6, "sell_volume": 4,
                         "depth_normalized_ofi": .1, "quote_updates": 20})
    frame = micro_model.feature_frame(rows)
    assert set(frame["inst_id"]) == {"NVDA-USDT-SWAP"}


def test_micro_lags_never_cross_market_data_gaps():
    ny = ZoneInfo("America/New_York")
    start = int(datetime(2026, 7, 20, 9, 30, tzinfo=ny).timestamp())
    rows = []
    for offset, book in ((0, .1), (120, .4), (180, .7)):
        rows.append({"minute_ts": start + offset, "inst_id": "NVDA-USDT-SWAP",
                     "bid_px": 99.99, "ask_px": 100.01, "ct_val": 1,
                     "taker_fee_bps": 5, "demo_tradeable": 1, "capture_complete": 1,
                     "book_age_seconds": 1, "trade_count": 10, "spread_bps": 2,
                     "bid_depth": 100, "ask_depth": 100, "book_imbalance": book,
                     "aggressive_imbalance": .2, "microprice": 100,
                     "buy_volume": 6, "sell_volume": 4,
                     "depth_normalized_ofi": .1, "quote_updates": 20})
    frame = micro_model.feature_frame(rows)
    assert frame["minute_ts"].tolist() == [start + 180]
    assert frame.iloc[0]["book_lag1"] == .4


def test_micro_model_does_not_freeze_before_ten_cash_days():
    frame = pd.DataFrame([{"date": f"2026-07-{day:02d}", "minute_ts": day * 100_000,
                           "inst_id": "S"} for day in range(1, 10)])
    state = try_freeze([], frame)
    assert state["research_phase"] == "collect_cash_development"
    assert state["cash_development_days"] == 0
    assert len(state["partial_cash_days_excluded"]) == 9
    assert state["research_version"] == "micro_barrier_cash_horizon_v13"


def test_micro_cash_horizon_never_extends_past_regular_close():
    ny = ZoneInfo("America/New_York")
    at = lambda hour, minute: int(datetime(2026, 7, 20, hour, minute, tzinfo=ny).timestamp())
    assert micro_model.cash_horizon_complete(at(15, 29), 30)
    assert not micro_model.cash_horizon_complete(at(15, 30), 30)
    assert micro_model.cash_horizon_complete(at(15, 44), 15)
    assert not micro_model.cash_horizon_complete(at(15, 45), 15)
    assert micro_model.entry_horizon_complete(at(15, 45), 15)
    assert not micro_model.entry_horizon_complete(at(15, 46), 15)


def test_cash_day_diagnostics_explain_incomplete_sessions():
    rows = [
        {"date": "2026-07-20", "minute_ts": minute * 60, "inst_id": f"S{symbol}"}
        for minute in [*range(301), 380] for symbol in range(10)
    ]
    rows += [
        {"date": "2026-07-21", "minute_ts": 100_000 + minute * 60,
         "inst_id": f"S{minute % 10}"}
        for minute in range(100)
    ]
    diagnostics = micro_model.cash_day_diagnostics(pd.DataFrame(rows))
    assert diagnostics[0]["qualified"] is True
    assert diagnostics[0]["valid_minutes"] == 302
    assert diagnostics[0]["span_minutes"] == 380
    assert diagnostics[1]["qualified"] is False
    assert diagnostics[0]["dense_symbols"] == 10
    assert diagnostics[1]["checks"] == {"minutes": False, "span": False, "dense_symbols": False}


def test_micro_model_advances_to_non_overlapping_development_cohort(monkeypatch):
    days = [f"2026-06-{day:02d}" for day in range(1, 21)]
    frame = pd.DataFrame({"date": days, "minute_ts": range(20), "inst_id": ["S"] * 20})
    monkeypatch.setattr(micro_model, "qualified_cash_days", lambda _frame: days)
    monkeypatch.setattr(micro_model, "executable_opportunities", lambda _frame: pd.DataFrame())
    empty = pd.DataFrame(columns=["date", *micro_model.FEATURES, "long_net_r", "short_net_r"])
    monkeypatch.setattr(micro_model, "labeled_frame", lambda *_args: empty)

    first = micro_model.try_freeze([], frame, cohort_index=0)
    assert first["research_phase"] == "collect_next_development_cohort"
    assert first["development_days"] == days[:10]
    assert first["next_attempt_after_cash_days"] == 20
    assert len(first["selection_attempts"]) == 1

    second = micro_model.try_freeze([], frame, cohort_index=1, prior_attempts=first["selection_attempts"])
    assert second["development_days"] == days[10:20]
    assert len(second["selection_attempts"]) == 2
    assert second["selection_attempts"][0]["development_days"] == days[:10]


def test_micro_experiment_fingerprint_binds_training_days_and_cutoff():
    selected = {"stop_bps": 50, "target_r": 1.5, "horizon": 15,
                "model": "ridge", "threshold": .1}
    first = micro_model.experiment_fingerprint(selected, ["2026-06-01"], 100, "digest-a")
    assert first != micro_model.experiment_fingerprint(selected, ["2026-06-02"], 100, "digest-a")
    assert first != micro_model.experiment_fingerprint(selected, ["2026-06-01"], 101, "digest-a")
    assert first != micro_model.experiment_fingerprint(selected, ["2026-06-01"], 100, "digest-b")


def test_micro_artifact_compatibility_rejects_stale_or_tampered_model():
    selected = {"stop_bps": 50, "target_r": 1.5, "horizon": 15,
                "model": "ridge", "threshold": .1}
    days, cutoff = ["2026-06-01"], 100
    experiment_id = "micro_barrier_" + micro_model.experiment_fingerprint(selected, days, cutoff, "digest")
    artifact = {"research_version": micro_model.RESEARCH_VERSION,
                "features": micro_model.FEATURES, "config": selected,
                "development_days": days, "training_data_end": cutoff,
                "training_data_digest": "digest",
                "experiment_id": experiment_id,
                "experiment_started_at": "2026-07-01T00:00:00+00:00"}
    assert micro_model.artifact_compatible(artifact)
    assert not micro_model.artifact_compatible({**artifact, "research_version": "old"})
    assert not micro_model.artifact_compatible({**artifact, "experiment_id": "tampered"})


def test_micro_model_freeze_forward_signal_and_label_lifecycle(tmp_path, monkeypatch):
    database = tmp_path / "micro-model.db"
    artifact_path = tmp_path / "micro-model.joblib"
    state_path = tmp_path / "micro-model.json"
    monkeypatch.setattr(micro_model, "DB_PATH", database)
    monkeypatch.setattr(micro_model, "ARTIFACT_PATH", artifact_path)
    monkeypatch.setattr(micro_model, "STATE_PATH", state_path)
    monkeypatch.setattr(micro_model, "DEV_STOP_BPS", (50,))
    monkeypatch.setattr(micro_model, "DEV_TARGET_R", (1.5,))
    monkeypatch.setattr(micro_model, "DEV_HORIZONS", (15,))
    monkeypatch.setattr(micro_model, "DEV_MODELS", ("ridge",))
    monkeypatch.setattr(micro_model, "DEV_THRESHOLDS", (.05,))
    monkeypatch.setattr(micro_model, "MIN_QUALIFIED_MINUTES", 60)
    monkeypatch.setattr(micro_model, "MIN_QUALIFIED_SPAN_MINUTES", 60)
    monkeypatch.setattr(micro_model, "MIN_QUALIFIED_SYMBOLS", 5)
    monkeypatch.setattr(micro_model, "MIN_MINUTES_PER_SYMBOL", 60)
    micro_model.ensure_schema()

    ny = ZoneInfo("America/New_York")
    weekdays = [datetime(2026, 6, day, 9, 30, tzinfo=ny) for day in range(1, 15)
                if datetime(2026, 6, day, tzinfo=ny).weekday() < 5][:10]
    feature_rows, quote_rows = [], []
    for session in weekdays:
        date = session.date().isoformat()
        for symbol_index in range(10):
            direction = 1 if symbol_index < 5 else -1
            symbol = f"S{symbol_index}-USDT-SWAP"
            for minute in range(76):
                stamp = int(session.timestamp()) + minute * 60
                mid = 100 * (1 + direction * .001 * minute)
                bid, ask = mid - .01, mid + .01
                values = {
                    "minute_ts": stamp, "date": date, "inst_id": symbol,
                    "bid_px": bid, "ask_px": ask, "mid": mid, "ct_val": 1,
                    "taker_fee_bps": 5,
                    "spread_bps": 2, "bid_depth_usd": 20_000, "ask_depth_usd": 20_000,
                    "book_imbalance": .8 * direction, "aggressive_imbalance": .7 * direction,
                    "flow_interaction": .56, "microprice_offset_bps": .5 * direction,
                    "log_trade_count": 3, "log_flow_volume": 4,
                    "log_bid_depth_usd": 10, "log_ask_depth_usd": 10,
                    "book_lag1": .8 * direction, "aggressive_lag1": .7 * direction,
                    "depth_normalized_ofi": .6 * direction,
                    "ofi_aggressive_interaction": .42,
                    "log_quote_updates": 5, "ofi_lag1": .6 * direction,
                    "mid_return_1m_bps": 10 * direction, "flow_persistence": .64,
                    "minute_sin": 0, "minute_cos": 1,
                }
                feature_rows.append(values)
                quote_rows.append({"inst_id": symbol, "minute_ts": stamp, "bid_px": bid, "ask_px": ask,
                                   "capture_complete": 1, "book_age_seconds": 1})
            for minute in range(76, 91):
                stamp = int(session.timestamp()) + minute * 60
                mid = 100 * (1 + direction * .001 * minute)
                quote_rows.append({"inst_id": symbol, "minute_ts": stamp,
                                   "bid_px": mid - .01, "ask_px": mid + .01,
                                   "capture_complete": 1, "book_age_seconds": 1})
    frame = pd.DataFrame(feature_rows)
    frozen = micro_model.try_freeze(quote_rows, frame)
    assert frozen["research_phase"] == "frozen_forward"
    assert artifact_path.exists()
    artifact = __import__("joblib").load(artifact_path)
    selected_labeled = micro_model.labeled_frame(
        frame, micro_model.quote_lookup(quote_rows), artifact["config"]["stop_bps"],
        artifact["config"]["target_r"], artifact["config"]["horizon"],
    )
    assert artifact["training_data_digest"] == micro_model.training_digest(selected_labeled)

    # The forward experiment must start after the artifact was frozen.  Keep
    # this fixture time-relative so advancing the wall clock cannot turn its
    # intended out-of-sample session into pre-experiment data.
    frozen_at = datetime.fromisoformat(artifact["experiment_started_at"].replace("Z", "+00:00")).astimezone(ny)
    forward_session = (frozen_at + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    while forward_session.weekday() >= 5:
        forward_session = (forward_session + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    forward_stamp = int(forward_session.timestamp())
    forward_rows = []
    for symbol_index in range(10):
        direction = 1 if symbol_index < 5 else -1
        source = feature_rows[symbol_index * 76].copy()
        source.update(minute_ts=forward_stamp, date=forward_session.date().isoformat(), inst_id=f"S{symbol_index}-USDT-SWAP",
                      bid_px=99.99, ask_px=100.01, mid=100,
                      book_imbalance=.8 * direction, aggressive_imbalance=.7 * direction,
                      microprice_offset_bps=.5 * direction, book_lag1=.8 * direction,
                      aggressive_lag1=.7 * direction, mid_return_1m_bps=10 * direction)
        forward_rows.append(source)
    inserted = micro_model.generate_forward(artifact, pd.DataFrame(forward_rows))
    assert inserted == 5
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        signals = conn.execute("SELECT * FROM okx_micro_model_signals").fetchall()
    quotes = {}
    for row in signals:
        direction = 1 if row["side"] == "LONG" else -1
        entry = float(row["entry_price"])
        for minute in range(15):
            executable = entry * (1 + direction * .001 * (minute + 1))
            quotes[(row["inst_id"], int(row["entry_minute"]) + minute * 60)] = (
                (executable if direction > 0 else executable - .02),
                (executable + .02 if direction > 0 else executable),
            )
    monkeypatch.setattr(micro_model.time, "time", lambda: forward_stamp + 16 * 60)
    assert micro_model.label_pending(artifact, quotes) == 5
    stats = micro_model.signal_stats(artifact["experiment_id"])
    assert stats["samples"] == 5
    assert stats["expectancy_r"] > 0

    live_artifact = {**artifact, "experiment_id": artifact["experiment_id"] + "_live"}
    live_stamp = forward_stamp + 300
    live_rows = pd.DataFrame([{**row, "minute_ts": live_stamp} for row in forward_rows])
    inserted = micro_model.generate_forward(
        live_artifact, live_rows, live_quote_provider=lambda _inst: (123, 123.01),
        decision_ts=live_stamp + 60, max_feature_age_seconds=120,
    )
    assert inserted == 5
    with sqlite3.connect(database) as conn:
        live = conn.execute("""SELECT DISTINCT feature_minute,entry_minute,entry_price,entry_quote_source,cost_bps
                               FROM okx_micro_model_signals WHERE experiment_id=?""",
                            (live_artifact["experiment_id"],)).fetchall()
    assert {row[0] for row in live} == {live_stamp}
    assert {row[1] for row in live} == {live_stamp + 60}
    assert {row[2] for row in live}.issubset({123.0, 123.01})
    assert {row[3] for row in live} == {"okx_rest_ticker"}
    assert {row[4] for row in live} == {14.0}

    stale_artifact = {**artifact, "experiment_id": artifact["experiment_id"] + "_stale"}
    assert micro_model.generate_forward(
        stale_artifact, live_rows, live_quote_provider=lambda _inst: (123, 123.01),
        decision_ts=live_stamp + 180, max_feature_age_seconds=120,
    ) == 0

    delayed_artifact = {**artifact, "experiment_id": artifact["experiment_id"] + "_delayed"}
    assert micro_model.generate_forward(
        delayed_artifact, live_rows, live_quote_provider=lambda _inst: (123, 123.01),
        decision_ts=live_stamp + 120, max_feature_age_seconds=120,
    ) == 0


def test_micro_forward_stats_expose_data_gap_rate(tmp_path, monkeypatch):
    database = tmp_path / "micro-gaps.db"
    monkeypatch.setattr(micro_model, "DB_PATH", database)
    micro_model.ensure_schema()
    values = [
        ("ok", "exp", "S", "LONG", 1000, 1060, 100, 99, 101, 1, .2,
         1060, 101, 1.0, "target", "now", "now"),
        ("gap", "exp", "S2", "LONG", 1000, 1060, 100, 99, 101, 1, .2,
         None, None, None, "data_gap", "now", "now"),
    ]
    with sqlite3.connect(database) as conn:
        conn.executemany("""INSERT INTO okx_micro_model_signals
            (signal_key,experiment_id,inst_id,side,entry_minute,due_minute,entry_price,
             stop_price,target_price,risk_price,predicted_r,exit_minute,exit_price,net_r,
             exit_reason,labeled_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)
    stats = micro_model.signal_stats("exp")
    assert stats["samples"] == 1
    assert stats["total_completed"] == 2
    assert stats["data_gap_samples"] == 1
    assert stats["data_gap_rate"] == .5


def test_micro_forward_profit_concentration_uses_daily_net_profit(tmp_path, monkeypatch):
    database = tmp_path / "micro-concentration.db"
    monkeypatch.setattr(micro_model, "DB_PATH", database)
    micro_model.ensure_schema()
    ny = ZoneInfo("America/New_York")
    day1 = int(datetime(2026, 7, 20, 10, 0, tzinfo=ny).timestamp())
    day2 = int(datetime(2026, 7, 21, 10, 0, tzinfo=ny).timestamp())
    rows = [("win", day1, 10.0001), ("loss", day1 + 60, -9.0), ("next", day2, 1.0)]
    with sqlite3.connect(database) as conn:
        conn.executemany("""INSERT INTO okx_micro_model_signals
            (signal_key,experiment_id,inst_id,side,entry_minute,due_minute,entry_price,
             stop_price,target_price,risk_price,predicted_r,exit_minute,net_r,
             exit_reason,labeled_at,created_at)
            VALUES (?,'exp','S','LONG',?,?,100,99,101,1,.2,?,?,'time','now','now')""",
            [(key, stamp, stamp + 60, stamp + 60, value) for key, stamp, value in rows])
    stats = micro_model.signal_stats("exp")
    assert stats["positive_trading_days"] == 2
    assert stats["best_day_profit_share"] > .5


def test_micro_forward_gate_rejects_concentrated_or_gappy_profit():
    healthy = {"samples": 100, "profit_factor": 1.21, "expectancy_r": .11,
               "opportunities": 20, "trading_days": 10, "positive_trading_days": 6,
               "best_day_profit_share": .49, "max_drawdown_r": 11.9, "data_gap_rate": .05}
    assert all(micro_model.forward_gate_checks(healthy).values())
    assert not micro_model.forward_gate_checks({**healthy, "best_day_profit_share": .8})[
        "best_day_profit_share_at_most_50pct"]
    assert not micro_model.forward_gate_checks({**healthy, "data_gap_rate": .051})[
        "data_gap_rate_at_most_5pct"]


def test_micro_development_scan_defers_full_database_reload(tmp_path, monkeypatch):
    state_path, artifact_path = tmp_path / "state.json", tmp_path / "missing.joblib"
    state_path.write_text(json.dumps({"research_version": micro_model.RESEARCH_VERSION,
                                      "research_phase": "collect_cash_development", "passed": False}))
    monkeypatch.setattr(micro_model, "STATE_PATH", state_path)
    monkeypatch.setattr(micro_model, "ARTIFACT_PATH", artifact_path)
    monkeypatch.setattr(micro_model.time, "time", lambda: 1000)
    monkeypatch.setattr(micro_model, "_rows", lambda *_: (_ for _ in ()).throw(AssertionError("full reload")))
    runner = micro_model.Runner.__new__(micro_model.Runner)
    runner.last_development_scan = 900
    state = runner.once()
    assert state["development_scan_deferred"] is True
    assert state["next_development_scan_at"].startswith("1970-01-01T00:25:00")


def test_pending_signal_extends_frozen_model_lookback(tmp_path, monkeypatch):
    database = tmp_path / "pending.db"
    monkeypatch.setattr(micro_model, "DB_PATH", database)
    micro_model.ensure_schema()
    with sqlite3.connect(database) as conn:
        conn.execute("""INSERT INTO okx_micro_model_signals
            (signal_key,experiment_id,inst_id,side,entry_minute,due_minute,entry_price,
             stop_price,target_price,risk_price,predicted_r,created_at)
            VALUES ('pending','exp','S','LONG',100,200,10,9,11,1,.2,'now')""")
        conn.execute("""INSERT INTO okx_micro_model_signals
            (signal_key,experiment_id,inst_id,side,entry_minute,due_minute,entry_price,
             stop_price,target_price,risk_price,predicted_r,labeled_at,created_at)
            VALUES ('done','exp','S2','LONG',50,100,10,9,11,1,.2,'now','now')""")
    assert micro_model.pending_lookback_start("exp", 500) == 100
    assert micro_model.pending_lookback_start("other", 500) == 500


def test_microstructure_confirmation_is_directional_and_shadow_only(tmp_path, monkeypatch):
    path = tmp_path / "micro.json"
    path.write_text(json.dumps({"data": {"NVDA-USDT-SWAP": {
        "minute_ts": 1000, "book_imbalance": 0.4,
        "aggressive_imbalance": 0.2, "trade_count": 8, "spread_bps": 1.2,
        "bid_depth": 100, "ask_depth": 80,
    }}}))
    monkeypatch.setattr(okx_candidate_ws, "MICROSTRUCTURE_PATH", path)
    monkeypatch.setattr(okx_candidate_ws.time, "time", lambda: 1040)

    long_result = okx_candidate_ws.microstructure_confirmation("NVDA-USDT-SWAP", "LONG")
    short_result = okx_candidate_ws.microstructure_confirmation("NVDA-USDT-SWAP", "SHORT")

    assert long_result["available"] is True
    assert long_result["aligned"] is True
    assert short_result["aligned"] is False
    assert long_result["mode"] == "shadow_only"
    assert long_result["ask_depth"] == 80


def test_record_shadow_signal_is_idempotent(tmp_path, monkeypatch):
    database = tmp_path / "shadow.db"
    monkeypatch.setattr(okx_candidate_ws, "DB_PATH", database)
    row = {
        "instId": "NVDA-USDT-SWAP", "side": "LONG", "atr14": 1,
        "score": 80, "volume_ratio": 2, "spread_bps": 1,
        "estimated_slippage_bps": 2,
        "microstructure": {"available": True, "book_imbalance": .4, "aggressive_imbalance": .2},
    }
    okx_candidate_ws.record_shadow_signal(row, 1000, 100, "ONE_MIN_PASS")
    okx_candidate_ws.record_shadow_signal(row, 1000, 100, "ONE_MIN_PASS")

    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM okx_signal_shadow").fetchone()[0] == 1
        assert conn.execute("SELECT stage FROM okx_signal_shadow").fetchone()[0] == "ONE_MIN_PASS"
        assert conn.execute("SELECT experiment_id FROM okx_signal_shadow").fetchone()[0] == "rule_v1"


def test_shadow_signal_retry_uses_ny_trading_day_and_preserves_first_quote(tmp_path, monkeypatch):
    database = tmp_path / "shadow.db"
    monkeypatch.setattr(okx_candidate_ws, "DB_PATH", database)
    row = {"instId": "NVDA-USDT-SWAP", "side": "LONG", "atr14": 1,
           "experiment_id": "gap_v5", "microstructure": {}}
    first = int(datetime(2026, 8, 8, 1, 0, tzinfo=timezone.utc).timestamp() * 1000)
    retry = first + 90_000
    next_ny_day = int(datetime(2026, 8, 8, 4, 1, tzinfo=timezone.utc).timestamp() * 1000)

    okx_candidate_ws.record_shadow_signal(row, first, 100, "GAP_FADE_PASS")
    okx_candidate_ws.record_shadow_signal(row, retry, 101, "GAP_FADE_PASS")
    okx_candidate_ws.record_shadow_signal(row, next_ny_day, 102, "GAP_FADE_PASS")

    with sqlite3.connect(database) as conn:
        rows = conn.execute(
            "SELECT signal_key,entry_ts,entry_price FROM okx_signal_shadow ORDER BY entry_ts"
        ).fetchall()
    assert rows[0] == ("2026-08-07|gap_v5|GAP_FADE_PASS|NVDA-USDT-SWAP|LONG", first, 100)
    assert rows[1] == ("2026-08-08|gap_v5|GAP_FADE_PASS|NVDA-USDT-SWAP|LONG", next_ny_day, 102)


@pytest.mark.parametrize("stage", ["ONE_MIN_PASS", "RETURN_MODEL_PASS"])
def test_intraday_shadow_signal_keeps_distinct_same_day_candles(tmp_path, monkeypatch, stage):
    database = tmp_path / "shadow.db"
    monkeypatch.setattr(okx_candidate_ws, "DB_PATH", database)
    row = {"instId": "NVDA-USDT-SWAP", "side": "LONG", "atr14": 1,
           "experiment_id": "intraday-v1", "microstructure": {}}
    first = int(datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)

    okx_candidate_ws.record_shadow_signal(row, first, 100, stage)
    okx_candidate_ws.record_shadow_signal(row, first + 60_000, 101, stage)
    okx_candidate_ws.record_shadow_signal(row, first + 60_000, 102, stage)

    with sqlite3.connect(database) as conn:
        rows = conn.execute(
            "SELECT entry_ts,entry_price FROM okx_signal_shadow ORDER BY entry_ts"
        ).fetchall()
    assert rows == [(first, 100), (first + 60_000, 101)]


def test_shadow_signal_recognizes_legacy_same_day_row_without_rewriting_it(tmp_path, monkeypatch):
    database = tmp_path / "shadow.db"
    monkeypatch.setattr(okx_candidate_ws, "DB_PATH", database)
    okx_candidate_ws.ensure_shadow_schema()
    first = int(datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)
    with sqlite3.connect(database) as conn:
        conn.execute("""INSERT INTO okx_signal_shadow
            (signal_key,inst_id,side,stage,entry_ts,due_ts,entry_price,created_at,experiment_id)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            ("NVDA-USDT-SWAP:LONG:legacy", "NVDA-USDT-SWAP", "LONG", "GAP_FADE_PASS",
             first, first + 60_000, 99, "old", "gap_v5"))
    row = {"instId": "NVDA-USDT-SWAP", "side": "LONG", "atr14": 1,
           "experiment_id": "gap_v5", "microstructure": {}}
    okx_candidate_ws.record_shadow_signal(row, first + 60_000, 101, "GAP_FADE_PASS")

    with sqlite3.connect(database) as conn:
        stored = conn.execute(
            "SELECT signal_key,entry_ts,entry_price FROM okx_signal_shadow"
        ).fetchall()
    assert stored == [("NVDA-USDT-SWAP:LONG:legacy", first, 99)]


def test_shadow_signal_supports_preregistered_150_minute_horizon(tmp_path, monkeypatch):
    database = tmp_path / "shadow.db"
    monkeypatch.setattr(okx_candidate_ws, "DB_PATH", database)
    row = {"instId": "NVDA-USDT-SWAP", "side": "SHORT", "horizon_minutes": 150,
           "experiment_id": "gap_v1", "atr14": 1, "microstructure": {}}
    okx_candidate_ws.record_shadow_signal(row, 1_000, 100, "GAP_FADE_PASS")
    with sqlite3.connect(database) as conn:
        due, horizon = conn.execute("SELECT due_ts,horizon_minutes FROM okx_signal_shadow").fetchone()
    assert due == 1_000 + 150 * 60_000
    assert horizon == 150


def test_shadow_signal_persists_symbol_edge_sizing_fields(tmp_path, monkeypatch):
    database = tmp_path / "shadow.db"
    monkeypatch.setattr(okx_candidate_ws, "DB_PATH", database)
    row = {"instId": "NVDA-USDT-SWAP", "side": "LONG", "horizon_minutes": 60,
           "atr14": 1, "stop_bps": 100, "symbol_edge_pct": .25,
           "allocation_multiplier": 1.25, "microstructure": {}}
    okx_candidate_ws.record_shadow_signal(row, 1_000, 100, "GAP_FADE_V5_FORWARD")
    with sqlite3.connect(database) as conn:
        edge, multiplier = conn.execute(
            "SELECT symbol_edge_pct,allocation_multiplier FROM okx_signal_shadow"
        ).fetchone()
    assert edge == .25
    assert multiplier == 1.25


def test_gap_experiment_records_executable_quote_version():
    assert "fade_confirm_e0936_quote" in EXPERIMENT_ID
    assert EXECUTION_EQUIVALENT is False


def test_gap_demo_ranking_is_not_starved_by_research_only_symbols():
    rows = [(500, "PUBLIC-A", 0, 0, 0), (400, "PUBLIC-B", 0, 0, 0),
            (300, "DEMO-A", 0, 0, 0), (200, "DEMO-B", 0, 0, 0)]
    assert [row[1] for row in top_ranked(rows, {"DEMO-A", "DEMO-B"})] == ["DEMO-A", "DEMO-B"]


def test_gap_shadow_fetches_complete_v5_history_plus_forward_and_demo_pools():
    symbols = shadow_market_data_symbols(
        ("FORWARD-USDT-SWAP",), ("DEMO-USDT-SWAP",),
        ("EWJ-USDT-SWAP", "NFLX-USDT-SWAP"),
    )
    assert symbols == ("FORWARD-USDT-SWAP", "DEMO-USDT-SWAP",
                       "EWJ-USDT-SWAP", "NFLX-USDT-SWAP")


def test_gap_shadow_state_distinguishes_legacy_demo_from_v5_shadow():
    lanes = strategy_lanes()
    assert lanes["legacy_demo"]["v5"] is False
    assert lanes["v5_shadow"]["mode"] == "shadow_only"
    assert lanes["v5_shadow"]["execution_enabled"] is False
    assert lanes["legacy_demo"]["experiment_id"] != lanes["v5_shadow"]["experiment_id"]
    assert datetime.fromisoformat(V5_STRICT_CONFIRM_FROZEN_AT) > datetime.fromisoformat(V5_FROZEN_AT)


def test_gap_shadow_v5_applies_rank_gap_confirmation_and_frozen_horizon():
    spy = {"gap_bps": 20.0, "first5_bps": 5.0, "previous_day_bps": 10.0}
    contexts = {
        "A": {"gap_bps": 320.0, "first5_bps": -20.0, "previous_day_bps": 50.0},
        "B": {"gap_bps": 220.0, "first5_bps": -10.0, "previous_day_bps": 50.0},
        "C": {"gap_bps": 120.0, "first5_bps": -5.0, "previous_day_bps": 50.0},
        "D": {"gap_bps": 70.0, "first5_bps": -5.0, "previous_day_bps": 50.0},
    }
    choices = {"SHORT": {"horizon_minutes": 60, "prior_side_samples": 12,
                           "prior_best_horizon_expectancy_pct": .4}}
    selected = v5_ranked_candidates(contexts, spy, tuple(contexts), choices)
    assert [item["inst_id"] for item in selected] == ["A", "B"]
    assert all(item["horizon_minutes"] == 60 for item in selected)


def test_v5_strict_first5_is_post_rank_subset_without_backfill():
    ranked = [
        {"inst_id": "A", "relative_gap_bps": 300, "relative_first5_bps": -10},
        {"inst_id": "B", "relative_gap_bps": 250, "relative_first5_bps": 5},
        {"inst_id": "C", "relative_gap_bps": -200, "relative_first5_bps": 4},
    ]
    assert [row["inst_id"] for row in v5_strict_first5_subset(ranked)] == ["A", "C"]


def test_v5_strict_forward_gate_requires_25_fresh_samples_and_five_days():
    healthy = {"samples": 25, "profit_factor": 1.21, "expectancy_r": .01,
               "trading_days": 5, "invalid_samples": 0}
    assert all(_v5_forward_gate_checks(healthy, 0).values())
    assert not _v5_forward_gate_checks({**healthy, "samples": 24}, 0)["samples_at_least_25"]
    assert not _v5_forward_gate_checks({**healthy, "trading_days": 4}, 0)["at_least_5_trading_days"]


def test_v5_strict_forward_pass_cannot_promote_execution(tmp_path, monkeypatch):
    database = tmp_path / "shadow.db"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    output = data_dir / "learning.json"
    monkeypatch.setattr(okx_candidate_ws, "DB_PATH", database)
    monkeypatch.setattr(shadow_labeler, "DB_PATH", database)
    monkeypatch.setattr(shadow_labeler, "ROOT", tmp_path)
    monkeypatch.setattr(shadow_labeler, "STATE_PATH", output)
    okx_candidate_ws.ensure_shadow_schema()
    experiment = "strict-v5"
    stage = "GAP_FADE_V5_STRICT_CONFIRM_FORWARD"
    started = datetime(2026, 8, 10, 13, 36, tzinfo=timezone.utc)
    with sqlite3.connect(database) as conn:
        for day in range(5):
            for index in range(5):
                entry = int((started + timedelta(days=day, seconds=index)).timestamp() * 1000)
                conn.execute("""INSERT INTO okx_signal_shadow
                    (signal_key,inst_id,side,stage,entry_ts,due_ts,entry_price,horizon_minutes,
                     atr14,stop_bps,spread_bps,slippage_bps,net_r,labeled_at,created_at,experiment_id)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (f"strict-{day}-{index}", f"S{index}", "LONG", stage, entry,
                     entry + 30 * 60_000, 100, 30, 1, 100, 1, 1, .5, "done", "now", experiment))
    gap_state = {
        "experiment_id": "legacy-gap", "experiment_started_at": started.isoformat(),
        "v5_experiment_id": "base-v5", "v5_experiment_started_at": started.isoformat(),
        "v5_strict_confirm_experiment_id": experiment,
        "v5_strict_confirm_stage": stage,
        "v5_strict_confirm_experiment_started_at": started.isoformat(),
    }
    (data_dir / "okx_gap_shadow_state.json").write_text(json.dumps(gap_state))

    shadow_labeler.Labeler.__new__(shadow_labeler.Labeler).write_state()
    state = json.loads(output.read_text())

    assert state["v5_strict_confirm_forward_passed"] is True
    assert state["v5_strict_confirm_execution_ready"] is False
    assert state["passed"] is False
    assert state["qualified_strategy"] is None


def test_v5_symbol_edge_sizing_keeps_gross_and_upweights_better_candidate():
    rows = []
    for index, multiplier in enumerate((1.5, 1.4, 1.0, .6, .5)):
        rows.append({
            "entry_ts": 1_000, "stop_bps": 100.0,
            "net_r": 2.0 if index == 0 else -1.0,
            "allocation_multiplier": multiplier,
        })
    value = _v5_symbol_edge_sizing_stats(rows)
    assert value["max_daily_gross_difference_pct_points"] == 0
    assert value["symbol_edge_weighted"]["return_pct"] > value["baseline"]["return_pct"]


def test_gap_next_evaluation_skips_weekend():
    ny = ZoneInfo("America/New_York")
    saturday = datetime(2026, 7, 18, 12, 0, tzinfo=ny)
    assert datetime.fromisoformat(next_evaluation_at(saturday)).astimezone(ny).isoformat().startswith("2026-07-20T09:36")


def test_barrier_target_uses_next_open_and_assumes_same_bar_stop_first():
    ny = ZoneInfo("America/New_York")
    start = datetime(2026, 7, 17, 8, 0, tzinfo=ny).astimezone(timezone.utc)
    rows = []
    for minute in range(180):
        stamp = int((start.timestamp() + minute * 60) * 1000)
        # The 10:00 5m bar is the next-open entry bar.  Its range touches both
        # a 1% stop and a 1.5R target, so the conservative result must be loss.
        local = datetime.fromtimestamp(stamp / 1000, timezone.utc).astimezone(ny)
        if local.hour == 10 and local.minute < 5:
            high, low = 102, 98
        else:
            high, low = 100.1, 99.9
        rows.append([str(stamp), "100", str(high), str(low), "100", "1", "1", "1", "1"])
    result = barrier_targets(rows, stop_min_bps=100, target_r=1.5, horizon_bars=12)
    first = result.iloc[0]
    assert first["long_net_r"] < -1
    assert first["short_net_r"] < -1


def test_shadow_outcome_includes_observed_costs():
    row = {"entry_ts": 0, "due_ts": 60 * 60_000, "entry_price": 100,
           "side": "LONG", "atr14": 1, "spread_bps": 2, "slippage_bps": 2}
    candles = [[str(minute * 60_000), "100", "101", "99.5", "100.6", "1", "1", "1", "1"]
               for minute in range(1, 61)]
    result = Labeler.outcome(row, candles)

    assert result is not None
    assert round(result["gross_r"], 3) == 0.5
    assert round(result["net_r"], 3) == 0.4
    assert round(result["mfe_r"], 3) == 0.833


def test_v5_shadow_outcome_applies_persisted_stop_before_horizon():
    row = {"entry_ts": 0, "due_ts": 30 * 60_000, "entry_price": 100,
           "side": "LONG", "stage": "GAP_FADE_V5_FORWARD", "stop_bps": 75,
           "atr14": 1, "spread_bps": 2, "slippage_bps": 4}
    candles = [[str(minute * 60_000), "100", "102", "99", "101", "1", "1", "1", "1"]
               for minute in range(1, 31)]
    result = Labeler.outcome(row, candles)

    assert result is not None
    assert result["exit_reason"] == "atr_stop"
    assert result["exit_price"] == pytest.approx(99.25)
    assert result["gross_r"] == pytest.approx(-1.0)
    assert result["net_r"] == pytest.approx(-1 - 14 / 75)


def test_v5_strict_shadow_outcome_is_stop_aware():
    row = {"entry_ts": 0, "due_ts": 30 * 60_000, "entry_price": 100,
           "side": "SHORT", "stage": "GAP_FADE_V5_STRICT_CONFIRM_FORWARD", "stop_bps": 75,
           "atr14": 1, "spread_bps": 2, "slippage_bps": 4}
    candles = [[str(minute * 60_000), "100", "101", "99", "100", "1", "1", "1", "1"]
               for minute in range(1, 31)]
    result = Labeler.outcome(row, candles)

    assert result is not None
    assert result["exit_reason"] == "atr_stop"
    assert result["exit_price"] == pytest.approx(100.75)


def test_return_shadow_scores_only_latest_fixed_opportunity():
    class Model:
        @staticmethod
        def predict(values):
            return values[:, 0] * 10

    rows = pd.DataFrame([
        {"entry_time": 1000, "symbol": "A", "r1": 1},
        {"entry_time": 2000, "symbol": "A", "r1": -2},
        {"entry_time": 2000, "symbol": "B", "r1": 3},
    ])
    result = score_rows(rows, {"features": ["r1", "missing_dummy"], "model": Model()})

    assert [row["symbol"] for row in result] == ["B", "A"]
    assert [row["prediction_bps"] for row in result] == [30, -20]


def test_historical_label_window_uses_due_timestamp_cursor(monkeypatch):
    calls = []
    client = OKX.__new__(OKX)
    client.request = lambda method, path, params: calls.append((method, path, params)) or {"data": [["1"]]}
    monkeypatch.setattr(okx_intraday_agent.time, "sleep", lambda _: None)

    result = client.candles_ending_at("NVDA-USDT-SWAP", 3_600_000, limit=100, bar="1m")

    assert result == [["1"]]
    assert calls[0][2]["after"] == "3600001"
    assert calls[0][2]["limit"] == "100"


def test_historical_mark_price_window_uses_mark_candle_endpoint(monkeypatch):
    calls = []
    client = OKX.__new__(OKX)
    client.request = lambda method, path, params: calls.append((method, path, params)) or {"data": [["1"]]}
    monkeypatch.setattr(okx_intraday_agent.time, "sleep", lambda _: None)

    result = client.mark_price_candles_ending_at(
        "NVDA-USDT-SWAP", 3_600_000, limit=100, bar="5m",
    )

    assert result == [["1"]]
    assert calls == [("GET", "/api/v5/market/history-mark-price-candles", {
        "instId": "NVDA-USDT-SWAP", "bar": "5m", "limit": "100", "after": "3600001",
    })]


def test_mark_price_requires_matching_public_row():
    calls = []
    client = OKX.__new__(OKX)
    client.request = lambda method, path, params: calls.append((method, path, params)) or {
        "data": [{"instId": "NVDA-USDT-SWAP", "markPx": "123.45"}],
    }

    result = client.mark_price("NVDA-USDT-SWAP")

    assert result["markPx"] == "123.45"
    assert calls[0][1] == "/api/v5/public/mark-price"


def test_deployment_gate_requires_both_forward_pass_and_execution_audit(tmp_path, monkeypatch):
    path = tmp_path / "learning.json"
    monkeypatch.setattr(okx_candidate_ws, "SHADOW_LEARNING_PATH", path)
    path.write_text(json.dumps({"passed": True, "execution_ready": False,
                                "qualified_strategy": "return_model_executable"}))
    allowed, reason = okx_candidate_ws.deployment_gate()
    assert not allowed
    assert "执行路径" in reason

    path.write_text(json.dumps({"passed": True, "execution_ready": True,
                                "qualified_strategy": "return_model_executable"}))
    assert okx_candidate_ws.deployment_gate()[0]
    path.write_text(json.dumps({"passed": True, "execution_ready": True,
                                "qualified_strategy": "micro_barrier_executable"}))
    assert okx_candidate_ws.deployment_gate()[0]
    assert okx_candidate_ws.deployment_gate({"micro_barrier_executable"})[0]
    allowed, reason = okx_candidate_ws.deployment_gate({"rule_micro_aligned"})
    assert not allowed
    assert "执行路径不匹配" in reason


def test_exploratory_demo_switch_is_demo_profile_only(monkeypatch):
    class Config:
        def __init__(self, profile): self.profile = profile

    monkeypatch.delenv("OKX_INTRADAY_EXPLORATORY_DEMO", raising=False)
    assert not okx_candidate_ws.exploratory_demo_enabled(Config("demo"))
    monkeypatch.setenv("OKX_INTRADAY_EXPLORATORY_DEMO", "1")
    assert okx_candidate_ws.exploratory_demo_enabled(Config("demo"))
    assert not okx_candidate_ws.exploratory_demo_enabled(Config("live"))


def test_gap_signal_uses_regular_open_and_previous_regular_close():
    ny = ZoneInfo("America/New_York")
    def candle(stamp, open_px, close_px):
        ms = int(datetime.fromisoformat(stamp).replace(tzinfo=ny).astimezone(timezone.utc).timestamp() * 1000)
        return [str(ms), str(open_px), str(open_px), str(open_px), str(close_px), "1", "1", "1", "1"]
    previous = [candle("2026-07-17T09:30:00", 99, 99), candle("2026-07-17T15:55:00", 100, 100)]
    current = [candle("2026-07-20T09:30:00", 102, 102.2)]

    assert round(gap_bps(current, previous, datetime(2026, 7, 20).date()), 6) == 200
    context = gap_context(current, previous, datetime(2026, 7, 20).date())
    assert round(context["first5_bps"], 6) == round((102.2 / 102 - 1) * 10_000, 6)
