"""Retrospective cash-only position/frequency ablation on cached Futunn daily bars.

No downloads, broker calls, order placement, or historical AI decisions.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from research.watchlist_daily_trend import OUT as DATA_DIR, features, load, rank_on, simulate


OUT = Path("outputs/watchlist_position_frequency_20260923")
WINDOWS = {
    "prior_2025": (pd.Timestamp("2025-03-24"), pd.Timestamp("2025-09-23")),
    "recent_2026": (pd.Timestamp("2026-03-23"), pd.Timestamp("2026-09-22")),
}
# Predeclared 2 x 3 matrix. 'Heavy' means larger per-stock exposure, never
# margin borrowing; holdings remain limited by available cash.
VARIANTS = {
    "base_weekly": (0.20, 0.010, (0,)),
    "heavy_weekly": (0.30, 0.015, (0,)),
    "base_twice_weekly": (0.20, 0.010, (0, 3)),
    "heavy_twice_weekly": (0.30, 0.015, (0, 3)),
    "base_daily": (0.20, 0.010, (0, 1, 2, 3, 4)),
    "heavy_daily": (0.30, 0.015, (0, 1, 2, 3, 4)),
}


def simulate_variant(data, feats, days, stock_types, bps, weight_cap, vol_target,
                     rebalance_weekdays, ranking_cache=None):
    """Use prior close for signals and next adjusted open for cash-only fills."""
    qqq = feats["US.QQQ"]
    dates = list(qqq.index)
    dates_index = {day: i for i, day in enumerate(dates)}
    cash, held = 10000.0, {}
    curve, deals, orders = [], [], []
    for day in days:
        previous = dates[dates_index[day] - 1]
        exiting = []
        for symbol, position in held.items():
            feat = feats[symbol]
            if previous not in feat.index or day not in data[symbol].index:
                raise ValueError(f"held symbol missing adjacent session: {symbol} {day}")
            row = feat.loc[previous]
            if row["close"] < row.ema50 or row["close"] < position["peak"] * 0.85:
                exiting.append(symbol)
        rebalance = day.weekday() in rebalance_weekdays
        targets = (ranking_cache[previous] if ranking_cache is not None else
                   rank_on(feats, qqq, previous, "dual_ema", stock_types)) if rebalance else []
        if rebalance:
            exiting.extend(symbol for symbol in held if symbol not in targets)
        for symbol in dict.fromkeys(exiting):
            position = held.pop(symbol)
            price = float(data[symbol].loc[day, "open"]) * (1 - bps / 10000)
            cash += position["qty"] * price
            deals.append({
                "symbol": symbol, "entry_date": str(position["entry_day"].date()),
                "exit_date": str(day.date()), "qty": position["qty"],
                "entry": position["entry"], "exit": price,
                "pnl": position["qty"] * (price - position["entry"]),
            })
            orders.append({"date": str(day.date()), "symbol": symbol,
                           "side": "SELL", "qty": position["qty"], "price": price})
        if rebalance:
            equity_open = cash + sum(position["qty"] * float(data[symbol].loc[day, "open"])
                                     for symbol, position in held.items())
            for symbol in targets:
                if symbol in held or day not in data[symbol].index:
                    continue
                price = float(data[symbol].loc[day, "open"]) * (1 + bps / 10000)
                weight = min(weight_cap, vol_target / float(feats[symbol].loc[previous, "vol20"]))
                quantity = int(min(cash, equity_open * weight) / price)
                if quantity < 1:
                    continue
                cash -= quantity * price
                if cash < -1e-8:
                    raise AssertionError("cash-only portfolio borrowed unexpectedly")
                held[symbol] = {"qty": quantity, "entry": price, "entry_day": day,
                                "peak": float(feats[symbol].loc[previous, "close"])}
                orders.append({"date": str(day.date()), "symbol": symbol,
                               "side": "BUY", "qty": quantity, "price": price})
        equity = cash
        for symbol, position in held.items():
            price = float(data[symbol].loc[day, "close"])
            equity += position["qty"] * price
            position["peak"] = max(position["peak"], price)
        curve.append((day, equity, cash, len(held)))
    frame = pd.DataFrame(curve, columns=["date", "equity", "cash", "holdings"])
    values = np.r_[10000.0, frame.equity.to_numpy()]
    stats = {
        "return_pct": float((values[-1] / 10000.0 - 1) * 100),
        "max_drawdown_pct": float((1 - values / np.maximum.accumulate(values)).max() * 100),
        "completed_trades": len(deals), "orders": len(orders),
        "holding_count_end": len(held),
        "win_rate": sum(deal["pnl"] > 0 for deal in deals) / len(deals) if deals else None,
        "trading_days": len(frame),
        "avg_invested_pct": float(((frame.equity - frame.cash) / frame.equity).mean() * 100),
        "order_notional_to_start_capital": float(sum(order["qty"] * order["price"]
                                                        for order in orders) / 10000.0),
    }
    return stats, frame, deals, orders


def run():
    OUT.mkdir(parents=True, exist_ok=True)
    data = load("2024-09-01")
    feats = features(data)
    stock_types = json.loads((DATA_DIR / "security_types.json").read_text())
    calendar = list(data["US.QQQ"].index)
    days_by_window = {
        label: [day for day in calendar if start <= day <= end]
        for label, (start, end) in WINDOWS.items()
    }
    for label, days in days_by_window.items():
        if len(days) < 110 or days[0] != WINDOWS[label][0] or days[-1] != WINDOWS[label][1]:
            raise RuntimeError(f"Incomplete calendar for {label}: {len(days)} {days[:1]} {days[-1:]}")
    # Rank once per completed close; all variants use identical prior-day ranks.
    needed = {calendar[calendar.index(day) - 1] for days in days_by_window.values() for day in days}
    ranking_cache = {day: rank_on(feats, feats["US.QQQ"], day, "dual_ema", stock_types)
                     for day in sorted(needed)}
    report = {
        "status": "retrospective parameter ablation; not AI replay, independent holdout, or live trading",
        "source": "cached Futunn REST autype=1 adjusted daily OHLCV; no dividends",
        "universe": "current US watchlist replayed historically; survivorship/selection bias",
        "data_symbol_count": len(data),
        "ordinary_stock_count": sum(stock_types.get(symbol) == "STOCK" for symbol in data),
        "decision_and_fill": "previous completed daily close -> next adjusted open",
        "capital": "USD 10000; cash-only, no margin or leverage",
        "variants": {label: {"weight_cap": x[0], "vol_target": x[1],
                             "rebalance_weekdays": list(x[2])} for label, x in VARIANTS.items()},
        "windows": {},
    }
    for window, days in days_by_window.items():
        report["windows"][window] = {
            "dates": [str(days[0].date()), str(days[-1].date())],
            "trading_days": len(days), "costs": {},
        }
        for bps in (10, 25):
            cost_results = {}
            for label, (weight_cap, vol_target, weekdays) in VARIANTS.items():
                result = simulate_variant(data, feats, days, stock_types, bps,
                                          weight_cap, vol_target, weekdays, ranking_cache)
                cost_results[label] = result[0]
                if bps == 25:
                    result[1].to_csv(OUT / f"{window}_{label}_25bps_curve.csv", index=False)
                    pd.DataFrame(result[3]).to_csv(OUT / f"{window}_{label}_25bps_orders.csv", index=False)
                if label == "base_weekly":
                    reference = simulate(data, feats, days, "dual_ema", bps, stock_types, .01)
                    if result[3] != reference[3] or not np.allclose(
                            result[1].equity, reference[1].equity, atol=1e-8, rtol=0):
                        raise AssertionError(f"Baseline parity failed: {window} {bps}bps")
            report["windows"][window]["costs"][str(bps)] = cost_results
    (OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    run()
