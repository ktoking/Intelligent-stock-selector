import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.daily_factor_lab.core import Policy, load_panel
from research.daily_factor_lab.paper import advance, initialize
from tests.test_daily_factor_lab_core import write_cache


def test_sealed_paper_replay_is_idempotent_and_rejects_data_drift(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    dates = write_cache(data)
    directory = tmp_path / "paper"
    seed = str(dates[190].date())
    policy = Policy("paper", positions=2, rank_buffer=2, schedule="daily",
                    annual_vol_target=.5, weight_cap=.4)
    manifest = initialize(directory, policy, seed, data)
    waiting = advance(directory, seed, data)
    assert waiting["status"] == "awaiting_first_session"
    assert waiting["orders"] == []
    first = advance(directory, str(dates[210].date()), data)
    assert first["orders"]
    second = advance(directory, str(dates[240].date()), data)
    assert first["curve"] == second["curve"][:len(first["curve"])]
    assert all(order["date"] > first["asof"] for order in second["new_orders"])
    assert (directory / "manifest.json").exists()
    with pytest.raises(FileExistsError):
        advance(directory, second["asof"], data)
    with pytest.raises(FileExistsError):
        initialize(directory, policy, seed, data)
    path = data / "AAA.pkl"
    frame = pd.read_pickle(path)
    frame.loc[205, ["open", "high", "low", "close"]] *= 1.1
    frame.to_pickle(path)
    with pytest.raises(ValueError, match="Data revision"):
        advance(directory, str(dates[250].date()), data)
    assert manifest["mode"] == "local_paper_no_broker"


def test_policy_or_sealed_artifact_tampering_is_detected(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    dates = write_cache(data)
    policy = Policy("paper", positions=2, rank_buffer=2)
    directory = tmp_path / "paper"
    initialize(directory, policy, str(dates[190].date()), data)
    first = advance(directory, str(dates[210].date()), data)
    path = directory / f"session_{first['asof']}.json"
    original = path.read_bytes()
    changed = json.loads(original)
    changed["cash"] += 1
    path.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="artifact was modified"):
        advance(directory, str(dates[220].date()), data)
    path.write_bytes(original)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["policy"]["weight_cap"] = .9
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="manifest changed"):
        advance(directory, str(dates[220].date()), data)


def test_missing_held_bar_fails_instead_of_replacing_stale_position():
    from research.daily_factor_lab.core import simulate
    from tests.test_daily_factor_lab_core import synthetic_panel, simple_policy
    panel = synthetic_panel()
    panel.fields["open"][73, 0] = np.nan
    panel.fields["close"][73, 0] = np.nan
    with pytest.raises(ValueError, match="Held asset has missing"):
        simulate(panel, simple_policy(), panel.dates[70], panel.dates[78])
