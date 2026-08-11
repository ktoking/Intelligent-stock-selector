from __future__ import annotations

from types import SimpleNamespace

from scripts import okx_gap_demo_executor
from scripts.okx_gap_demo_executor import candidate_stop_bps, execution_candidates


def test_candidate_stop_bps_uses_atr_and_bounds():
    assert candidate_stop_bps({"atr_bps": 100}) == 120.0
    assert candidate_stop_bps({"atr_bps": 10}) == 75.0
    assert candidate_stop_bps({"atr_bps": 400}) == 300.0


def test_candidate_stop_bps_falls_back_to_frozen_default():
    assert candidate_stop_bps({}) == 75.0


def test_execution_candidates_prefers_demo_lane_even_when_it_is_empty():
    assert execution_candidates({"demo_candidates": [], "candidates": [{"inst_id": "PUBLIC"}]}) == []
    assert execution_candidates({"candidates": [{"inst_id": "LEGACY"}]}) == [{"inst_id": "LEGACY"}]


def test_gap_demo_enabled_obeys_emergency_kill_switch(monkeypatch):
    monkeypatch.setenv("OKX_GAP_EXPLORATORY_DEMO", "1")
    monkeypatch.setattr(okx_gap_demo_executor, "settings", lambda: SimpleNamespace(profile="demo"))
    monkeypatch.setattr(okx_gap_demo_executor, "kill_switch", lambda: {"enabled": False})
    assert okx_gap_demo_executor.enabled()

    monkeypatch.setattr(okx_gap_demo_executor, "kill_switch", lambda: {"enabled": True})
    assert not okx_gap_demo_executor.enabled()


def test_running_gap_demo_refuses_submit_after_kill_switch(monkeypatch):
    monkeypatch.setattr(okx_gap_demo_executor, "enabled", lambda: False)
    executor = okx_gap_demo_executor.Executor.__new__(okx_gap_demo_executor.Executor)
    executor._submit({"inst_id": "NVDA-USDT-SWAP", "side": "LONG"}, 1_000)
