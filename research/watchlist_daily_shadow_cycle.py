"""One-shot, read-only daily-K review snapshot for the frozen watchlist baseline.

This is a research/shadow artifact, not an order generator. Historical replay
positions are labelled as such and never treated as the user's actual account.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import exchange_calendars as xc
import pandas as pd

from research.watchlist_daily_trend import OUT as DATA_DIR
from research.watchlist_daily_trend import features, load, rank_on, simulate


OUT = Path("outputs/watchlist_ai_review_forward")
BASELINE_START = pd.Timestamp("2026-03-23")


def next_session(day: pd.Timestamp) -> pd.Timestamp:
    calendar = xc.get_calendar("XNYS")
    future = calendar.sessions_in_range(day + pd.Timedelta(days=1), day + pd.Timedelta(days=10))
    return pd.Timestamp(future[0].date())


def reconstruct_positions(orders, data, as_of):
    positions = {}
    for order in orders:
        if order["side"] == "BUY":
            if order["symbol"] in positions:
                raise AssertionError("Baseline unexpectedly adds to an existing position")
            positions[order["symbol"]] = {
                "symbol": order["symbol"], "qty": int(order["qty"]),
                "entry_date": order["date"], "entry_price": float(order["price"]),
            }
        elif order["side"] == "SELL":
            prior = positions.pop(order["symbol"])
            if prior["qty"] != int(order["qty"]):
                raise AssertionError("Baseline sell quantity does not close position")
    for symbol, position in positions.items():
        entry = pd.Timestamp(position["entry_date"])
        prior = data[symbol].index[data[symbol].index.get_loc(entry) - 1]
        peak = float(data[symbol].loc[prior:as_of, "close"].max())
        position["peak_close"] = peak
        position["last_close"] = float(data[symbol].loc[as_of, "close"])
    return positions


def build_snapshot(as_of: str):
    data = load("2024-09-01")
    signal_day = pd.Timestamp(as_of)
    latest = data["US.QQQ"].index[-1]
    if signal_day != latest:
        raise ValueError(f"Refuse stale/future signal: requested={signal_day.date()}, latest QQQ={latest.date()}")
    if len(data) < 200:
        raise ValueError(f"Unexpectedly small data universe: {len(data)}")
    feats = features(data)
    types = json.loads((DATA_DIR / "security_types.json").read_text())
    dates = [day for day in data["US.QQQ"].index if BASELINE_START <= day <= signal_day]
    stats, curve, deals, orders = simulate(data, feats, dates, "dual_ema", 25, types, 0.01)
    holdings = reconstruct_positions(orders, data, signal_day)
    if len(holdings) != int(curve.iloc[-1].holdings):
        raise AssertionError("Replayed positions do not match strategy curve")
    planned_day = next_session(signal_day)
    qqq = feats["US.QQQ"]
    q = qqq.loc[signal_day]
    ranks = rank_on(feats, qqq, signal_day, "dual_ema", types)
    candidates = []
    for symbol in ranks:
        row = feats[symbol].loc[signal_day]
        candidates.append({
            "symbol": symbol, "close": float(row["close"]),
            "ema50": float(row.ema50), "ema150": float(row.ema150),
            "momentum_63d": float(row.mom63), "volatility_20d": float(row.vol20),
            "baseline_weight_cap": float(min(0.2, 0.01 / row.vol20)),
            "rank_score": float(row.mom63 / row.vol20),
        })
    exits = []
    for symbol, position in holdings.items():
        row = feats[symbol].loc[signal_day]
        reasons = []
        if row["close"] < row.ema50:
            reasons.append("close_below_ema50")
        if row["close"] < position["peak_close"] * 0.85:
            reasons.append("close_below_85pct_peak")
        if planned_day.weekday() == 0 and symbol not in ranks:
            reasons.append("not_in_monday_top5")
        if reasons:
            exits.append({"symbol": symbol, "reasons": reasons})
    prior_equity = float(curve.iloc[-2].equity)
    today_orders = [order for order in orders if order["date"] == as_of]
    one_day_attribution = []
    if not today_orders:
        for symbol, position in holdings.items():
            previous_close = float(data[symbol].loc[dates[-2], "close"])
            one_day_attribution.append({
                "symbol": symbol,
                "pnl_dollars": position["qty"] * (position["last_close"] - previous_close),
            })
        if abs(sum(row["pnl_dollars"] for row in one_day_attribution)
               - (float(curve.iloc[-1].equity) - prior_equity)) > 1e-6:
            raise AssertionError("One-day attribution does not reconcile")
    result = {
        "status": "shadow only; no real account/actual trades; AI text is separate from frozen strategy",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "signal_date": as_of, "next_session": str(planned_day.date()),
        "source": "cached Futunn REST autype=1 daily K; excludes dividends",
        "qqq_cache_modified_utc": datetime.fromtimestamp(
            (DATA_DIR / "QQQ.pkl").stat().st_mtime, timezone.utc
        ).isoformat(),
        "universe_count": len(data),
        "ordinary_stock_count": sum(types.get(symbol) == "STOCK" for symbol in data),
        "qqq": {
            "close": float(q["close"]), "prior_close": float(qqq.loc[dates[-2], "close"]),
            "ema100": float(q.ema100), "market_entry_gate": bool(q["close"] > q.ema100),
        },
        "paper_replay_not_actual_account": {
            "starting_capital": 10000.0, "cost_bps_each_side": 25,
            "equity_at_close": float(curve.iloc[-1].equity),
            "cash_at_close": float(curve.iloc[-1].cash),
            "one_day_return_pct": (float(curve.iloc[-1].equity) / prior_equity - 1) * 100,
            "orders_on_signal_date": today_orders,
            "one_day_attribution_no_orders_only": one_day_attribution,
            "positions": list(holdings.values()),
            "since_2026_03_23": stats,
        },
        "candidate_watchlist_not_orders": candidates,
        "next_open_frozen_strategy": {
            "is_monday_rebalance": planned_day.weekday() == 0,
            "planned_exits": exits,
            "planned_new_buy_symbols": [symbol for symbol in ranks if symbol not in holdings]
            if planned_day.weekday() == 0 else [],
            "caveat": "Only signals. Exact shares require next-open price and cash; no orders submitted.",
        },
    }
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--asof", required=True, help="Most recent completed US session YYYY-MM-DD")
    args = parser.parse_args()
    result = build_snapshot(args.asof)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"snapshot_{args.asof}.json"
    if path.exists():
        raise FileExistsError(f"Existing snapshot is immutable: {path}")
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
