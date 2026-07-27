import json
import math

from scripts import okx_runtime


def test_runtime_heartbeat_age_parses_utc_and_rejects_missing_state(tmp_path, monkeypatch):
    path = tmp_path / "heartbeat.json"
    path.write_text(json.dumps({"updated_at": "1970-01-01T00:01:30+00:00"}))
    monkeypatch.setattr(okx_runtime.time, "time", lambda: 100)
    assert okx_runtime.Runtime.heartbeat_age(path) == 10
    assert math.isinf(okx_runtime.Runtime.heartbeat_age(tmp_path / "missing.json"))
