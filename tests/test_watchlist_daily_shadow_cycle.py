import pytest
import pandas as pd

from research.watchlist_daily_shadow_cycle import build_snapshot
from research.watchlist_daily_trend import OUT as DATA_DIR


def cached_sessions():
    frame = pd.read_pickle(DATA_DIR / "QQQ.pkl")
    return pd.to_datetime(frame.date.astype(str), format="%Y%m%d")


def test_completed_bar_shadow_snapshot_reconciles():
    latest = cached_sessions().iloc[-1]
    snapshot = build_snapshot(str(latest.date()))
    paper = snapshot["paper_replay_not_actual_account"]
    assert snapshot["signal_date"] == str(latest.date())
    assert pd.Timestamp(snapshot["next_session"]) > latest
    assert snapshot["universe_count"] >= 200
    assert len(paper["positions"]) == paper["since_2026_03_23"]["holding_count_end"]
    if not paper["orders_on_signal_date"]:
        assert len(paper["one_day_attribution_no_orders_only"]) == len(paper["positions"])
    if not snapshot["next_open_frozen_strategy"]["is_monday_rebalance"]:
        assert snapshot["next_open_frozen_strategy"]["planned_new_buy_symbols"] == []


def test_refuses_to_label_old_bar_as_current():
    previous = cached_sessions().iloc[-2]
    with pytest.raises(ValueError, match="Refuse stale/future signal"):
        build_snapshot(str(previous.date()))
