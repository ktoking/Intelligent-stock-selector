#!/usr/bin/env python3
"""Separate failed-opening-gap reversals from opening-drive continuation."""
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

from scripts.okx_gap_feature_research import opening_features  # noqa: E402
from scripts.okx_gap_research import opportunities  # noqa: E402
from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import CACHE_DIR, load_market_data, weekday_sessions  # noqa: E402
from scripts.okx_research_universe import load_symbols  # noqa: E402
from scripts.okx_return_model_research import metric  # noqa: E402

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
OUTPUT = ROOT / "data" / "okx_opening_drive_90d.json"


def simulate(rows: pd.DataFrame, horizon: int, style: str) -> list[dict]:
    output = []
    for _, group in rows.groupby("entry_time", sort=True):
        for _, row in group.reindex(group.relative_gap.abs().sort_values(ascending=False).index).head(5).iterrows():
            # A failed opening drive fades the initial relative gap; a sustained
            # drive follows it.  Directions are mutually exclusive by filter.
            side = (-1 if row.relative_gap > 0 else 1) if style == "fade" else (1 if row.relative_gap > 0 else -1)
            bps = side * (float(row[f"exit_{horizon}"]) / float(row.entry) - 1) * 10_000 - 14
            output.append({"date": row.date, "entry_time": int(row.entry_time),
                           "exit_time": int(row.entry_time + horizon * 60_000), "net_r": bps / 100})
    return output


def evaluate(rows: pd.DataFrame, days: list[str], name: str, horizon: int, style: str, mask: pd.Series) -> dict:
    value = simulate(rows[mask & rows[f"exit_{horizon}"].notna()], horizon, style)
    periods = {"train": days[:40], "validation": days[40:50], "development": days[50:60], "final_diagnostic": days[60:90]}
    return {"name": name, "horizon_minutes": horizon, "style": style,
            **{key: metric([trade for trade in value if trade["date"] in target]) for key, target in periods.items()}}


def main() -> None:
    end = datetime.now(NY).date() - timedelta(days=1)
    sessions = weekday_sessions(end, 90); days = [date.isoformat() for date in sessions]
    symbols = tuple(symbol for symbol in load_symbols("historical_90d") if len(list(CACHE_DIR.glob(f"{symbol}_*_1m.json"))) >= len(days))
    raw = load_market_data(OKX(settings()), symbols, sessions)
    rows = opportunities(raw).merge(opening_features(raw), on=["symbol", "date"], how="left").dropna().copy()
    rows["relative_gap"] = rows.gap_bps - rows.spy_gap
    rows["relative_first5"] = rows.first5_bps - rows.spy_first5
    rows["relative_rank"] = rows.groupby("date").relative_gap.transform(lambda value: value.abs().rank(pct=True))
    gap = rows.relative_gap.abs() >= 100
    failed = gap & (rows.relative_gap * rows.relative_first5 <= -10)
    drive = gap & (rows.relative_gap * rows.relative_first5 >= 10)
    variants = []
    for horizon in (30, 60, 90):
        variants.extend([
            ("failed_gap_fade", horizon, "fade", failed),
            ("failed_gap_fade_liquid", horizon, "fade", failed & (rows.open_volume_ratio >= 1.0)),
            ("opening_drive_continue", horizon, "continue", drive),
            ("opening_drive_continue_liquid", horizon, "continue", drive & (rows.open_volume_ratio >= 1.0)),
            ("opening_drive_top_rank", horizon, "continue", drive & (rows.relative_rank >= .75)),
        ])
    results = [evaluate(rows, days, *variant) for variant in variants]
    eligible = [item for item in results if all(item[split]["trades"] >= 25 and item[split]["net_r"] > 0 and
                                                (item[split]["profit_factor"] or 0) >= 1.2 and item[split]["expectancy_r"] >= .08
                                                for split in ("validation", "development"))]
    OUTPUT.write_text(json.dumps({"generated_at": datetime.now(UTC).isoformat(), "symbols": list(symbols),
        "rules": "failed opening drive=faded; sustained opening drive=continued; 14bp round-trip cost",
        "eligible": eligible, "results": results,
        "warning": "Candidate selection must not use final diagnostic metrics."}, ensure_ascii=False, indent=2))
    print(json.dumps({"eligible": eligible, "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
