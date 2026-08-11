from __future__ import annotations

import json

import pandas as pd

import scripts.okx_gap_confirmation_overlay_research as overlay_research
from scripts.okx_gap_confirmation_overlay_research import (
    COSTS_BPS,
    assess_trades,
    confirmation_overlay_trades,
    research,
    tail_robustness,
)
from scripts.okx_gap_strategy_v5 import AdaptiveHorizonConfig


def _trade(day: str, symbol: str, net_pct: float, *, horizon: int = 30) -> dict:
    return {
        "date": day,
        "symbol": symbol,
        "entry_time": 1_000,
        "exit_time": 1_000 + horizon * 60_000,
        "exit_time_basis": "scheduled_horizon",
        "stop_bar_number": None,
        "side": "SHORT",
        "relative_gap_bps": 200.0,
        "horizon_minutes": horizon,
        "stop_bps": 100.0,
        "net_pct": net_pct,
        "exit_reason": "horizon",
    }


def test_confirmation_overlay_filters_frozen_trades_without_backfill_or_refit():
    frozen = [
        _trade("d1", "KEEP", 1.0, horizon=60),
        _trade("d1", "DROP", -1.0, horizon=90),
    ]
    features = pd.DataFrame([
        {"date": "d1", "symbol": "KEEP", "relative_gap": 200.0, "relative_first5": -10.0},
        {"date": "d1", "symbol": "DROP", "relative_gap": 200.0, "relative_first5": 10.0},
        # This row would qualify, but it was not selected by frozen V5 and may
        # never backfill the skipped DROP slot.
        {"date": "d1", "symbol": "BACKFILL", "relative_gap": 300.0, "relative_first5": -20.0},
    ])
    value = confirmation_overlay_trades(frozen, features)
    assert [item["symbol"] for item in value] == ["KEEP"]
    assert value[0]["horizon_minutes"] == 60
    assert value[0]["net_pct"] == 1.0
    assert frozen[0].get("overlay_confirmation") is None


def test_assessment_reports_fixed_costs_folds_and_risk_portfolios():
    days = [f"d{index:02d}" for index in range(99)]
    trades = [_trade(days[20], "A", 2.0), _trade(days[90], "B", -1.0)]
    value = assess_trades(trades, days)
    assert set(value["cost_sensitivity"]) == {str(item) for item in COSTS_BPS}
    assert value["cost_sensitivity"]["100"]["trade_metrics"]["equal_weight_net_pct"] == -0.72
    assert set(value["folds"]) == {
        "validation_a", "validation_b", "validation_c", "diagnostic", "latest_unseen",
    }
    assert value["risk_weighted_portfolio"]["all"]["initial_equity"] == 10_000.0


def test_tail_robustness_removes_fixed_best_symbol_trade_and_latest_fold():
    days = [f"d{index:02d}" for index in range(99)]
    trades = [
        _trade(days[20], "A", 5.0),
        _trade(days[21], "A", -1.0),
        _trade(days[40], "B", 2.0),
        _trade(days[90], "C", 3.0),
    ]
    value = tail_robustness(trades, days, AdaptiveHorizonConfig())
    assert value["remove_best_symbol"]["excluded_symbol"] == "A"
    assert value["remove_best_trade"]["excluded_trade"]["net_pct"] == 5.0
    assert value["remove_latest_fold"]["trade_metrics"]["trades"] == 3


def test_research_is_always_blocked_and_binds_frozen_subset():
    days = [f"d{index:02d}" for index in range(99)]
    frozen = [_trade(days[20], "KEEP", 1.0), _trade(days[21], "DROP", -1.0)]
    rows = pd.DataFrame([
        {"date": day, "symbol": "KEEP", "relative_gap": 200.0, "relative_first5": -10.0}
        for day in days
    ] + [
        {"date": days[21], "symbol": "DROP", "relative_gap": 200.0, "relative_first5": 10.0},
    ])
    # Keep one unique feature row per key while retaining every session date.
    rows = rows.drop_duplicates(["date", "symbol"])
    source = {
        "generated_at": "source-time",
        "effective_sessions": {"count": 99, "start": days[0], "end": days[-1]},
        "trades": frozen,
        "warning": "retrospective",
    }
    value = research(
        rows,
        source,
        fingerprints={"frozen_rule_sha256": "test"},
        generated_at="generated-time",
    )
    assert value["promotion_passed"] is False
    assert value["experiment_id"] == "gap_v5_strict_first5_no_backfill_shadow_20260808"
    assert value["invariants"]["replacement_trades"] == 0
    assert value["invariants"]["adaptive_horizon_refit"] is False
    assert value["overlay"]["all"]["trades"] == 1
    assert any("new_paired_forward" in item for item in value["promotion_blockers"])


def test_universe_fingerprint_is_stable_when_only_quote_metadata_changes(tmp_path, monkeypatch):
    universe = tmp_path / "universe.json"
    first = {
        "generated_at": "t1",
        "historical_90d": [
            {"inst_id": "B-USDT-SWAP", "volume_24h_usdt": 1.0},
            {"inst_id": "A-USDT-SWAP", "spread_bps": 2.0},
        ],
    }
    universe.write_text(json.dumps(first))
    monkeypatch.setattr(overlay_research, "SOURCE_UNIVERSE", universe)
    first_symbols = overlay_research._historical_universe_symbols()
    first_hash = overlay_research._stable_sha256(first_symbols)
    first_snapshot_hash = overlay_research._sha256_file(universe)

    second = {
        "generated_at": "t2",
        "historical_90d": [
            {"inst_id": "A-USDT-SWAP", "spread_bps": 9.0},
            {"inst_id": "B-USDT-SWAP", "volume_24h_usdt": 99.0},
        ],
    }
    universe.write_text(json.dumps(second))
    second_symbols = overlay_research._historical_universe_symbols()
    second_hash = overlay_research._stable_sha256(second_symbols)
    second_snapshot_hash = overlay_research._sha256_file(universe)

    assert first_symbols == second_symbols == ["A-USDT-SWAP", "B-USDT-SWAP"]
    assert first_hash == second_hash
    assert first_snapshot_hash != second_snapshot_hash
