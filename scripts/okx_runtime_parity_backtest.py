#!/usr/bin/env python3
"""Diagnostic replay of the exact deployed 10m -> 5m -> 1m entry pipeline."""
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
from scripts.okx_multitimeframe_backtest import (  # noqa: E402
    DEFAULT_SYMBOLS, StrategyConfig, backtest_symbol, load_market_data, stats, weekday_sessions,
)

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
OUTPUT = ROOT / "data" / "okx_runtime_parity_90d.json"


def main() -> None:
    end = datetime.now(NY).date() - timedelta(days=1)
    days = weekday_sessions(end, 90)
    data = load_market_data(OKX(settings()), DEFAULT_SYMBOLS, days)
    cfg = StrategyConfig(
        "runtime_parity", min_10m_volume=1.0, min_5m_volume=1.0,
        max_extension_atr=999, confirmation_bars=1, min_1m_volume=0,
        require_confirmation_body=False, require_ema_slope=False,
        require_rsi_band=False, require_5m_extension=False,
        min_reward_cost_multiple=0, round_trip_cost_bps=14,
        max_holding_minutes=60, cooldown_minutes=30,
        stop_atr_multiplier=1.2, min_stop_bps=20,
        breakeven_r=1.0, scale_target_r=1.5, scale_fraction=0.4,
        trail_stop_r=1.0, enable_5m_reversal=True,
    )
    trades = []
    for index, (symbol, rows) in enumerate(data.items(), 1):
        trades.extend(backtest_symbol(symbol, rows, cfg))
        print(f"replayed {index}/{len(data)} {symbol}", flush=True)
    split_day = days[60].isoformat()
    for trade in trades:
        trade["date"] = datetime.fromtimestamp(trade["entry_time"] / 1000, UTC).astimezone(NY).date().isoformat()
    train = [trade for trade in trades if trade["date"] < split_day]
    holdout = [trade for trade in trades if trade["date"] >= split_day]
    report = {
        "generated_at": datetime.now(UTC).isoformat(), "config": cfg.__dict__,
        "split": {"diagnostic_train": [days[0].isoformat(), days[59].isoformat()],
                  "previously_inspected_final": [days[60].isoformat(), days[-1].isoformat()]},
        "warning": "parity diagnostic only; final 30 days were previously inspected and are not a fresh deployment holdout",
        "train": stats(train), "final_diagnostic": stats(holdout),
        "breakdown": {
            "side": {side: stats([row for row in holdout if row["side"] == side]) for side in ("LONG", "SHORT")},
            "session": {session: stats([row for row in holdout if row["session"] == session])
                        for session in sorted({row["session"] for row in holdout})},
        },
    }
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
