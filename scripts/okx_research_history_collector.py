#!/usr/bin/env python3
"""Resumable historical 1m cache downloader for the research-only universe."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import load_market_data, weekday_sessions  # noqa: E402
from scripts.okx_research_universe import load_symbols  # noqa: E402

NY = ZoneInfo("America/New_York")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=120)
    parser.add_argument("--cohort", default="historical_90d",
                        help="historical_<N>d or forward_observation; the historical cohort is frozen by list time")
    args = parser.parse_args()
    days = weekday_sessions(datetime.now(NY).date() - timedelta(days=1), max(1, args.sessions))
    symbols = load_symbols(args.cohort)
    if not symbols:
        raise SystemExit(f"no symbols are eligible for cohort {args.cohort}")
    # load_market_data is already restart-safe at one daily file per symbol.
    # Newer listings simply contribute the sessions the exchange actually has.
    data = load_market_data(OKX(settings()), symbols, days)
    output = ROOT / "data" / "okx_research_history_status.json"
    output.write_text(json.dumps({"cohort": args.cohort, "sessions_requested": len(days),
                                  "start": days[0].isoformat(), "end": days[-1].isoformat(),
                                  "symbols": {symbol: len(rows) for symbol, rows in data.items()}}, ensure_ascii=False, indent=2))
    print(output)


if __name__ == "__main__":
    main()
