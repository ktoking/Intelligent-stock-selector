from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.okx_research_bias_audit import (
    audit,
    cscv_pbo,
    daily_return_stats,
    deflated_sharpe_probability,
    portfolio_daily_returns,
    reported_label_intervals,
)


def test_portfolio_returns_fill_verified_no_trade_days_with_zero():
    calendar = ["2026-01-05", "2026-01-06", "2026-01-07"]
    portfolio = {
        "initial_equity": 100.0,
        "daily": [
            {"date": "2026-01-06", "pnl": 10.0, "equity": 110.0},
        ],
    }
    values, error = portfolio_daily_returns(portfolio, calendar)
    assert error is None
    assert values == pytest.approx([0.0, 10.0, 0.0])
    stats = daily_return_stats(values)
    assert stats["active_days"] == 1
    assert stats["zero_days"] == 2
    assert stats["cumulative_return_pct"] == 10.0


def test_portfolio_returns_reject_unverified_outside_day():
    values, error = portfolio_daily_returns(
        {
            "initial_equity": 100.0,
            "daily": [{"date": "2026-01-08", "pnl": 1.0, "equity": 101.0}],
        },
        ["2026-01-05"],
    )
    assert values is None
    assert error == "portfolio_days_outside_verified_calendar:2026-01-08"


def test_deflated_sharpe_is_unavailable_for_constant_returns():
    result = deflated_sharpe_probability([1.0] * 20, [0.1, 0.2], 2)
    assert result == {"available": False, "reason": "zero_variance_return_series"}


def test_trial_count_stress_cannot_raise_deflated_sharpe_probability():
    values = [1.0, -0.4, 0.8, -0.2, 1.2, -0.3, 0.7, -0.1] * 5
    trial_sharpes = [-0.2, 0.0, 0.1, 0.3, 0.5]
    small = deflated_sharpe_probability(values, trial_sharpes, 5)
    stressed = deflated_sharpe_probability(values, trial_sharpes, 100)
    assert small["available"] is True
    assert stressed["available"] is True
    assert stressed["selection_benchmark_daily_sharpe"] > small["selection_benchmark_daily_sharpe"]
    assert stressed["deflated_sharpe_probability"] <= small["deflated_sharpe_probability"]


def test_cscv_pbo_reports_mathematical_boundaries():
    too_few = cscv_pbo({"a": [0.0] * 40, "b": [0.1] * 40})
    assert too_few == {"available": False, "reason": "fewer_than_four_unique_candidates"}

    values = {
        "a": [1.0] * 20 + [-1.0] * 20,
        "b": [-1.0] * 20 + [1.0] * 20,
        "c": [0.2, -0.1] * 20,
        "d": [-0.1, 0.2] * 20,
    }
    result = cscv_pbo(values, block_count=4)
    assert result["available"] is True
    assert result["combinations"] == 6
    assert 0.0 <= result["pbo_probability"] <= 1.0
    assert result["purging_applied"] is False


def test_purged_cscv_removes_overlapping_labels_and_embargoes_next_session():
    values = {
        "a": [1.0] * 20 + [-1.0] * 20,
        "b": [-1.0] * 20 + [1.0] * 20,
        "c": [0.2, -0.1] * 20,
        "d": [-0.1, 0.2] * 20,
    }
    # Adjacent session labels overlap by five timestamp units.  Every
    # candidate supplies its own matrix; CSCV must use a common union purge
    # mask so candidate scores still share identical observations.
    intervals = {
        name: [[(index * 10, index * 10 + 15)] for index in range(40)]
        for name in values
    }
    result = cscv_pbo(
        values,
        block_count=4,
        label_intervals_by_candidate=intervals,
        embargo_sessions=1,
    )
    assert result["available"] is True
    assert result["purging_applied"] is True
    assert result["embargo_applied"] is True
    assert result["overlap_purged_sessions_across_splits"] > 0
    assert result["embargoed_sessions_across_splits"] > 0
    assert result["minimum_in_sample_sessions_after_purge_embargo"] < 20
    assert result["label_interval_count"] == 160


def test_reported_label_intervals_never_infers_missing_exit_from_horizon():
    intervals, evidence = reported_label_intervals(
        [{
            "date": "2026-01-05",
            "entry_time": 1_767_600_000_000,
            "horizon_minutes": 90,
            "net_pct": 1.0,
        }],
        ["2026-01-05"],
    )
    assert intervals is None
    assert evidence == {
        "available": False,
        "reason": "explicit_entry_exit_timestamp_missing_or_ambiguous:row_0",
    }


def test_reported_label_intervals_reconciles_portfolio_trade_counts():
    intervals, evidence = reported_label_intervals(
        [{
            "date": "2026-01-05",
            "entry_time": 1_767_600_000_000,
            "exit_time": 1_767_603_600_000,
            "net_pct": 1.0,
        }],
        ["2026-01-05"],
        portfolio={"daily": [{"date": "2026-01-05", "trades": 2}]},
    )
    assert intervals is None
    assert evidence == {
        "available": False,
        "reason": "portfolio_daily_trade_count_mismatch:2026-01-05:2!=1",
    }


def test_reported_label_intervals_accepts_explicit_label_pair_without_execution_pair():
    intervals, evidence = reported_label_intervals(
        [{
            "date": "2026-01-05",
            "label_start_time": 1_767_600_000_000,
            "label_end_time": 1_767_603_600_000,
            "net_pct": 1.0,
        }],
        ["2026-01-05"],
    )
    assert intervals == [[(1_767_600_000_000, 1_767_603_600_000)]]
    assert evidence["available"] is True
    assert evidence["timestamp_field_pairs"] == ["label_start_time/label_end_time"]


def test_missing_artifacts_are_registered_without_fabricated_candidates(tmp_path: Path):
    report = audit(tmp_path)
    assert report["candidate_counts"]["available_series"] == 0
    assert report["candidate_counts"]["comparable_exact_unique"] == 0
    assert report["selection_adjustment"]["available"] is False
    assert report["cscv_pbo"]["available"] is False
    assert all(item["status"] == "missing" for item in report["artifact_inventory"])
    assert all(item["status"] == "unavailable" for item in report["candidate_registry"])


def test_audit_deduplicates_exact_portfolio_series(tmp_path: Path):
    calendar = {"count": 20, "start": "2026-01-05", "end": "2026-01-30"}
    # Jan 19 is a weekday, so the simple weekday calendar contains exactly 20 sessions.
    daily = []
    equity = 100.0
    dates = []
    cursor = __import__("datetime").date.fromisoformat(calendar["start"])
    end = __import__("datetime").date.fromisoformat(calendar["end"])
    while cursor <= end:
        if cursor.weekday() < 5:
            dates.append(cursor.isoformat())
        cursor += __import__("datetime").timedelta(days=1)
    assert len(dates) == 20
    for index, day in enumerate(dates):
        pnl = 1.0 if index % 2 == 0 else -0.25
        equity += pnl
        daily.append({"date": day, "pnl": pnl, "equity": equity})
    document = {
        "effective_sessions": calendar,
        "results": {
            "one": {
                "all": {"trades": 20},
                "risk_weighted_portfolio": {"initial_equity": 100.0, "daily": daily},
            },
            "duplicate": {
                "all": {"trades": 20},
                "risk_weighted_portfolio": {"initial_equity": 100.0, "daily": daily},
            },
        },
    }
    (tmp_path / "okx_gap_beta_research.json").write_text(json.dumps(document))
    report = audit(tmp_path)
    assert report["candidate_counts"]["reported_comparable_before_exact_deduplication"] == 2
    assert report["candidate_counts"]["comparable_exact_unique"] == 1
    candidates = [
        item for item in report["candidate_registry"]
        if item["family"] == "beta" and item.get("daily_return_stats")
    ]
    assert sum(item.get("selection_bias_included") is True for item in candidates) == 1
    assert sum(item.get("duplicate_of") is not None for item in candidates) == 1


def test_declared_trials_stress_and_diagnostic_paths_are_separate(tmp_path: Path):
    from datetime import date, timedelta

    dates = []
    cursor = date(2026, 1, 5)
    while len(dates) < 20:
        if cursor.weekday() < 5:
            dates.append(cursor.isoformat())
        cursor += timedelta(days=1)

    def portfolio(returns):
        equity = 100.0
        daily = []
        for day, value in zip(dates, returns):
            next_equity = equity * (1.0 + value / 100.0)
            daily.append({"date": day, "pnl": next_equity - equity, "equity": next_equity})
            equity = next_equity
        return {"initial_equity": 100.0, "daily": daily}

    baseline = portfolio([0.2, -0.1] * 10)
    overlay = portfolio([0.3, -0.05] * 10)
    diagnostic = portfolio([1.0, -0.01] * 10)
    confirmation = {
        "effective_sessions": {"count": 20, "start": dates[0], "end": dates[-1]},
        "baseline": {
            "all": {"trades": 20},
            "risk_weighted_portfolio": {"all": baseline},
        },
        "overlay": {
            "all": {"trades": 20},
            "risk_weighted_portfolio": {"all": overlay},
            "cost_sensitivity": {
                "cost14": {
                    "all": {"trades": 20},
                    "risk_weighted_portfolio": diagnostic,
                },
            },
        },
    }
    (tmp_path / "okx_gap_confirmation_overlay_research.json").write_text(json.dumps(confirmation))

    protected = [
        {"stop_r": 1 + index % 5, "breakeven_at_1r": bool(index % 2), "state_lookback_days": index}
        for index in range(20)
    ]
    protected.append(dict(protected[0]))
    historical = {
        "tested": 120,
        "eligible": [{"config": ["relative", "fade", 150, 100]}],
        "selected_diagnostic": {"config": ["relative", "fade", 150, 100]},
        "protected_state_diagnostics": protected,
    }
    (tmp_path / "okx_gap_research_90d.json").write_text(json.dumps(historical))

    report = audit(tmp_path)
    governance = report["historical_trial_governance"]
    assert governance["declared_historical_trial_lower_bound"] == 140
    assert governance["evidence"][0]["within_artifact_total"] == 140
    assert report["selection_adjustment"]["best_candidate"].endswith("::overlay")
    assert [
        item["trial_count"] for item in report["selection_adjustment"]["trial_count_stress"]
    ] == [140, 250, 500]
    cost_candidate = next(
        item for item in report["candidate_registry"]
        if item.get("candidate_path") == "overlay.cost_sensitivity.cost14"
    )
    assert cost_candidate["diagnostic_only"] is True
    assert cost_candidate["selection_bias_eligible"] is False


def test_audit_builds_separate_purged_cohort_only_from_explicit_intervals(tmp_path: Path):
    from datetime import date, timedelta

    dates = []
    cursor = date(2026, 1, 5)
    while len(dates) < 20:
        if cursor.weekday() < 5:
            dates.append(cursor.isoformat())
        cursor += timedelta(days=1)

    return_paths = {
        "one": [0.3, -0.1] * 10,
        "two": [-0.1, 0.3] * 10,
        "three": [0.4, -0.2, 0.1, -0.05] * 5,
        "four": [-0.3, 0.5, -0.1, 0.2] * 5,
        "missing_exit": [0.15, -0.05, 0.25, -0.1] * 5,
    }
    results = {}
    base_timestamp = 1_767_600_000_000
    for candidate_index, (name, returns) in enumerate(return_paths.items()):
        equity = 100.0
        daily = []
        trades = []
        for index, (day, value) in enumerate(zip(dates, returns)):
            next_equity = equity * (1.0 + value / 100.0)
            daily.append({
                "date": day,
                "pnl": next_equity - equity,
                "equity": next_equity,
                "trades": 1,
            })
            trade = {
                "date": day,
                "entry_time": base_timestamp + index * 86_400_000 + candidate_index * 100,
                "net_pct": value,
            }
            if name != "missing_exit":
                trade["exit_time"] = trade["entry_time"] + 3_600_000
            trades.append(trade)
            equity = next_equity
        results[name] = {
            "all": {"trades": 20},
            "risk_weighted_portfolio": {"initial_equity": 100.0, "daily": daily},
            "trades": trades,
        }

    document = {
        "effective_sessions": {"count": 20, "start": dates[0], "end": dates[-1]},
        "results": results,
    }
    (tmp_path / "okx_gap_beta_research.json").write_text(json.dumps(document))

    report = audit(tmp_path)
    assert report["cscv_pbo"]["candidate_count"] == 5
    purged = report["purged_embargo_cscv_pbo"]
    assert purged["available"] is True
    assert purged["candidate_count"] == 4
    assert purged["purging_applied"] is True
    assert purged["embargo_applied"] is True
    assert purged["overlap_purged_sessions_across_splits"] == 0
    assert purged["embargoed_sessions_across_splits"] > 0
    assert purged["same_interval_capable_cohort_references"]["unpurged"]["candidate_count"] == 4
    assert (
        purged["same_interval_capable_cohort_references"]["purged_without_embargo"]
        ["overlap_purged_sessions_across_splits"]
        == 0
    )
    assert purged["excluded_candidate_ids"] == [
        "okx_gap_beta_research::results.missing_exit",
    ]
    excluded = next(
        item for item in report["candidate_registry"]
        if item["candidate_id"].endswith("::results.missing_exit")
    )
    assert excluded["label_interval_evidence"]["available"] is False
    assert excluded["label_interval_evidence"]["reason"].startswith(
        "explicit_entry_exit_timestamp_missing_or_ambiguous"
    )


@pytest.mark.parametrize(
    "artifact_name",
    ["okx_gap_stop_trigger_research.json", "okx_gap_mark_stop_parity.json"],
)
def test_artifact_governance_registers_diagnostics_but_excludes_selection(
    tmp_path: Path,
    artifact_name: str,
):
    from datetime import date, timedelta

    dates = []
    cursor = date(2026, 1, 5)
    while len(dates) < 20:
        if cursor.weekday() < 5:
            dates.append(cursor.isoformat())
        cursor += timedelta(days=1)

    def candidate(offset: float):
        equity = 100.0
        daily = []
        trades = []
        for index, day in enumerate(dates):
            value = offset + (0.1 if index % 2 else -0.05)
            next_equity = equity * (1.0 + value / 100.0)
            daily.append({
                "date": day,
                "trades": 1,
                "pnl": next_equity - equity,
                "equity": next_equity,
            })
            start = 1_767_600_000_000 + index * 86_400_000
            trades.append({
                "date": day,
                "label_start_time": start,
                "label_end_time": start + 300_000,
                "net_pct": value,
            })
            equity = next_equity
        return {
            "all": {"trades": 20},
            "risk_weighted_portfolio": {"initial_equity": 100.0, "daily": daily},
            "trades": trades,
        }

    document = {
        "effective_sessions": {"count": 20, "start": dates[0], "end": dates[-1]},
        "research_governance": {
            "diagnostic_only": True,
            "selection_eligible": False,
            "explicit_trial_count": 2,
            "trial_count_method": "two_predeclared_diagnostics",
        },
        "baseline": candidate(0.0),
        "diagnostics": {
            "first_diagnostic": candidate(0.01),
            "second_diagnostic": candidate(-0.01),
        },
    }
    path = tmp_path / artifact_name
    path.write_text(json.dumps(document))

    report = audit(tmp_path)
    candidates = [
        item for item in report["candidate_registry"]
        if item.get("artifact") == str(path)
    ]
    assert len(candidates) == 3
    assert all(item["diagnostic_only"] is True for item in candidates)
    assert all(item["selection_bias_eligible"] is False for item in candidates)
    assert all(item["label_interval_evidence"]["available"] is True for item in candidates)
    assert report["candidate_counts"]["reported_comparable_before_exact_deduplication"] == 0
    governance = report["historical_trial_governance"]
    assert governance["declared_historical_trial_lower_bound"] == 2
    assert governance["evidence"] == [{
        "artifact": str(path),
        "components": [{
            "path": "research_governance.explicit_trial_count",
            "count": 2,
            "count_method": "two_predeclared_diagnostics",
            "diagnostic_only": True,
            "selection_eligible": False,
        }],
        "within_artifact_total": 2,
        "visible_eligible_config_count": 0,
        "not_added_again": [],
        "deduplication_note": (
            "Artifact-level explicit trials are registered, but are not added across artifacts "
            "without persisted evidence that their research lineages are disjoint."
        ),
    }]
