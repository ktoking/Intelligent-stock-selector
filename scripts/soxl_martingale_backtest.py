#!/usr/bin/env python3
"""Close-only, cash-funded SOXL martingale parameter study.

The feed is OKX's public SOXL-USDT-SWAP 5-minute history.  Executions are
limited to US regular trading hours to approximate a strategy sent to Futu for
US.SOXL.  This is research only: no order endpoints are called.
"""
from __future__ import annotations

import csv
import json
import math
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.okx_intraday_agent import OKX, settings  # noqa: E402

UTC, NY = timezone.utc, ZoneInfo("America/New_York")
SYMBOL = "SOXL-USDT-SWAP"
BAR = os.getenv("SOXL_BACKTEST_BAR", "5m")
BAR_MS = {"5m": 300_000, "15m": 900_000, "1H": 3_600_000}
HISTORY_DAYS = int(os.getenv("SOXL_HISTORY_DAYS", "93"))
GRID = os.getenv("SOXL_BACKTEST_GRID", "standard")
CAPITAL = 10_000.0
FIRST_ORDER = 1_000.0
FEE_RATE = 0.001  # 10bp per side; deliberately higher than a zero-fee assumption.


@dataclass(frozen=True)
class Params:
    drop_pct: float
    take_profit_pct: float
    amount_multiplier: float
    max_add_orders: int = 9


def history(start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    """Page public five-minute candles backward and retain closed bars only."""
    client = OKX(settings())
    if BAR not in BAR_MS:
        raise ValueError("SOXL_BACKTEST_BAR must be 5m, 15m, or 1H")
    cursor = int(end.timestamp() * 1000) + BAR_MS[BAR]
    out: dict[int, tuple[datetime, float]] = {}
    start_ms = int(start.timestamp() * 1000)
    for _ in range(110):
        rows = client.request("GET", "/api/v5/market/history-candles", {
            "instId": SYMBOL, "bar": BAR, "after": str(cursor), "limit": "300",
        })["data"]
        if not rows:
            break
        stamps = [int(row[0]) for row in rows]
        for row in rows:
            if len(row) >= 9 and row[8] == "1" and int(row[0]) >= start_ms:
                out[int(row[0])] = (datetime.fromtimestamp(int(row[0]) / 1000, UTC), float(row[4]))
        oldest = min(stamps)
        if oldest <= start_ms or oldest >= cursor:
            break
        cursor = oldest
        time.sleep(0.13)
    return [out[key] for key in sorted(out)]


def rth(stamp: datetime) -> bool:
    local = stamp.astimezone(NY)
    minute = local.hour * 60 + local.minute
    return local.weekday() < 5 and 9 * 60 + 30 <= minute <= 15 * 60 + 55


def run(bars: list[tuple[datetime, float]], p: Params) -> dict[str, float | int | bool]:
    cash, units, cost = CAPITAL, 0.0, 0.0
    last_buy = 0.0
    adds, cycles, wins, fees = 0, 0, 0, 0.0
    equity_peak, max_drawdown, max_deployed = CAPITAL, 0.0, 0.0
    cycle_pnls: list[float] = []
    for stamp, price in bars:
        equity = cash + units * price
        equity_peak = max(equity_peak, equity)
        max_drawdown = max(max_drawdown, 1 - equity / equity_peak)
        max_deployed = max(max_deployed, CAPITAL - cash)
        if not rth(stamp):
            continue
        average = cost / units if units else 0.0
        # Close-only rule.  A buy has priority over a sell when both would have
        # happened somewhere inside an omitted 5m bar, which is conservative.
        should_buy = units == 0 or (adds < p.max_add_orders and price <= last_buy * (1 - p.drop_pct))
        if should_buy and cash > 0:
            is_add = units > 0
            intended = FIRST_ORDER * (p.amount_multiplier ** adds) if is_add else FIRST_ORDER
            notional = min(intended, cash / (1 + FEE_RATE))
            if notional > 1:
                charge = notional * FEE_RATE
                acquired = notional / price
                cash -= notional + charge
                units += acquired
                cost += notional + charge
                fees += charge
                last_buy = price
                if is_add:
                    adds += 1
            continue
        if units and price >= average * (1 + p.take_profit_pct):
            proceeds = units * price
            charge = proceeds * FEE_RATE
            pnl = proceeds - charge - cost
            cash += proceeds - charge
            fees += charge
            cycle_pnls.append(pnl)
            cycles += 1
            wins += pnl > 0
            units, cost, last_buy, adds = 0.0, 0.0, 0.0, 0
    final_price = bars[-1][1]
    equity = cash + units * final_price
    days = (bars[-1][0] - bars[0][0]).total_seconds() / 86400
    total_return = equity / CAPITAL - 1
    annualized = (equity / CAPITAL) ** (365 / days) - 1 if equity > 0 and days > 0 else -1.0
    return {
        "drop_pct": p.drop_pct * 100, "take_profit_pct": p.take_profit_pct * 100,
        "amount_multiplier": p.amount_multiplier, "max_add_orders": p.max_add_orders,
        "return_pct": total_return * 100, "annualized_pct": annualized * 100,
        "max_drawdown_pct": max_drawdown * 100, "cycles": cycles, "win_cycles": wins,
        "open_position": units > 0, "open_unrealized_pct": ((final_price / (cost / units)) - 1) * 100 if units else 0.0,
        "max_deployed_usd": max_deployed, "fees_usd": fees, "ending_equity_usd": equity,
        "calculated_days": days,
    }


def main() -> None:
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    bars = history(now - timedelta(days=HISTORY_DAYS), now)
    if len(bars) < 1_000:
        raise RuntimeError(f"Insufficient history ({len(bars)} five-minute bars)")
    windows = {"30d": now - timedelta(days=30)}
    if bars[0][0] <= now - timedelta(days=90):
        windows["90d"] = now - timedelta(days=90)
    if GRID == "wide":
        grid = [Params(drop / 100, tp / 100, mult, orders)
                for drop in range(2, 11) for tp in range(2, 13)
                for mult in (1.0, 1.1, 1.2, 1.3, 1.5) for orders in (4, 6, 9)]
    else:
        grid = [Params(drop, tp, mult) for drop in (0.03, 0.04, 0.05, 0.06, 0.08)
                for tp in (0.03, 0.05, 0.07, 0.10) for mult in (1.0, 1.2, 1.5)]
    rows = []
    for label, begin in windows.items():
        window = [bar for bar in bars if bar[0] >= begin]
        for params in grid:
            result = run(window, params)
            result["window"] = label
            # A risk-adjusted ranking: reward net return but make drawdown and
            # stranded final positions explicit.  It is not an optimisation target.
            result["score"] = result["return_pct"] - 1.5 * result["max_drawdown_pct"] - (3 if result["open_position"] else 0)
            rows.append(result)
    rows.sort(key=lambda x: (x["window"], -float(x["score"])))
    suffix = f"{BAR}_{GRID}"
    target = ROOT / "data" / f"soxl_martingale_backtest_{suffix}.csv"
    with target.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)
    summary = {label: [row for row in rows if row["window"] == label][:10] for label in windows}
    (ROOT / "data" / f"soxl_martingale_backtest_summary_{suffix}.json").write_text(json.dumps({
        "source": SYMBOL, "bar": f"{BAR} closed candle", "execution": "US regular hours, close-only",
        "capital_usd": CAPITAL, "first_order_usd": FIRST_ORDER, "fee_per_side": FEE_RATE,
        "bar_count": len(bars), "from": bars[0][0].isoformat(), "to": bars[-1][0].isoformat(),
        "top_10_by_window": summary,
    }, indent=2))
    for label in windows:
        print("\n", label)
        for row in summary[label][:5]:
            print("drop={drop_pct:.0f}% tp={take_profit_pct:.0f}% mult={amount_multiplier:.1f} "
                  "ret={return_pct:.2f}% ann={annualized_pct:.1f}% mdd={max_drawdown_pct:.2f}% "
                  "cycles={cycles} open={open_position}".format(**row))


if __name__ == "__main__":
    main()
