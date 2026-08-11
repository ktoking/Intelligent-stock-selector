import json
import math
from datetime import datetime
from zoneinfo import ZoneInfo

from scripts import okx_runtime


def test_runtime_heartbeat_age_parses_utc_and_rejects_missing_state(tmp_path, monkeypatch):
    path = tmp_path / "heartbeat.json"
    path.write_text(json.dumps({"updated_at": "1970-01-01T00:01:30+00:00"}))
    monkeypatch.setattr(okx_runtime.time, "time", lambda: 100)
    assert okx_runtime.Runtime.heartbeat_age(path) == 10
    assert math.isinf(okx_runtime.Runtime.heartbeat_age(tmp_path / "missing.json"))


def test_v5_refresh_due_only_after_close_and_when_report_is_stale(tmp_path):
    ny = ZoneInfo("America/New_York")
    path = tmp_path / "v5.json"
    path.write_text(json.dumps({"effective_sessions": {"end": "2026-08-06"}}))
    assert okx_runtime.v5_refresh_due(datetime(2026, 8, 7, 16, 14, tzinfo=ny), path) is False
    assert okx_runtime.v5_refresh_due(datetime(2026, 8, 7, 16, 15, tzinfo=ny), path) is True
    path.write_text(json.dumps({"effective_sessions": {"end": "2026-08-07"}}))
    assert okx_runtime.v5_refresh_due(datetime(2026, 8, 7, 20, 0, tzinfo=ny), path) is False
    assert okx_runtime.v5_refresh_due(datetime(2026, 8, 8, 20, 0, tzinfo=ny), path) is False


def test_runtime_never_starts_gap_demo_while_kill_switch_is_enabled(monkeypatch):
    class RunningChild:
        terminated = False

        @staticmethod
        def poll():
            return None

        def terminate(self):
            self.terminated = True

    runtime = okx_runtime.Runtime()
    existing_gap_demo = RunningChild()
    runtime.children["gap_demo_executor"] = existing_gap_demo
    started = []
    monkeypatch.setattr(okx_runtime, "monitor_control", lambda: {"enabled": True})
    monkeypatch.setattr(okx_runtime, "kill_switch", lambda: {"enabled": True})
    monkeypatch.setenv("OKX_GAP_EXPLORATORY_DEMO", "1")
    monkeypatch.setattr(runtime, "start_child", lambda name, _command: started.append(name))

    runtime.ensure_children()

    assert "gap_demo_executor" not in started
    assert "executor" not in started
    assert "gap_shadow" in started
    assert existing_gap_demo.terminated


def test_runtime_starts_opted_in_gap_demo_when_kill_switch_is_off(monkeypatch):
    runtime = okx_runtime.Runtime()
    started = []
    monkeypatch.setattr(okx_runtime, "monitor_control", lambda: {"enabled": True})
    monkeypatch.setattr(okx_runtime, "kill_switch", lambda: {"enabled": False})
    monkeypatch.setenv("OKX_GAP_EXPLORATORY_DEMO", "1")
    monkeypatch.setattr(runtime, "start_child", lambda name, _command: started.append(name))

    runtime.ensure_children()

    assert "gap_demo_executor" in started
    assert "executor" in started
