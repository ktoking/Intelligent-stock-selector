#!/usr/bin/env python3
"""Feature ablation for the relative opening-gap fade, research only.

All filters are declared below before the run.  The first 40 sessions select a
single candidate; validation/development decide whether it may enter a new
forward Demo shadow period.  The last 30 sessions are diagnostic only.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_gap_research import TRADE_SYMBOLS, opportunities  # noqa: E402
from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import DEFAULT_SYMBOLS, aggregate_bars, load_market_data, weekday_sessions  # noqa: E402
from scripts.okx_return_model_research import metric  # noqa: E402

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
OUTPUT = ROOT / "data" / "okx_gap_feature_research_90d.json"


def opening_features(raw: dict[str, list[list[str]]]) -> pd.DataFrame:
    """Features known at 09:35; no later bars appear in any filter."""
    result = []
    for symbol, rows in raw.items():
        bars = aggregate_bars(rows, 5)
        frame = pd.DataFrame(bars, columns=["ts", "open", "high", "low", "close", "volume", "v1", "v2", "confirm"])
        frame["stamp"] = pd.to_datetime(frame.ts.astype("int64"), unit="ms", utc=True)
        frame["local"] = frame.stamp.dt.tz_convert(NY)
        frame["date"] = frame.local.dt.date.astype(str)
        frame["clock"] = frame.local.dt.strftime("%H:%M")
        for col in ("open", "high", "low", "close", "volume"):
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
        sessions = {date: part.set_index("clock") for date, part in frame.groupby("date")}
        dates = sorted(sessions)
        for index, date in enumerate(dates[1:], 1):
            previous, day = sessions[dates[index - 1]], sessions[date]
            if not {"09:30", "15:55"}.issubset(previous.index) or "09:30" not in day.index:
                continue
            history = []
            for old in dates[max(0, index - 21):index]:
                old_day = sessions[old]
                if "09:30" in old_day.index:
                    history.append(float(old_day.loc["09:30"].volume))
            prior_cash = previous.loc[(previous.index >= "09:30") & (previous.index <= "15:55")]
            previous_close = float(previous.loc["15:55"].close)
            result.append({
                "symbol": symbol, "date": date,
                "open_volume_ratio": float(day.loc["09:30"].volume) / max(sum(history) / len(history), 1e-9) if history else None,
                "prior_range_bps": (float(prior_cash.high.max()) / float(prior_cash.low.min()) - 1) * 10_000 if not prior_cash.empty else None,
                "prior_close": previous_close,
            })
    return pd.DataFrame(result)


def portfolio(rows: pd.DataFrame) -> list[dict]:
    trades = []
    for _, group in rows.groupby("entry_time", sort=True):
        # Relative-gap magnitude is the prespecified cross-sectional rank.
        for _, row in group.reindex(group.relative_gap.abs().sort_values(ascending=False).index).head(5).iterrows():
            direction = -1 if float(row.relative_gap) > 0 else 1
            ret = direction * (float(row.exit_150) / float(row.entry) - 1) * 10_000 - 14
            trades.append({"date": row.date, "symbol": row.symbol, "entry_time": int(row.entry_time),
                           "exit_time": int(row.entry_time + 150 * 60_000), "side": "LONG" if direction > 0 else "SHORT",
                           "net_r": ret / 100})
    return trades


def candidate_rows(rows: pd.DataFrame, variant: dict) -> pd.DataFrame:
    base = rows[(rows.relative_gap.abs() >= variant["gap_bps"]) & rows.exit_150.notna()].copy()
    # The original rule: either the first completed bar reverses the relative
    # gap or the previous day's relative return opposed it.
    confirmed = (base.relative_gap * base.relative_first5 < 0) | (base.relative_gap * base.relative_previous <= 0)
    if variant.get("first5_only"):
        confirmed = base.relative_gap * base.relative_first5 <= -variant.get("min_first5_reverse_bps", 0)
    base = base[confirmed]
    if variant.get("max_spy_gap") is not None:
        base = base[base.spy_gap.abs() <= variant["max_spy_gap"]]
    if variant.get("min_open_volume") is not None:
        base = base[base.open_volume_ratio >= variant["min_open_volume"]]
    if variant.get("max_prior_range") is not None:
        base = base[base.prior_range_bps <= variant["max_prior_range"]]
    if variant.get("min_rank") is not None:
        base = base[base.relative_gap_rank >= variant["min_rank"]]
    return base


def assess(rows: pd.DataFrame, days: list[str], variant: dict) -> dict:
    chosen = candidate_rows(rows, variant)
    trades = portfolio(chosen)
    lookback = variant.get("state_lookback_days")
    if lookback:
        # Causal regime switch: today's decision only knows completed paper
        # trades from earlier sessions.  It pauses rather than reverses a
        # failing strategy, so it cannot invent an opposite-direction alpha.
        daily: dict[str, list[dict]] = {day: [] for day in days}
        for trade in trades:
            daily.setdefault(trade["date"], []).append(trade)
        enabled: list[dict] = []
        for index, day in enumerate(days):
            history = [trade["net_r"] for old in days[max(0, index - lookback):index] for trade in daily.get(old, [])]
            if len(history) >= 10 and sum(history) / len(history) > 0:
                enabled.extend(daily.get(day, []))
        trades = enabled
    split_days = {"train": days[:40], "validation": days[40:50], "development": days[50:60], "final_diagnostic": days[60:90]}
    return {"name": variant["name"], "filters": {key: value for key, value in variant.items() if key != "name"},
            **{name: metric([trade for trade in trades if trade["date"] in dates]) for name, dates in split_days.items()}}


def passes(parts: dict) -> tuple[bool, list[str]]:
    problems = []
    for split in ("validation", "development"):
        value = parts[split]
        if value["trades"] < 25: problems.append(f"{split}:样本<25")
        if value["win_rate"] < 50: problems.append(f"{split}:胜率<50%")
        if (value["profit_factor"] or 0) < 1.2: problems.append(f"{split}:PF<1.2")
        if value["expectancy_r"] < .08: problems.append(f"{split}:期望<0.08R")
    return not problems, problems


def main() -> None:
    end = datetime.now(NY).date() - timedelta(days=1)
    sessions = weekday_sessions(end, 90); days = [day.isoformat() for day in sessions]
    raw = load_market_data(OKX(settings()), DEFAULT_SYMBOLS, sessions)
    rows = opportunities(raw).merge(opening_features(raw), on=["symbol", "date"], how="left").dropna()
    rows["relative_gap"] = rows.gap_bps - rows.spy_gap
    rows["relative_first5"] = rows.first5_bps - rows.spy_first5
    rows["relative_previous"] = rows.previous_day_bps - rows.spy_previous_day
    rows["relative_gap_rank"] = rows.groupby("date").relative_gap.transform(lambda x: x.abs().rank(pct=True))

    # Compact, interpretable grid.  It deliberately avoids thousands of
    # parameter combinations that would manufacture a historical winner.
    variants = [
        {"name": "base_confirmed", "gap_bps": 100},
        {"name": "strict_first5_10", "gap_bps": 100, "first5_only": True, "min_first5_reverse_bps": 10},
        {"name": "strict_first5_25", "gap_bps": 100, "first5_only": True, "min_first5_reverse_bps": 25},
        {"name": "calm_market", "gap_bps": 100, "max_spy_gap": 75},
        {"name": "liquid_open", "gap_bps": 100, "min_open_volume": 1.0},
        {"name": "normal_prior_range", "gap_bps": 100, "max_prior_range": 500},
        {"name": "top_relative_quartile", "gap_bps": 100, "min_rank": .75},
        {"name": "strict_reversal_calm", "gap_bps": 100, "first5_only": True, "min_first5_reverse_bps": 10, "max_spy_gap": 75},
        {"name": "strict_reversal_liquid", "gap_bps": 100, "first5_only": True, "min_first5_reverse_bps": 10, "min_open_volume": 1.0},
        {"name": "calm_liquid", "gap_bps": 100, "max_spy_gap": 75, "min_open_volume": 1.0},
        {"name": "higher_gap_calm", "gap_bps": 125, "max_spy_gap": 75},
        {"name": "higher_gap_strict", "gap_bps": 125, "first5_only": True, "min_first5_reverse_bps": 10},
        {"name": "base_state_5d", "gap_bps": 100, "state_lookback_days": 5},
        {"name": "base_state_10d", "gap_bps": 100, "state_lookback_days": 10},
        {"name": "top_rank_state_5d", "gap_bps": 100, "min_rank": .75, "state_lookback_days": 5},
        {"name": "top_rank_state_10d", "gap_bps": 100, "min_rank": .75, "state_lookback_days": 10},
    ]
    results = [assess(rows, days, variant) for variant in variants]
    for item in results:
        item["forward_demo_eligible"], item["rejection_reasons"] = passes(item)
    # The ranking only sees the first 40 sessions.  This still does not permit
    # execution: validation and development must independently pass above.
    ranked = sorted(results, key=lambda x: ((x["train"]["profit_factor"] or 0), x["train"]["expectancy_r"], x["train"]["trades"]), reverse=True)
    selected = ranked[0] if ranked else None
    report = {"generated_at": datetime.now(UTC).isoformat(), "sessions": {"count": len(days), "start": days[0], "end": days[-1]},
              "cost_model": "14 bp round trip; enter 09:35 next 5m open; exit 12:00", "selection": "ranked only on first 40 sessions",
              "promotion_gate": "validation and development each: n>=25, win rate>=50%, PF>=1.2, expectancy>=0.08R",
              "patterns": "open-volume ratio, prior range, relative-gap rank, market-gap magnitude, first-5-minute reversal",
              "selected_by_train": selected, "results": results,
              "warning": "Final 30 sessions are diagnostic because they have previously been inspected. A passing candidate still requires 10 new forward shadow sessions."}
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
