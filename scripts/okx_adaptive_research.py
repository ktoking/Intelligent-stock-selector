#!/usr/bin/env python3
"""Test faster walk-forward adaptation without opening the sealed deployment gate."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import DEFAULT_SYMBOLS, load_market_data, weekday_sessions  # noqa: E402
from scripts.okx_portfolio_robustness import construct  # noqa: E402
from scripts.okx_return_model_research import BASE_FEATURES, dataset, metric, walkforward_predictions  # noqa: E402

NY = ZoneInfo("America/New_York")
OUTPUT = ROOT / "data" / "okx_adaptive_research_90d.json"


def main() -> None:
    end = datetime.now(NY).date() - timedelta(days=1)
    sessions = weekday_sessions(end, 90)
    days = [day.isoformat() for day in sessions]
    rows = dataset(load_market_data(OKX(settings()), DEFAULT_SYMBOLS, sessions))
    features = [*BASE_FEATURES, *sorted(c for c in rows if c.startswith("symbol_"))]
    results = []
    for model_name in ("ridge", "hist7"):
        for lookback in (10, 20, 30, 40):
            for split, target in (("validation", days[40:50]), ("development", days[50:60]),
                                  ("final", days[60:90])):
                frame, prediction = walkforward_predictions(
                    rows, days, target, model_name, 12, features,
                    lookback_days=lookback, retrain_days=1,
                )
                trades = construct(frame, prediction, threshold=25, per_side=2)
                results.append({"model": model_name, "lookback_days": lookback, "split": split,
                                **metric(trades)})
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "results": results,
              "warning": "diagnostic over previously inspected dates; never a deployment pass"}
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
