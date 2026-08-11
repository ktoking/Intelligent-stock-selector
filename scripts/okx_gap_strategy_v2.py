#!/usr/bin/env python3
"""Walk-forward research for a guarded relative opening-gap fade.

This is deliberately separate from the Demo executor.  The previous gap rule
would fade very large, market-wide opening moves and had no tail-loss control.
V2 adds only information available by 09:35:

* ignore market-wide opening gaps larger than ``max_spy_gap_bps``;
* keep the cross-sectional top quartile, with a hard relative-gap cap;
* retain the original first-5-minute/prior-day confirmation; and
* cap a one-sided continuation with a hard stop before the fixed 90-minute
  exit.

The replay uses public OKX candles, a 14bp round-trip cost, and at most five
symbols per opening timestamp.  It is a research artifact, not an execution
authorization.  No future candle is used for candidate selection.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_gap_feature_research import opening_features  # noqa: E402
from scripts.okx_gap_research import opportunities  # noqa: E402
from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import load_market_data, weekday_sessions  # noqa: E402
from scripts.okx_research_universe import load_symbols  # noqa: E402

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
OUTPUT = ROOT / "data" / "okx_gap_strategy_v2_backtest.json"
LAST_WEEK = {"2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30", "2026-07-31", "2026-08-03"}


@dataclass(frozen=True)
class V2Config:
    min_relative_gap_bps: float = 100.0
    max_relative_gap_bps: float = 600.0
    max_spy_gap_bps: float = 75.0
    min_cross_sectional_rank: float = 0.75
    horizon_minutes: int = 90
    stop_loss_pct: float = 3.0
    round_trip_cost_bps: float = 14.0
    max_positions: int = 5


def build_features(raw: dict[str, list[list[str]]]) -> pd.DataFrame:
    """Build the opening feature table; all fields are known at 09:35."""
    rows = opportunities(raw).merge(opening_features(raw), on=["symbol", "date"], how="left")
    rows = rows.dropna(subset=[
        "entry", "exit_90", "spy_gap", "spy_first5", "spy_previous_day",
        "open_volume_ratio", "prior_range_bps",
    ]).copy()
    rows["relative_gap"] = rows.gap_bps - rows.spy_gap
    rows["relative_first5"] = rows.first5_bps - rows.spy_first5
    rows["relative_previous"] = rows.previous_day_bps - rows.spy_previous_day
    rows["relative_gap_rank"] = rows.groupby("date").relative_gap.transform(
        lambda values: values.abs().rank(pct=True)
    )
    return rows


def select_candidates(rows: pd.DataFrame, config: V2Config) -> pd.DataFrame:
    """Apply causal filters and return rows eligible for the 09:35 entry."""
    selected = rows[
        rows.relative_gap.abs().between(
            config.min_relative_gap_bps, config.max_relative_gap_bps, inclusive="both"
        )
        & (rows.spy_gap.abs() <= config.max_spy_gap_bps)
        & (rows.relative_gap_rank >= config.min_cross_sectional_rank)
        & rows.exit_90.notna()
    ].copy()
    confirmed = (
        (selected.relative_gap * selected.relative_first5 < 0)
        | (selected.relative_gap * selected.relative_previous <= 0)
    )
    return selected[confirmed].copy()


def _adverse_pct(direction: int, entry: float, high: float, low: float) -> float:
    return (low / entry - 1) * 100 if direction > 0 else (1 - high / entry) * 100


def _trade_net_pct(row: pd.Series, config: V2Config) -> tuple[float, str]:
    """Replay a fixed stop with conservative same-bar stop precedence."""
    direction = -1 if float(row.relative_gap) > 0 else 1
    # The 09:35 entry to 11:00 exit contains 18 five-minute bars.  The path
    # itself is formed from completed bars, but it is used only after entry.
    path = row.path_150[:18]
    for high, low, _close in path:
        if _adverse_pct(direction, float(row.entry), float(high), float(low)) <= -config.stop_loss_pct:
            return -config.stop_loss_pct - config.round_trip_cost_bps / 100.0, "hard_stop"
    exit_price = float(row[f"exit_{config.horizon_minutes}"])
    gross_pct = direction * (exit_price / float(row.entry) - 1) * 100
    return gross_pct - config.round_trip_cost_bps / 100.0, "horizon"


def replay(rows: pd.DataFrame, config: V2Config) -> list[dict[str, Any]]:
    """Replay at most ``max_positions`` top-ranked candidates per entry time."""
    selected = select_candidates(rows, config)
    trades: list[dict[str, Any]] = []
    for _entry_time, group in selected.groupby("entry_time", sort=True):
        ranked = group.reindex(group.relative_gap.abs().sort_values(ascending=False).index)
        for _, row in ranked.head(config.max_positions).iterrows():
            net_pct, exit_reason = _trade_net_pct(row, config)
            direction = -1 if float(row.relative_gap) > 0 else 1
            trades.append({
                "date": str(row.date),
                "symbol": str(row.symbol),
                "entry_time": int(row.entry_time),
                "exit_time": int(row.entry_time + config.horizon_minutes * 60_000),
                "side": "LONG" if direction > 0 else "SHORT",
                "relative_gap_bps": round(float(row.relative_gap), 2),
                "net_pct": round(float(net_pct), 6),
                "exit_reason": exit_reason,
            })
    return trades


def metrics(trades: Iterable[dict[str, Any]]) -> dict[str, Any]:
    values = [float(item["net_pct"]) for item in trades]
    positive = sum(value for value in values if value > 0)
    negative = -sum(value for value in values if value <= 0)
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "trades": len(values),
        "wins": sum(value > 0 for value in values),
        "win_rate_pct": round(sum(value > 0 for value in values) / len(values) * 100, 2) if values else 0.0,
        "equal_weight_net_pct": round(sum(values), 3),
        "expectancy_pct_per_trade": round(sum(values) / len(values), 4) if values else 0.0,
        "profit_factor": round(positive / negative, 3) if negative else None,
        "max_drawdown_pct_points": round(drawdown, 3),
    }


def split_metrics(trades: list[dict[str, Any]], days: list[str]) -> dict[str, Any]:
    splits = {
        "train": set(days[:40]),
        "validation": set(days[40:50]),
        "development": set(days[50:60]),
        "final_diagnostic": set(days[60:90]),
        "last_week_to_yesterday": LAST_WEEK,
    }
    return {
        name: metrics([trade for trade in trades if trade["date"] in target])
        for name, target in splits.items()
    }


def failure_attribution(rows: pd.DataFrame, trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize causal features behind winners and losers for AI review."""
    if not trades:
        return {}
    keys = {(item["date"], item["symbol"]): item for item in trades}
    selected = select_candidates(rows, V2Config())
    selected["outcome"] = [
        keys.get((str(row.date), str(row.symbol)), {}).get("net_pct")
        for _, row in selected.iterrows()
    ]
    selected = selected[selected.outcome.notna()].copy()
    result: dict[str, Any] = {}
    for name in ("relative_gap", "spy_gap", "relative_first5", "relative_previous", "relative_gap_rank", "prior_range_bps"):
        groups = selected.assign(bucket=pd.qcut(selected[name], q=3, duplicates="drop")).groupby("bucket", observed=True)
        result[name] = [
            {"bucket": str(bucket), "trades": int(len(group)), "win_rate_pct": round(float((group.outcome > 0).mean() * 100), 2),
             "mean_net_pct": round(float(group.outcome.mean()), 4)}
            for bucket, group in groups
        ]
    return result


def run(end: date | None = None) -> dict[str, Any]:
    end = end or (datetime.now(NY).date() - timedelta(days=1))
    sessions = weekday_sessions(end, 90)
    days = [item.isoformat() for item in sessions]
    symbols = load_symbols("historical_90d")
    raw = load_market_data(OKX(settings()), symbols, sessions)
    rows = build_features(raw)
    baseline = V2Config(
        max_relative_gap_bps=10_000,
        max_spy_gap_bps=10_000,
        min_cross_sectional_rank=0.0,
        stop_loss_pct=100_000,
    )
    baseline_trades = replay(rows, baseline)
    config = V2Config()
    trades = replay(rows, config)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "sessions": {"count": len(days), "start": days[0], "end": days[-1]},
        "universe_size": len(symbols),
        "strategy": asdict(config),
        "baseline_strategy": {"rule": "old confirmed relative fade; 90m; 14bp; no stop/filter", **split_metrics(baseline_trades, days)},
        "v2": {"rule": "moderate relative fade + calm SPY + top-quartile rank + 3% stop", **split_metrics(trades, days)},
        "failure_attribution_v2": failure_attribution(rows, trades),
        "trades": trades,
        "warning": "OHLCV replay only; no historical L2/fill queue. Final diagnostic and last-week results are not promotion proof.",
    }


def main() -> None:
    report = run()
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({key: report[key] for key in ("sessions", "universe_size", "strategy", "baseline_strategy", "v2", "warning")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
