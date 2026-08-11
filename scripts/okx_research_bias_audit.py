#!/usr/bin/env python3
"""Read-only audit of selection bias across the existing OKX research artifacts.

The audit never calls OKX, never imports the trading runtime, and never rewrites
its source artifacts.  It inventories the candidate families already persisted
under ``data/``, reconstructs daily portfolio returns only when the artifact
contains enough evidence, fills verified no-trade sessions with zero, and then
applies conservative multiple-trial diagnostics to an exactly comparable set.

Artifacts that contain only aggregate metrics remain in the registry with an
explicit ``unavailable`` reason.  They are never reverse-engineered into a
synthetic return path.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import NormalDist, mean, median
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = ROOT / "data"
OUTPUT = DEFAULT_DATA_DIR / "okx_research_bias_audit.json"
UTC = timezone.utc
EPSILON = 1e-12


FAMILY_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("historical_search", ("okx_gap_research_90d.json",)),
    ("v2", ("okx_gap_strategy_v2_backtest*.json",)),
    ("v3", ("okx_gap_strategy_v3*.json",)),
    ("v4", ("okx_gap_strategy_v4_backtest.json",)),
    ("v5", ("okx_gap_strategy_v5_backtest.json",)),
    ("entry_delay", ("okx_gap_entry_delay_research.json",)),
    ("beta", ("okx_gap_beta_research.json",)),
    ("benchmark", ("okx_gap_benchmark*_research.json",)),
    ("confidence_sizing", ("okx_gap_confidence_sizing_research.json",)),
    ("symbol_edge", ("okx_gap_symbol_edge_research.json",)),
    ("continuation", ("okx_gap_continuation_research.json",)),
    ("confirmation_overlay", ("okx_gap_confirmation_overlay_research.json",)),
    ("exit_management", (
        "okx_gap_execution_management_research.json",
        "okx_gap_stop_trigger_research.json",
        "okx_gap_mark_stop_parity.json",
    )),
)


@dataclass
class CandidateSource:
    logical_path: str
    method: str | None = None
    portfolio: dict[str, Any] | None = None
    trades: list[dict[str, Any]] | None = None
    reported_trade_count: int | None = None
    unavailable_reason: str | None = None
    diagnostic_only: bool = False
    declared_trial_count: int | None = None
    declared_parameters: dict[str, Any] | None = None


LabelInterval = tuple[int, int]
DailyLabelIntervals = list[list[LabelInterval]]


def _round(value: float | None, digits: int = 6) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), digits)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _weekdays(start: str, end: str) -> list[str]:
    first = date.fromisoformat(start)
    last = date.fromisoformat(end)
    if last < first:
        raise ValueError("calendar end precedes start")
    result: list[str] = []
    cursor = first
    while cursor <= last:
        if cursor.weekday() < 5:
            result.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return result


def artifact_calendar(document: dict[str, Any]) -> tuple[list[str] | None, str | None, str | None]:
    """Return a calendar only when the artifact supplies an exact date basis."""
    effective_dates = document.get("effective_dates")
    if isinstance(effective_dates, list) and effective_dates and all(
        isinstance(item, str) for item in effective_dates
    ):
        dates = sorted(set(effective_dates))
        return dates, "effective_dates", None

    for key in ("effective_sessions", "sessions", "range"):
        value = document.get(key)
        if not isinstance(value, dict):
            continue
        start, end = value.get("start"), value.get("end")
        if not isinstance(start, str) or not isinstance(end, str):
            continue
        try:
            dates = _weekdays(start, end)
        except (TypeError, ValueError) as exc:
            return None, None, f"invalid_{key}_range:{exc}"
        expected = value.get("count", value.get("sessions"))
        if isinstance(expected, int) and expected != len(dates):
            return None, None, f"{key}_count_mismatch:{expected}!={len(dates)}"
        return dates, f"{key}.start_end_weekdays", None

    effective_range = document.get("effective_range")
    effective_count = document.get("effective_sessions")
    if isinstance(effective_range, dict):
        start, end = effective_range.get("start"), effective_range.get("end")
        if isinstance(start, str) and isinstance(end, str):
            dates = _weekdays(start, end)
            if isinstance(effective_count, int) and effective_count != len(dates):
                return None, None, f"effective_range_count_mismatch:{effective_count}!={len(dates)}"
            return dates, "effective_range.start_end_weekdays", None

    month_range = document.get("month_range")
    if isinstance(month_range, str) and ".." in month_range:
        start, end = month_range.split("..", 1)
        try:
            return _weekdays(start, end), "month_range_weekdays", None
        except ValueError as exc:
            return None, None, f"invalid_month_range:{exc}"
    return None, None, "artifact_has_no_exact_session_calendar"


def _is_trade_list(value: Any) -> bool:
    return bool(
        isinstance(value, list)
        and value
        and all(isinstance(item, dict) for item in value)
        and all("date" in item and _number(item.get("net_pct")) is not None for item in value)
    )


def _is_daily_portfolio(value: Any) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("daily"), list):
        return False
    rows = value["daily"]
    return all(
        isinstance(row, dict)
        and isinstance(row.get("date"), str)
        and (
            _number(row.get("return_pct")) is not None
            or (_number(row.get("pnl")) is not None and _number(row.get("equity")) is not None)
        )
        for row in rows
    )


def _logical_path(parts: tuple[str, ...]) -> str:
    if parts == ("metrics",):
        return "v5"
    return ".".join(parts) if parts else "root"


def _is_diagnostic_path(path: str) -> bool:
    """Keep post-selection robustness/stress views out of the trial matrix."""
    diagnostic_segments = {
        "cost_sensitivity",
        "tail_robustness",
        "baseline_tail_robustness",
        "folds",
        "leave_one_out",
        "stress",
        "stress_test",
        "stress_tests",
    }
    for segment in path.lower().split("."):
        if segment in diagnostic_segments:
            return True
        if segment.startswith(("recent_", "leave_one_out", "stress_")):
            return True
        if segment.endswith(("_stress", "_diagnostic", "_diagnostics")):
            return True
    return False


def _metric_trade_count(value: Any) -> int | None:
    if not isinstance(value, dict):
        return None
    direct = value.get("trades")
    if isinstance(direct, int):
        return direct
    all_metrics = value.get("all")
    if isinstance(all_metrics, dict) and isinstance(all_metrics.get("trades"), int):
        return int(all_metrics["trades"])
    return None


def _generic_candidate_sources(document: dict[str, Any]) -> list[CandidateSource]:
    """Discover persisted candidate paths without interpreting summary metrics as returns."""
    found: dict[str, CandidateSource] = {}

    def put(source: CandidateSource, priority: int) -> None:
        current = found.get(source.logical_path)
        current_priority = 2 if current and current.portfolio is not None else 1 if current and current.trades else 0
        # A candidate commonly persists the risk-weighted portfolio and its
        # trade rows as sibling fields.  Keep both evidence forms: portfolio
        # returns establish comparability, while trade rows are the only honest
        # source for label start/end intervals.
        if current is not None:
            if source.portfolio is None:
                source.portfolio = current.portfolio
            if source.trades is None:
                source.trades = current.trades
            if source.reported_trade_count is None:
                source.reported_trade_count = current.reported_trade_count
        if priority >= current_priority:
            found[source.logical_path] = source
        elif current is not None:
            if current.portfolio is None:
                current.portfolio = source.portfolio
            if current.trades is None:
                current.trades = source.trades
            if current.reported_trade_count is None:
                current.reported_trade_count = source.reported_trade_count

    def walk(value: Any, parts: tuple[str, ...] = ()) -> None:
        if not isinstance(value, dict):
            return

        risk_portfolio = value.get("risk_weighted_portfolio")
        if _is_daily_portfolio(risk_portfolio):
            path = _logical_path(parts)
            put(CandidateSource(
                logical_path=path,
                method="reported_risk_weighted_portfolio",
                portfolio=risk_portfolio,
                reported_trade_count=_metric_trade_count(value),
                diagnostic_only=_is_diagnostic_path(path),
            ), 2)

        if parts and parts[-1] == "all" and _is_daily_portfolio(value):
            # Some artifacts wrap the all-period curve as
            # risk_weighted_portfolio.all, while older ones persist the curve
            # directly.  Both describe the surrounding candidate.
            candidate_parts = (
                parts[:-2]
                if len(parts) >= 2 and parts[-2] == "risk_weighted_portfolio"
                else parts[:-1]
            )
            path = _logical_path(candidate_parts)
            put(CandidateSource(
                logical_path=path,
                method="reported_risk_weighted_portfolio",
                portfolio=value,
                reported_trade_count=_metric_trade_count(value),
                diagnostic_only=_is_diagnostic_path(path),
            ), 2)

        trades = value.get("trades")
        if _is_trade_list(trades):
            path = _logical_path(parts)
            put(CandidateSource(
                logical_path=path,
                method="equal_weight_trade_day_proxy",
                trades=trades,
                reported_trade_count=len(trades),
                diagnostic_only=_is_diagnostic_path(path),
            ), 1)

        all_metrics = value.get("all")
        if (
            isinstance(all_metrics, dict)
            and isinstance(all_metrics.get("trades"), int)
            and _logical_path(parts) not in found
        ):
            path = _logical_path(parts)
            put(CandidateSource(
                logical_path=path,
                reported_trade_count=int(all_metrics["trades"]),
                unavailable_reason="summary_metrics_without_daily_returns_or_trade_rows",
                diagnostic_only=_is_diagnostic_path(path),
            ), 0)

        for key, nested in value.items():
            if key in {"trades", "daily"}:
                continue
            if key == "risk_weighted_portfolio" and _is_daily_portfolio(nested):
                continue
            walk(nested, parts + (str(key),))

    walk(document)
    return list(found.values())


def _v2_sources(document: dict[str, Any]) -> list[CandidateSource]:
    sources: list[CandidateSource] = []
    if isinstance(document.get("baseline_strategy"), dict):
        sources.append(CandidateSource(
            logical_path="baseline_strategy",
            reported_trade_count=_metric_trade_count(document["baseline_strategy"]),
            unavailable_reason="baseline_has_summary_metrics_only",
        ))
    trades = document.get("trades")
    if _is_trade_list(trades):
        sources.append(CandidateSource(
            logical_path="v2",
            method="equal_weight_trade_day_proxy",
            trades=trades,
            reported_trade_count=len(trades),
        ))
    else:
        sources.append(CandidateSource(
            logical_path="v2",
            reported_trade_count=int(trades) if isinstance(trades, int) else None,
            unavailable_reason="aggregate_trade_count_without_trade_rows",
        ))
    return sources


def _v3_sources(document: dict[str, Any], artifact_name: str) -> list[CandidateSource]:
    trades = document.get("trades")
    if not _is_trade_list(trades):
        return [CandidateSource(
            logical_path="v3_model",
            reported_trade_count=int(trades) if isinstance(trades, int) else None,
            unavailable_reason="aggregate_trade_count_without_trade_rows",
        )]

    if "no_timeout" in artifact_name:
        logical_path = "no_timeout_month"
    elif "last_month" in artifact_name:
        logical_path = "month"
    elif "this_week" in artifact_name:
        logical_path = "week"
    else:
        logical_path = "v3_model"
    result = [CandidateSource(
        logical_path=logical_path,
        method="equal_weight_trade_day_proxy",
        trades=trades,
        reported_trade_count=len(trades),
    )]
    if "no_timeout" in artifact_name:
        for name in ("fixed_90m_month", "no_timeout_week", "fixed_90m_week"):
            metrics = document.get(name)
            result.append(CandidateSource(
                logical_path=name,
                reported_trade_count=_metric_trade_count(metrics),
                unavailable_reason="summary_metrics_without_matching_trade_rows",
            ))
    return result


def _protected_config_identity(value: dict[str, Any]) -> dict[str, Any]:
    preferred = ("stop_r", "breakeven_at_1r", "state_lookback_days")
    identity = {key: value[key] for key in preferred if key in value}
    if identity:
        return identity
    return {
        str(key): item
        for key, item in value.items()
        if not isinstance(item, (dict, list))
    }


def _historical_search_sources(document: dict[str, Any]) -> list[CandidateSource]:
    """Register explicit historical attempts even when their paths are absent."""
    result: list[CandidateSource] = []
    tested = document.get("tested")
    if isinstance(tested, int) and tested > 0:
        result.append(CandidateSource(
            logical_path="tested_grid_aggregate",
            unavailable_reason="declared_aggregate_trials_without_daily_return_paths",
            declared_trial_count=tested,
        ))

    protected = document.get("protected_state_diagnostics")
    unique: dict[str, dict[str, Any]] = {}
    if isinstance(protected, list):
        for value in protected:
            if not isinstance(value, dict):
                continue
            identity = _protected_config_identity(value)
            key = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            unique[key] = identity
    for index, key in enumerate(sorted(unique), start=1):
        result.append(CandidateSource(
            logical_path=f"protected_state_diagnostics.config_{index:02d}",
            unavailable_reason="aggregate_metrics_without_daily_return_path",
            declared_trial_count=1,
            declared_parameters=unique[key],
        ))
    return result


def candidate_sources(family: str, document: dict[str, Any], artifact_name: str) -> list[CandidateSource]:
    if family == "historical_search":
        sources = _historical_search_sources(document)
    elif family == "v2":
        sources = _v2_sources(document)
    elif family == "v3":
        sources = _v3_sources(document, artifact_name)
    else:
        sources = _generic_candidate_sources(document)
    artifact_governance = document.get("research_governance")
    artifact_diagnostic_only = bool(
        isinstance(artifact_governance, dict)
        and artifact_governance.get("diagnostic_only") is True
    )
    if artifact_diagnostic_only:
        for source in sources:
            source.diagnostic_only = True
    if family != "v5":
        return sources

    # V5 stores its selected portfolio below ``metrics`` but its matching trade
    # rows at the document root.  These are two evidence forms for one strategy,
    # not two research trials.  Prefer the reported portfolio over the proxy.
    selected: CandidateSource | None = None
    root_trade_source: CandidateSource | None = None
    result: list[CandidateSource] = []
    for source in sources:
        if source.logical_path not in {"root", "v5"}:
            result.append(source)
            continue
        if source.logical_path == "root" and source.trades is not None:
            root_trade_source = source
        if selected is None or (selected.portfolio is None and source.portfolio is not None):
            selected = source
    if selected is not None:
        if selected.trades is None and root_trade_source is not None:
            selected.trades = root_trade_source.trades
        if selected.reported_trade_count is None and root_trade_source is not None:
            selected.reported_trade_count = root_trade_source.reported_trade_count
        selected.logical_path = "v5"
        result.append(selected)
    return result


def portfolio_daily_returns(
    portfolio: dict[str, Any], calendar: list[str],
) -> tuple[list[float] | None, str | None]:
    """Reconstruct returns from the reported equity curve, preserving zero days."""
    rows = portfolio.get("daily")
    if not isinstance(rows, list):
        return None, "portfolio_daily_rows_missing"
    by_date: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("date"), str):
            return None, "invalid_portfolio_daily_row"
        day = str(row["date"])
        if day in by_date:
            return None, f"duplicate_portfolio_day:{day}"
        by_date[day] = row
    outside = sorted(set(by_date).difference(calendar))
    if outside:
        return None, f"portfolio_days_outside_verified_calendar:{','.join(outside[:3])}"

    initial = _number(portfolio.get("initial_equity"))
    if initial is None and rows:
        first = sorted(rows, key=lambda item: str(item["date"]))[0]
        equity, pnl = _number(first.get("equity")), _number(first.get("pnl"))
        if equity is not None and pnl is not None:
            initial = equity - pnl
    if initial is None or initial <= 0:
        return None, "initial_equity_unavailable"

    current_equity = initial
    result: list[float] = []
    for day in calendar:
        row = by_date.get(day)
        if row is None:
            result.append(0.0)
            continue
        explicit = _number(row.get("return_pct"))
        equity = _number(row.get("equity"))
        if explicit is not None:
            value = explicit
            if equity is not None:
                current_equity = equity
            else:
                current_equity *= 1.0 + value / 100.0
        elif equity is not None and current_equity > 0:
            value = (equity / current_equity - 1.0) * 100.0
            current_equity = equity
        else:
            return None, f"cannot_reconstruct_portfolio_return:{day}"
        if not math.isfinite(value):
            return None, f"non_finite_portfolio_return:{day}"
        result.append(value)
    return result, None


def trade_proxy_daily_returns(
    trades: list[dict[str, Any]], calendar: list[str],
) -> tuple[list[float] | None, str | None]:
    """Build a clearly-labelled fully-invested equal-weight trade-day proxy."""
    grouped: dict[str, list[float]] = defaultdict(list)
    for trade in trades:
        day = trade.get("date")
        value = _number(trade.get("net_pct"))
        if not isinstance(day, str) or value is None:
            return None, "invalid_trade_row"
        grouped[day].append(value)
    outside = sorted(set(grouped).difference(calendar))
    if outside:
        return None, f"trade_days_outside_verified_calendar:{','.join(outside[:3])}"
    return [mean(grouped[day]) if grouped.get(day) else 0.0 for day in calendar], None


def _epoch_milliseconds(value: Any) -> int | None:
    """Parse an explicitly persisted timestamp without guessing seconds vs ms."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        numeric = float(value)
        # Current artifacts persist Unix epoch milliseconds.  Smaller numbers
        # are ambiguous (often epoch seconds), so accepting them would silently
        # manufacture an interval unit.
        if numeric < 100_000_000_000 or not numeric.is_integer():
            return None
        return int(numeric)
    if isinstance(value, str):
        normalized = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return int(parsed.timestamp() * 1000.0)
    return None


def reported_label_intervals(
    trades: list[dict[str, Any]],
    calendar: list[str],
    *,
    portfolio: dict[str, Any] | None = None,
    reported_trade_count: int | None = None,
) -> tuple[DailyLabelIntervals | None, dict[str, Any]]:
    """Validate and align persisted holding intervals to the session calendar.

    No interval is inferred from ``horizon_minutes`` or ``exit_reason``.  A
    candidate is interval-capable only when every trade row carries either an
    explicit label-start/label-end pair or an explicit entry/exit pair, and
    those rows reconcile with any persisted daily trade counts.
    """
    if reported_trade_count is not None and reported_trade_count != len(trades):
        return None, {
            "available": False,
            "reason": f"reported_trade_count_mismatch:{reported_trade_count}!={len(trades)}",
        }
    calendar_index = {day: index for index, day in enumerate(calendar)}
    grouped: dict[str, list[LabelInterval]] = defaultdict(list)
    timestamp_field_pairs: set[str] = set()
    for row_index, trade in enumerate(trades):
        day = trade.get("date")
        if not isinstance(day, str) or day not in calendar_index:
            return None, {
                "available": False,
                "reason": f"trade_interval_day_outside_verified_calendar:{day}",
            }
        label_pair_present = "label_start_time" in trade or "label_end_time" in trade
        execution_pair_present = "entry_time" in trade or "exit_time" in trade
        label_start = _epoch_milliseconds(trade.get("label_start_time"))
        label_end = _epoch_milliseconds(trade.get("label_end_time"))
        execution_start = _epoch_milliseconds(trade.get("entry_time"))
        execution_end = _epoch_milliseconds(trade.get("exit_time"))
        if label_pair_present and (label_start is None or label_end is None):
            return None, {
                "available": False,
                "reason": f"explicit_label_timestamp_pair_incomplete_or_ambiguous:row_{row_index}",
            }
        if execution_pair_present and (execution_start is None or execution_end is None):
            # A complete explicit label pair is sufficient even when execution
            # fields are entirely absent, but a partially persisted execution
            # pair is contradictory evidence and must not be ignored.
            return None, {
                "available": False,
                "reason": f"explicit_entry_exit_timestamp_missing_or_ambiguous:row_{row_index}",
            }
        if label_start is not None and label_end is not None:
            start, end = label_start, label_end
            timestamp_field_pairs.add("label_start_time/label_end_time")
            if (
                execution_start is not None
                and execution_end is not None
                and (execution_start, execution_end) != (start, end)
            ):
                return None, {
                    "available": False,
                    "reason": f"label_and_execution_interval_disagree:row_{row_index}",
                }
        else:
            start, end = execution_start, execution_end
            timestamp_field_pairs.add("entry_time/exit_time")
        if start is None or end is None:
            return None, {
                "available": False,
                "reason": f"explicit_entry_exit_timestamp_missing_or_ambiguous:row_{row_index}",
            }
        if end < start:
            return None, {
                "available": False,
                "reason": f"exit_precedes_entry:row_{row_index}",
            }
        grouped[day].append((start, end))

    if portfolio is not None:
        rows = portfolio.get("daily")
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("date"), str):
                    continue
                expected = row.get("trades")
                if not isinstance(expected, int):
                    continue
                day = str(row["date"])
                actual = len(grouped.get(day, []))
                if expected != actual:
                    return None, {
                        "available": False,
                        "reason": f"portfolio_daily_trade_count_mismatch:{day}:{expected}!={actual}",
                    }

    aligned = [sorted(grouped.get(day, [])) for day in calendar]
    flattened = [interval for intervals in aligned for interval in intervals]
    payload = "\n".join(
        f"{day},{start},{end}"
        for day, intervals in zip(calendar, aligned)
        for start, end in intervals
    )
    evidence = {
        "available": True,
        "method": "explicit_persisted_label_intervals",
        "timestamp_field_pairs": sorted(timestamp_field_pairs),
        "timestamp_unit": "unix_epoch_milliseconds",
        "trade_rows": len(trades),
        "interval_count": len(flattened),
        "sessions_with_intervals": sum(bool(intervals) for intervals in aligned),
        "first_entry_time": min((start for start, _ in flattened), default=None),
        "last_exit_time": max((end for _, end in flattened), default=None),
        "interval_signature": hashlib.sha256(payload.encode()).hexdigest()[:16],
        "validation": "all_rows_explicit_and_daily_trade_counts_reconciled_when_available",
    }
    return aligned, evidence


def _sample_std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    center = mean(values)
    return math.sqrt(sum((item - center) ** 2 for item in values) / (len(values) - 1))


def _daily_sharpe(values: list[float]) -> float | None:
    deviation = _sample_std(values)
    return None if deviation <= EPSILON else mean(values) / deviation


def daily_return_stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"observations": 0, "reason": "empty_return_series"}
    average = mean(values)
    deviation = _sample_std(values)
    daily_sharpe = _daily_sharpe(values)
    equity = peak = 1.0
    max_drawdown = 0.0
    for value in values:
        equity *= 1.0 + value / 100.0
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak * 100.0)
    return {
        "observations": len(values),
        "active_days": sum(abs(item) > EPSILON for item in values),
        "positive_days": sum(item > EPSILON for item in values),
        "negative_days": sum(item < -EPSILON for item in values),
        "zero_days": sum(abs(item) <= EPSILON for item in values),
        "mean_daily_pct": _round(average),
        "median_daily_pct": _round(median(values)),
        "std_daily_pct": _round(deviation),
        "min_daily_pct": _round(min(values)),
        "max_daily_pct": _round(max(values)),
        "cumulative_return_pct": _round((equity - 1.0) * 100.0),
        "max_drawdown_pct": _round(max_drawdown),
        "daily_sharpe": _round(daily_sharpe),
        "annualized_sharpe": _round(daily_sharpe * math.sqrt(252.0)) if daily_sharpe is not None else None,
    }


def _skew_and_kurtosis(values: list[float]) -> tuple[float | None, float | None]:
    if len(values) < 3:
        return None, None
    center = mean(values)
    variance = sum((value - center) ** 2 for value in values) / len(values)
    if variance <= EPSILON:
        return None, None
    scale = math.sqrt(variance)
    skew = sum(((value - center) / scale) ** 3 for value in values) / len(values)
    kurtosis = sum(((value - center) / scale) ** 4 for value in values) / len(values)
    return skew, kurtosis


def expected_max_sharpe(trial_sharpes: list[float], trial_count: int) -> float:
    """Expected maximum daily Sharpe under the DSR trial-count approximation."""
    if trial_count <= 1 or len(trial_sharpes) < 2:
        return 0.0
    deviation = _sample_std(trial_sharpes)
    if deviation <= EPSILON:
        return 0.0
    normal = NormalDist()
    euler_gamma = 0.5772156649015329
    first = normal.inv_cdf(max(EPSILON, min(1.0 - EPSILON, 1.0 - 1.0 / trial_count)))
    second = normal.inv_cdf(
        max(EPSILON, min(1.0 - EPSILON, 1.0 - 1.0 / (trial_count * math.e)))
    )
    return deviation * ((1.0 - euler_gamma) * first + euler_gamma * second)


def deflated_sharpe_probability(
    values: list[float], trial_sharpes: list[float], trial_count: int,
) -> dict[str, Any]:
    observed = _daily_sharpe(values)
    if observed is None:
        return {"available": False, "reason": "zero_variance_return_series"}
    if len(values) < 3:
        return {"available": False, "reason": "fewer_than_three_daily_observations"}
    skew, kurtosis = _skew_and_kurtosis(values)
    if skew is None or kurtosis is None:
        return {"available": False, "reason": "higher_moments_unavailable"}
    benchmark = expected_max_sharpe(trial_sharpes, trial_count)
    denominator_sq = 1.0 - skew * observed + ((kurtosis - 1.0) / 4.0) * observed ** 2
    if denominator_sq <= EPSILON:
        return {"available": False, "reason": "non_positive_psr_denominator"}
    z_score = (observed - benchmark) * math.sqrt(len(values) - 1) / math.sqrt(denominator_sq)
    probability = NormalDist().cdf(z_score)
    return {
        "available": True,
        "observations": len(values),
        "trial_count": trial_count,
        "observed_daily_sharpe": _round(observed),
        "observed_annualized_sharpe": _round(observed * math.sqrt(252.0)),
        "selection_benchmark_daily_sharpe": _round(benchmark),
        "skewness": _round(skew),
        "raw_kurtosis": _round(kurtosis),
        "z_score": _round(z_score),
        "deflated_sharpe_probability": _round(probability),
    }


def _ranking_score(values: Iterable[float]) -> float:
    sequence = list(values)
    if not sequence:
        return -math.inf
    deviation = _sample_std(sequence)
    average = mean(sequence)
    if deviation <= EPSILON:
        if average > EPSILON:
            return math.inf
        if average < -EPSILON:
            return -math.inf
        return 0.0
    return average / deviation


def _overlaps(left: LabelInterval, right: LabelInterval) -> bool:
    return left[0] <= right[1] and right[0] <= left[1]


def _contiguous_ranges(indices: list[int]) -> list[tuple[int, int]]:
    if not indices:
        return []
    ordered = sorted(set(indices))
    result: list[tuple[int, int]] = []
    start = previous = ordered[0]
    for index in ordered[1:]:
        if index != previous + 1:
            result.append((start, previous))
            start = index
        previous = index
    result.append((start, previous))
    return result


def _purge_and_embargo_indices(
    in_sample: list[int],
    out_sample: list[int],
    global_intervals: DailyLabelIntervals,
    embargo_sessions: int,
) -> tuple[list[int], set[int], set[int]]:
    """Return a common leakage-safe IS index set for every candidate.

    The interval union across candidates is intentional: candidate ranking must
    use the same IS observations, so a day is removed for all candidates when
    any persisted candidate label overlaps any OOS label.
    """
    test_intervals = [
        interval for index in out_sample for interval in global_intervals[index]
    ]
    overlap_purged = {
        index
        for index in in_sample
        if any(
            _overlaps(interval, test_interval)
            for interval in global_intervals[index]
            for test_interval in test_intervals
        )
    }
    in_set = set(in_sample)
    embargoed: set[int] = set()
    if embargo_sessions:
        for _, test_end in _contiguous_ranges(out_sample):
            for index in range(test_end + 1, min(len(global_intervals), test_end + 1 + embargo_sessions)):
                if index in in_set:
                    embargoed.add(index)
    excluded = overlap_purged | embargoed
    return [index for index in in_sample if index not in excluded], overlap_purged, embargoed


def cscv_pbo(
    series_by_candidate: dict[str, list[float]],
    block_count: int | None = None,
    *,
    label_intervals_by_candidate: dict[str, DailyLabelIntervals] | None = None,
    embargo_sessions: int = 0,
) -> dict[str, Any]:
    """Estimate PBO using CSCV on an already aligned daily-return matrix.

    When complete explicit label intervals are supplied, each split uses a
    common candidate-union purge mask plus a session embargo.  Without that
    evidence the legacy unpurged CSCV result remains available and is labelled
    as such instead of inferring holding periods from strategy parameters.
    """
    if len(series_by_candidate) < 4:
        return {"available": False, "reason": "fewer_than_four_unique_candidates"}
    lengths = {len(values) for values in series_by_candidate.values()}
    if len(lengths) != 1:
        return {"available": False, "reason": "candidate_return_lengths_differ"}
    observations = lengths.pop()
    if observations < 20:
        return {"available": False, "reason": "fewer_than_twenty_common_sessions"}
    if block_count is None:
        block_count = 8 if observations >= 64 else 6 if observations >= 36 else 4
    if block_count < 4 or block_count % 2:
        return {"available": False, "reason": "block_count_must_be_even_and_at_least_four"}
    if observations < block_count * 2:
        return {"available": False, "reason": "fewer_than_two_observations_per_block"}

    if not isinstance(embargo_sessions, int) or embargo_sessions < 0:
        return {"available": False, "reason": "embargo_sessions_must_be_non_negative_integer"}

    global_intervals: DailyLabelIntervals | None = None
    if label_intervals_by_candidate is not None:
        if set(label_intervals_by_candidate) != set(series_by_candidate):
            return {"available": False, "reason": "label_interval_candidates_differ"}
        for name, intervals_by_day in label_intervals_by_candidate.items():
            if len(intervals_by_day) != observations:
                return {
                    "available": False,
                    "reason": f"label_interval_observation_count_differs:{name}",
                }
            for intervals in intervals_by_day:
                if not isinstance(intervals, list) or any(
                    not isinstance(interval, tuple)
                    or len(interval) != 2
                    or not all(isinstance(value, int) for value in interval)
                    or interval[1] < interval[0]
                    for interval in intervals
                ):
                    return {"available": False, "reason": f"invalid_label_interval:{name}"}
        global_intervals = [
            sorted(
                interval
                for name in sorted(label_intervals_by_candidate)
                for interval in label_intervals_by_candidate[name][index]
            )
            for index in range(observations)
        ]
        if not any(global_intervals):
            return {"available": False, "reason": "no_explicit_label_intervals"}

    names = sorted(series_by_candidate)
    blocks = [
        list(range(index * observations // block_count, (index + 1) * observations // block_count))
        for index in range(block_count)
    ]
    overfit = 0
    lambdas: list[float] = []
    ranks: list[float] = []
    degradation: list[float] = []
    selection_counts: Counter[str] = Counter()
    overlap_purge_counts: list[int] = []
    embargo_counts: list[int] = []
    retained_in_sample_counts: list[int] = []
    for chosen_blocks in itertools.combinations(range(block_count), block_count // 2):
        chosen = set(chosen_blocks)
        in_sample = [item for index, block in enumerate(blocks) if index in chosen for item in block]
        out_sample = [item for index, block in enumerate(blocks) if index not in chosen for item in block]
        if global_intervals is not None:
            in_sample, overlap_purged, embargoed = _purge_and_embargo_indices(
                in_sample, out_sample, global_intervals, embargo_sessions,
            )
            overlap_purge_counts.append(len(overlap_purged))
            embargo_counts.append(len(embargoed))
            retained_in_sample_counts.append(len(in_sample))
            if len(in_sample) < 2:
                return {
                    "available": False,
                    "reason": "fewer_than_two_in_sample_sessions_after_purge_embargo",
                }
        in_scores = {
            name: _ranking_score(series_by_candidate[name][index] for index in in_sample)
            for name in names
        }
        winner = max(names, key=lambda name: (in_scores[name], name))
        selection_counts[winner] += 1
        out_scores = {
            name: _ranking_score(series_by_candidate[name][index] for index in out_sample)
            for name in names
        }
        winner_score = out_scores[winner]
        less = sum(score < winner_score for score in out_scores.values())
        equal = sum(score == winner_score for score in out_scores.values())
        average_rank = less + (equal + 1.0) / 2.0
        relative_rank = average_rank / (len(names) + 1.0)
        relative_rank = max(EPSILON, min(1.0 - EPSILON, relative_rank))
        logit = math.log(relative_rank / (1.0 - relative_rank))
        ranks.append(relative_rank)
        lambdas.append(logit)
        if logit <= 0.0:
            overfit += 1
        score_delta = out_scores[winner] - in_scores[winner]
        if math.isfinite(score_delta):
            degradation.append(score_delta)

    result = {
        "available": True,
        "method": (
            "purged_embargoed_combinatorially_symmetric_cross_validation"
            if global_intervals is not None
            else "combinatorially_symmetric_cross_validation"
        ),
        "candidate_count": len(names),
        "observations": observations,
        "block_count": block_count,
        "combinations": len(lambdas),
        "pbo_probability": _round(overfit / len(lambdas)),
        "median_selected_oos_rank_pct": _round(median(ranks) * 100.0),
        "median_logit_rank": _round(median(lambdas)),
        "mean_selected_score_degradation": _round(mean(degradation)) if degradation else None,
        "selection_counts": dict(sorted(selection_counts.items())),
        "purging_applied": global_intervals is not None,
    }
    if global_intervals is None:
        result["embargo_applied"] = False
        result["purging_note"] = (
            "No complete explicit candidate label-interval matrix was supplied; this is unpurged CSCV."
        )
    else:
        result.update({
            "embargo_applied": embargo_sessions > 0,
            "embargo_sessions": embargo_sessions,
            "label_interval_method": "candidate_union_of_explicit_persisted_label_intervals",
            "label_interval_count": sum(len(intervals) for intervals in global_intervals),
            "sessions_with_label_intervals": sum(bool(intervals) for intervals in global_intervals),
            "overlap_purged_sessions_across_splits": sum(overlap_purge_counts),
            "embargoed_sessions_across_splits": sum(embargo_counts),
            "minimum_in_sample_sessions_after_purge_embargo": min(retained_in_sample_counts),
            "median_in_sample_sessions_after_purge_embargo": _round(median(retained_in_sample_counts)),
            "maximum_in_sample_sessions_after_purge_embargo": max(retained_in_sample_counts),
            "purging_note": (
                "IS observations use one conservative union mask across candidates; intervals are read "
                "only from explicit timestamp pairs, never inferred from horizons."
            ),
        })
    return result


def _calendar_signature(calendar: list[str]) -> str:
    return hashlib.sha256("\n".join(calendar).encode()).hexdigest()[:16]


def _series_signature(values: list[float]) -> str:
    payload = ",".join(f"{value:.10f}" for value in values)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _family_files(data_dir: Path, patterns: tuple[str, ...]) -> list[Path]:
    result: set[Path] = set()
    for pattern in patterns:
        result.update(path for path in data_dir.glob(pattern) if path.is_file())
    return sorted(result)


def declared_historical_trial_governance(
    discovered: list[
        tuple[str, Path, dict[str, Any], list[str] | None, str | None, str | None]
    ],
    comparable_path_count: int,
    comparable_unique_count: int,
) -> dict[str, Any]:
    """Build a non-additive lower bound from counts explicitly persisted in artifacts.

    The 90-day artifact declares one base grid in ``tested`` and a second,
    separately named protected-state grid.  Within that artifact those two
    groups are additive.  Its eligible/selected/forward entries are descendants
    of the grids and are not counted again.  Across research artifacts, lineage
    is not sufficiently persisted to prove disjointness, so this audit takes the
    maximum of declared experiment totals and the aligned candidate-path count
    rather than summing and pretending independence.
    """
    evidence: list[dict[str, Any]] = []
    declared_totals: list[int] = []
    for family, path, document, *_ in discovered:
        components: list[dict[str, Any]] = []
        visible_eligible: set[str] = set()
        not_added_again: list[str] = []
        notes: list[str] = []
        if family == "historical_search":
            tested = document.get("tested")
            tested_count = tested if isinstance(tested, int) and tested > 0 else 0
            if tested_count:
                components.append({
                    "path": "tested",
                    "count": tested_count,
                    "count_method": "explicit_integer",
                })

            protected = document.get("protected_state_diagnostics")
            protected_keys: set[str] = set()
            if isinstance(protected, list):
                for value in protected:
                    if not isinstance(value, dict):
                        continue
                    identity = _protected_config_identity(value)
                    protected_keys.add(json.dumps(
                        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                    ))
            if protected_keys:
                components.append({
                    "path": "protected_state_diagnostics",
                    "count": len(protected_keys),
                    "raw_list_length": len(protected) if isinstance(protected, list) else None,
                    "count_method": "unique_explicit_parameter_tuples",
                })

            eligible = document.get("eligible")
            if isinstance(eligible, list):
                for value in eligible:
                    if isinstance(value, dict) and isinstance(value.get("config"), list):
                        visible_eligible.add(json.dumps(value["config"], separators=(",", ":")))
            not_added_again.extend([
                "eligible",
                "selected_diagnostic",
                "final_diagnostic",
                "forward_candidate",
            ])
            notes.append(
                "Eligible and selected records are descendants of the declared grids; protected "
                "diagnostics are de-duplicated by their explicit parameter tuple."
            )

        governance = document.get("research_governance")
        if isinstance(governance, dict):
            explicit_trials = governance.get("explicit_trial_count")
            if isinstance(explicit_trials, int) and not isinstance(explicit_trials, bool) and explicit_trials > 0:
                components.append({
                    "path": "research_governance.explicit_trial_count",
                    "count": explicit_trials,
                    "count_method": str(governance.get("trial_count_method") or "explicit_integer"),
                    "diagnostic_only": governance.get("diagnostic_only") is True,
                    "selection_eligible": governance.get("selection_eligible"),
                })
                notes.append(
                    "Artifact-level explicit trials are registered, but are not added across artifacts "
                    "without persisted evidence that their research lineages are disjoint."
                )

        if not components:
            continue
        total = sum(int(component["count"]) for component in components)
        if total:
            declared_totals.append(total)
        evidence.append({
            "artifact": str(path),
            "components": components,
            "within_artifact_total": total,
            "visible_eligible_config_count": len(visible_eligible),
            "not_added_again": not_added_again,
            "deduplication_note": " ".join(notes),
        })

    declared_max = max(declared_totals, default=0)
    lower_bound = max(declared_max, comparable_path_count)
    return {
        "available": bool(lower_bound),
        "declared_historical_trial_lower_bound": lower_bound,
        "declared_experiment_total_max": declared_max,
        "aligned_comparable_path_count": comparable_path_count,
        "aligned_exact_unique_return_series_count": comparable_unique_count,
        "evidence": evidence,
        "cross_artifact_aggregation": "maximum_not_sum_due_to_unresolved_lineage_overlap",
        "scope_note": (
            "The lower bound covers explicitly declared but non-reconstructable attempts for DSR "
            "trial-count stress. It is not the candidate matrix used by CSCV/PBO."
        ),
        "completeness": "lower_bound_only_unpersisted_or_deleted_trials_remain_unknowable",
    }


def audit(data_dir: Path = DEFAULT_DATA_DIR) -> dict[str, Any]:
    data_dir = Path(data_dir)
    artifact_inventory: list[dict[str, Any]] = []
    loaded: list[tuple[str, Path, dict[str, Any], list[str] | None, str | None, str | None]] = []
    missing_candidates: list[dict[str, Any]] = []
    shared_calendar: list[str] | None = None

    discovered: list[tuple[str, Path, dict[str, Any], list[str] | None, str | None, str | None]] = []
    for family, patterns in FAMILY_PATTERNS:
        paths = _family_files(data_dir, patterns)
        if not paths:
            artifact_inventory.append({
                "family": family,
                "path": None,
                "patterns": list(patterns),
                "status": "missing",
                "reason": "no_matching_artifact",
            })
            missing_candidates.append({
                "candidate_id": f"{family}::missing_artifact",
                "family": family,
                "artifact": None,
                "candidate_path": None,
                "status": "unavailable",
                "reason": "no_matching_artifact",
                "selection_bias_eligible": False,
            })
            continue
        for path in paths:
            try:
                document = json.loads(path.read_text())
                if not isinstance(document, dict):
                    raise ValueError("JSON root is not an object")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                artifact_inventory.append({
                    "family": family,
                    "path": str(path),
                    "patterns": list(patterns),
                    "status": "malformed",
                    "reason": str(exc),
                })
                missing_candidates.append({
                    "candidate_id": f"{path.stem}::malformed_artifact",
                    "family": family,
                    "artifact": str(path),
                    "candidate_path": None,
                    "status": "unavailable",
                    "reason": f"malformed_artifact:{exc}",
                    "selection_bias_eligible": False,
                })
                continue
            calendar, calendar_source, calendar_error = artifact_calendar(document)
            discovered.append((family, path, document, calendar, calendar_source, calendar_error))
            if family == "v5" and calendar:
                shared_calendar = calendar

    for family, path, document, calendar, calendar_source, calendar_error in discovered:
        if calendar is None and family in {
            "entry_delay", "beta", "benchmark", "confidence_sizing",
            "symbol_edge", "continuation", "exit_management",
        } and shared_calendar:
            calendar = list(shared_calendar)
            calendar_source = "shared_v5_effective_sessions"
            calendar_error = None
        sources = candidate_sources(family, document, path.name)
        artifact_inventory.append({
            "family": family,
            "path": str(path),
            "patterns": next(list(patterns) for name, patterns in FAMILY_PATTERNS if name == family),
            "status": "loaded",
            "generated_at": document.get("generated_at"),
            "research_governance": document.get("research_governance"),
            "candidate_count": len(sources),
            "calendar": ({
                "sessions": len(calendar),
                "start": calendar[0],
                "end": calendar[-1],
                "source": calendar_source,
            } if calendar else None),
            "calendar_error": calendar_error,
        })
        loaded.append((family, path, document, calendar, calendar_source, calendar_error))

    candidate_registry: list[dict[str, Any]] = list(missing_candidates)
    internal_series: dict[str, tuple[list[str], list[float]]] = {}
    internal_label_intervals: dict[str, DailyLabelIntervals] = {}
    for family, path, document, calendar, calendar_source, calendar_error in loaded:
        for source in candidate_sources(family, document, path.name):
            candidate_id = f"{path.stem}::{source.logical_path}"
            item: dict[str, Any] = {
                "candidate_id": candidate_id,
                "family": family,
                "artifact": str(path),
                "candidate_path": source.logical_path,
                "reported_trade_count": source.reported_trade_count,
                "declared_trial_count": source.declared_trial_count,
                "declared_parameters": source.declared_parameters,
                "extraction_method": source.method,
                "calendar_source": calendar_source,
                "diagnostic_only": source.diagnostic_only,
                "selection_bias_eligible": False,
            }
            if source.unavailable_reason:
                item.update(
                    status="unavailable",
                    reason=source.unavailable_reason,
                    label_interval_evidence={
                        "available": False,
                        "reason": "candidate_return_path_unavailable",
                    },
                )
                candidate_registry.append(item)
                continue
            if not calendar:
                item.update(
                    status="unavailable",
                    reason=calendar_error or "verified_calendar_unavailable",
                    label_interval_evidence={
                        "available": False,
                        "reason": "verified_calendar_unavailable",
                    },
                )
                candidate_registry.append(item)
                continue
            if source.portfolio is not None:
                returns, error = portfolio_daily_returns(source.portfolio, calendar)
            elif source.trades is not None:
                returns, error = trade_proxy_daily_returns(source.trades, calendar)
            else:
                returns, error = None, "candidate_has_no_return_evidence"
            if returns is None:
                item.update(
                    status="unavailable",
                    reason=error,
                    label_interval_evidence={
                        "available": False,
                        "reason": "candidate_return_path_unavailable",
                    },
                )
                candidate_registry.append(item)
                continue

            if source.trades is None:
                label_intervals = None
                interval_evidence = {
                    "available": False,
                    "reason": "candidate_has_no_explicit_trade_rows",
                }
            else:
                label_intervals, interval_evidence = reported_label_intervals(
                    source.trades,
                    calendar,
                    portfolio=source.portfolio,
                    reported_trade_count=source.reported_trade_count,
                )
                if label_intervals is not None:
                    uncovered = [
                        day
                        for day, value, intervals in zip(calendar, returns, label_intervals)
                        if abs(value) > EPSILON and not intervals
                    ]
                    if uncovered:
                        label_intervals = None
                        interval_evidence = {
                            "available": False,
                            "reason": f"nonzero_return_without_trade_interval:{uncovered[0]}",
                        }

            stats = daily_return_stats(returns)
            eligible = (
                source.method == "reported_risk_weighted_portfolio"
                and not source.diagnostic_only
                and len(calendar) >= 20
            )
            item.update({
                "status": "comparable" if eligible else "available_noncomparable",
                "reason": (
                    None if eligible else
                    "post_hoc_diagnostic_not_a_causal_candidate" if source.diagnostic_only else
                    "equal_weight_proxy_not_comparable_to_reported_risk_weighted_portfolios"
                ),
                "calendar": {
                    "sessions": len(calendar), "start": calendar[0], "end": calendar[-1],
                    "signature": _calendar_signature(calendar),
                },
                "daily_return_stats": stats,
                "daily_returns_pct": [
                    {"date": day, "return_pct": _round(value, 10)}
                    for day, value in zip(calendar, returns)
                ],
                "series_signature": _series_signature(returns),
                "selection_bias_eligible": eligible,
                "label_interval_evidence": interval_evidence,
            })
            internal_series[candidate_id] = (calendar, returns)
            if label_intervals is not None:
                internal_label_intervals[candidate_id] = label_intervals
            candidate_registry.append(item)

    # Select the largest exactly comparable calendar/method cohort.
    cohorts: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in candidate_registry:
        if item.get("selection_bias_eligible"):
            cohorts[(str(item["extraction_method"]), str(item["calendar"]["signature"]))].append(item)
    chosen_key: tuple[str, str] | None = None
    if cohorts:
        chosen_key = max(cohorts, key=lambda key: (len(cohorts[key]), key))
    chosen = cohorts.get(chosen_key, []) if chosen_key else []

    by_signature: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in chosen:
        by_signature[str(item["series_signature"])].append(item)
    unique_candidates: list[dict[str, Any]] = []
    for signature in sorted(by_signature):
        group = sorted(by_signature[signature], key=lambda item: str(item["candidate_id"]))
        canonical = group[0]
        unique_candidates.append(canonical)
        canonical["selection_bias_included"] = True
        canonical["duplicate_of"] = None
        for duplicate in group[1:]:
            duplicate["selection_bias_included"] = False
            duplicate["duplicate_of"] = canonical["candidate_id"]
            duplicate["reason"] = "exact_duplicate_daily_return_series"
    chosen_ids = {str(item["candidate_id"]) for item in chosen}
    for item in candidate_registry:
        if item.get("selection_bias_eligible") and item.get("candidate_id") not in chosen_ids:
            item["selection_bias_included"] = False
            item["reason"] = "different_calendar_from_largest_comparable_cohort"

    unique_series = {
        str(item["candidate_id"]): internal_series[str(item["candidate_id"])][1]
        for item in unique_candidates
    }

    # For purged/embargoed validation, choose one interval-capable
    # representative per exact return signature.  This is deliberately
    # separate from the canonical raw-CSCV representative: an identical return
    # path does not authorize copying another candidate's holding intervals.
    interval_unique_candidates: list[dict[str, Any]] = []
    for signature in sorted(by_signature):
        interval_capable = sorted(
            (
                item for item in by_signature[signature]
                if str(item["candidate_id"]) in internal_label_intervals
            ),
            key=lambda item: str(item["candidate_id"]),
        )
        if interval_capable:
            interval_unique_candidates.append(interval_capable[0])
    purged_series = {
        str(item["candidate_id"]): internal_series[str(item["candidate_id"])][1]
        for item in interval_unique_candidates
    }
    purged_intervals = {
        str(item["candidate_id"]): internal_label_intervals[str(item["candidate_id"])]
        for item in interval_unique_candidates
    }
    sharpe_by_candidate = {
        name: _daily_sharpe(values) for name, values in unique_series.items()
    }
    valid_sharpes = [value for value in sharpe_by_candidate.values() if value is not None]
    best_name = max(
        (name for name, value in sharpe_by_candidate.items() if value is not None),
        key=lambda name: (float(sharpe_by_candidate[name]), name),
        default=None,
    )
    historical_trial_governance = declared_historical_trial_governance(
        discovered,
        comparable_path_count=len(chosen),
        comparable_unique_count=len(unique_candidates),
    )
    if best_name is None:
        selection_adjustment: dict[str, Any] = {
            "available": False,
            "reason": "no_nonconstant_candidate_in_comparable_cohort",
        }
    else:
        aligned_trials = max(1, len(unique_candidates))
        historical_trials = max(
            aligned_trials,
            int(historical_trial_governance["declared_historical_trial_lower_bound"]),
        )
        trial_stress = sorted(set((historical_trials, max(historical_trials, 250), max(historical_trials, 500))))
        selection_adjustment = {
            "available": True,
            "best_candidate": best_name,
            "selection_rule": "highest_daily_sharpe_in_largest_exact_calendar_cohort",
            "aligned_exact_unique_return_series_count": aligned_trials,
            "reported_candidate_count_before_exact_deduplication": len(chosen),
            "declared_historical_trial_lower_bound": historical_trials,
            "aligned_series_count_is_not_historical_trial_count": True,
            "trial_count_note": (
                "The DSR trial count uses explicit aggregate attempts even when their daily paths cannot "
                "be reconstructed. Cross-trial Sharpe dispersion is estimated only from aligned paths; "
                "250/500 stresses cover additional unpersisted attempts."
            ),
            "deflated_sharpe": deflated_sharpe_probability(
                unique_series[best_name], valid_sharpes, historical_trials,
            ),
            "aligned_candidate_only_reference_not_selection_adjusted_for_full_history": (
                deflated_sharpe_probability(unique_series[best_name], valid_sharpes, aligned_trials)
            ),
            "trial_count_stress": [
                deflated_sharpe_probability(unique_series[best_name], valid_sharpes, count)
                for count in trial_stress
            ],
        }

    pbo = cscv_pbo(unique_series)
    pbo["scope_note"] = (
        "CSCV/PBO uses only exact-aligned, reconstructable, non-diagnostic candidate series. "
        "Aggregate-only historical attempts affect DSR trial-count stress, not the PBO matrix."
    )
    purged_pbo = cscv_pbo(
        purged_series,
        label_intervals_by_candidate=purged_intervals,
        embargo_sessions=1,
    )
    same_cohort_unpurged = cscv_pbo(purged_series)
    same_cohort_purge_only = cscv_pbo(
        purged_series,
        label_intervals_by_candidate=purged_intervals,
        embargo_sessions=0,
    )
    reference_fields = (
        "available", "candidate_count", "observations", "combinations",
        "pbo_probability", "median_selected_oos_rank_pct", "median_logit_rank",
        "mean_selected_score_degradation", "overlap_purged_sessions_across_splits",
        "embargoed_sessions_across_splits",
    )
    purged_ids = set(purged_series)
    purged_pbo.update({
        "scope_note": (
            "Purged/embargoed CSCV uses only exact-aligned, non-diagnostic return paths whose own "
            "artifact candidate has complete explicit label-interval trade rows. It is a "
            "narrower evidence cohort and must not be compared one-for-one with the raw CSCV PBO."
        ),
        "eligible_exact_unique_return_series": len(unique_candidates),
        "interval_capable_exact_unique_return_series": len(purged_series),
        "excluded_candidate_ids": sorted(set(unique_series).difference(purged_ids)),
        "embargo_basis": "one_verified_session_after_each_contiguous_oos_segment",
        "same_interval_capable_cohort_references": {
            "unpurged": {
                key: same_cohort_unpurged[key]
                for key in reference_fields if key in same_cohort_unpurged
            },
            "purged_without_embargo": {
                key: same_cohort_purge_only[key]
                for key in reference_fields if key in same_cohort_purge_only
            },
        },
        "cohort_comparison_warning": (
            "Compare purge/embargo effects only with same_interval_capable_cohort_references; "
            "the 29-candidate raw CSCV uses a different candidate universe."
        ),
    })
    comparable_calendar = internal_series[best_name][0] if best_name else []
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": {
            "source_directory": str(data_dir),
            "read_only_sources": True,
            "network_calls": False,
            "runtime_imports": False,
            "families": [family for family, _ in FAMILY_PATTERNS],
        },
        "artifact_inventory": sorted(
            artifact_inventory, key=lambda item: (str(item["family"]), str(item.get("path")))
        ),
        "candidate_registry": sorted(candidate_registry, key=lambda item: str(item["candidate_id"])),
        "candidate_counts": {
            "registry_total": len(candidate_registry),
            "available_series": sum(item.get("daily_return_stats") is not None for item in candidate_registry),
            "unavailable": sum(item.get("status") == "unavailable" for item in candidate_registry),
            "reported_comparable_before_exact_deduplication": len(chosen),
            "comparable_exact_unique": len(unique_candidates),
        },
        "comparison_universe": {
            "method": chosen_key[0] if chosen_key else None,
            "calendar": ({
                "sessions": len(comparable_calendar),
                "start": comparable_calendar[0],
                "end": comparable_calendar[-1],
                "signature": _calendar_signature(comparable_calendar),
            } if comparable_calendar else None),
            "candidate_ids": sorted(unique_series),
            "zero_fill_policy": "verified_calendar_sessions_without_reported_trade_pnl_are_zero",
        },
        "historical_trial_governance": historical_trial_governance,
        "selection_adjustment": selection_adjustment,
        "cscv_pbo": pbo,
        "purged_embargo_cscv_pbo": purged_pbo,
        "limitations": [
            "Aggregate-only artifacts are registered but excluded; no daily path was fabricated.",
            "Reported portfolio equity and PnL are rounded in source artifacts, so reconstructed daily returns inherit rounding noise.",
            "Exact duplicate paths are de-duplicated, but correlated non-identical experiments still count as separate conservative trials.",
            "Raw PBO includes all aligned candidates and remains unpurged; it is retained for cohort continuity.",
            (
                "Purged/embargoed PBO excludes candidates without complete explicit label intervals "
                "and therefore uses a narrower cohort."
            ),
            (
                "A stored label-end timestamp is audited as reported evidence; this audit cannot "
                "independently prove exchange-level fill time accuracy."
            ),
            "The one-session embargo is a conservative policy parameter, not an empirically optimized horizon.",
            "The registry is a lower bound on all research trials because deleted or never-persisted experiments are unknowable.",
            "Aggregate historical trial counts inform DSR stress but cannot enter CSCV/PBO without aligned daily paths.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument(
        "--stdout-only", action="store_true",
        help="Do not write the derived audit report; source artifacts are always read-only.",
    )
    args = parser.parse_args()
    report = audit(args.data_dir)
    if not args.stdout_only:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    compact = {
        "candidate_counts": report["candidate_counts"],
        "comparison_universe": report["comparison_universe"],
        "historical_trial_governance": report["historical_trial_governance"],
        "selection_adjustment": report["selection_adjustment"],
        "cscv_pbo": report["cscv_pbo"],
        "purged_embargo_cscv_pbo": report["purged_embargo_cscv_pbo"],
        "limitations": report["limitations"],
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
