"""Causal daily risk-review ablation on the existing US watchlist strategy.

This tests a deterministic proxy for an AI risk review, not historical LLM
decisions. It never downloads data, calls an order endpoint, or changes rules.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from research.watchlist_daily_trend import (
    OUT as DATA_DIR,
    buy_hold,
    features,
    load,
    rank_on,
    simulate,
)


OUT = Path("outputs/watchlist_daily_review_20260923")
START = pd.Timestamp("2026-03-23")
END = pd.Timestamp("2026-09-22")


def simulate_review(data, feats, days, stock_types, bps=10, flatten_below_ema100=False):
    """Mirror the frozen baseline; optionally flatten after a completed QQQ bar.

    The sole review action is: if yesterday's QQQ close was below its EMA100,
    sell all remaining holdings at today's open. Re-entry uses the unchanged
    baseline's Monday selection rule. No future bars or retrospective news.
    """
    qqq = feats["US.QQQ"]
    dates = list(qqq.index)
    cash = 10000.0
    held = {}
    curve, deals, orders, review_days = [], [], [], []
    first, last = days[0], days[-1]
    for i, day in enumerate(dates):
        if day < first or day > last:
            continue
        previous = dates[i - 1]
        market_off = bool(
            flatten_below_ema100
            and pd.notna(qqq.loc[previous, "ema100"])
            and qqq.loc[previous, "close"] < qqq.loc[previous, "ema100"]
        )
        if market_off:
            review_days.append(str(day.date()))
        exiting = []
        for symbol, position in held.items():
            feat = feats[symbol]
            if previous not in feat.index or day not in data[symbol].index:
                raise ValueError(f"held symbol missing adjacent session: {symbol} {day}")
            p = feat.loc[previous]
            if p["close"] < p.ema50 or p["close"] < position["peak"] * 0.85:
                exiting.append(symbol)
        weekly = day.weekday() == 0
        targets = rank_on(feats, qqq, previous, "dual_ema", stock_types) if weekly else []
        if weekly:
            exiting.extend(symbol for symbol in held if symbol not in targets)
        if market_off:
            exiting.extend(held)
        for symbol in dict.fromkeys(exiting):
            position = held.pop(symbol)
            price = float(data[symbol].loc[day, "open"]) * (1 - bps / 10000)
            cash += position["qty"] * price
            deals.append({
                "symbol": symbol,
                "entry_date": str(position["entry_day"].date()),
                "exit_date": str(day.date()),
                "qty": position["qty"],
                "entry": position["entry"],
                "exit": price,
                "pnl": position["qty"] * (price - position["entry"]),
            })
            orders.append({"date": str(day.date()), "symbol": symbol, "side": "SELL",
                           "qty": position["qty"], "price": price})
        if weekly and not market_off:
            equity_open = cash + sum(
                position["qty"] * float(data[symbol].loc[day, "open"])
                for symbol, position in held.items()
            )
            for symbol in targets:
                if symbol in held or day not in data[symbol].index:
                    continue
                price = float(data[symbol].loc[day, "open"]) * (1 + bps / 10000)
                weight = min(0.2, 0.01 / float(feats[symbol].loc[previous, "vol20"]))
                qty = int(min(cash, equity_open * weight) / price)
                if qty < 1:
                    continue
                cash -= qty * price
                held[symbol] = {"qty": qty, "entry": price, "entry_day": day,
                                "peak": float(feats[symbol].loc[previous, "close"])}
                orders.append({"date": str(day.date()), "symbol": symbol, "side": "BUY",
                               "qty": qty, "price": price})
        equity = cash
        for symbol, position in held.items():
            price = float(data[symbol].loc[day, "close"])
            equity += position["qty"] * price
            position["peak"] = max(position["peak"], price)
        curve.append((day, equity, cash, len(held)))
    frame = pd.DataFrame(curve, columns=["date", "equity", "cash", "holdings"])
    equity = np.r_[10000.0, frame.equity.to_numpy()]
    stats = {
        "return_pct": float((equity[-1] / equity[0] - 1) * 100),
        "max_drawdown_pct": float((1 - equity / np.maximum.accumulate(equity)).max() * 100),
        "completed_trades": len(deals),
        "orders": len(orders),
        "holding_count_end": len(held),
        "win_rate": sum(deal["pnl"] > 0 for deal in deals) / len(deals) if deals else None,
        "trading_days": len(frame),
        "market_off_days": len(review_days),
    }
    return stats, frame, deals, orders, review_days


def run():
    OUT.mkdir(parents=True, exist_ok=True)
    data = load("2024-09-01")
    feats = features(data)
    stock_types = json.loads((DATA_DIR / "security_types.json").read_text())
    days = [day for day in data["US.QQQ"].index if START <= day <= END]
    if len(days) < 110 or days[-1] != END:
        raise RuntimeError(f"Incomplete six-month calendar: {len(days)} days; last={days[-1]}")
    report = {
        "status": "retrospective ablation; not a historical AI replay or independent holdout",
        "period": [str(days[0].date()), str(days[-1].date())],
        "source": "cached Futunn REST autype=1 daily OHLCV; dividends excluded",
        "universe_source": "current US watchlist viewed retrospectively; survivorship bias",
        "ordinary_stock_count": sum(stock_types.get(symbol) == "STOCK" for symbol in data),
        "decision_timing": "prior completed daily close; next adjusted open fill",
        "review_rule": "QQQ prior close below EMA100: flatten existing stocks at next open; keep baseline Monday re-entry",
        "benchmark": {symbol: buy_hold(data, symbol, days) for symbol in ("US.QQQ", "US.SPY")},
        "costs": {},
    }
    for bps in (10, 25):
        baseline_reference = simulate(data, feats, days, "dual_ema", bps, stock_types, 0.01)
        baseline = simulate_review(data, feats, days, stock_types, bps, False)
        if baseline_reference[3] != baseline[3] or not np.allclose(
            baseline_reference[1].equity, baseline[1].equity, atol=1e-8, rtol=0
        ):
            raise AssertionError("Local baseline diverges from frozen research engine")
        reviewed = simulate_review(data, feats, days, stock_types, bps, True)
        report["costs"][str(bps)] = {
            "baseline": baseline[0],
            "daily_review_proxy": reviewed[0],
            "return_delta_points": reviewed[0]["return_pct"] - baseline[0]["return_pct"],
            "drawdown_delta_points": reviewed[0]["max_drawdown_pct"] - baseline[0]["max_drawdown_pct"],
        }
        for label, result in (("baseline", baseline), ("daily_review_proxy", reviewed)):
            result[1].to_csv(OUT / f"{label}_{bps}bps_curve.csv", index=False)
            pd.DataFrame(result[2]).to_csv(OUT / f"{label}_{bps}bps_trades.csv", index=False)
            pd.DataFrame(result[3]).to_csv(OUT / f"{label}_{bps}bps_orders.csv", index=False)
    (OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    run()
