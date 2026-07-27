#!/usr/bin/env python3
"""Test a causal 5-minute relative-overreaction reversion family.

The signal is a stock token's closed 5-minute return relative to SPY, normalized
only by earlier intraday relative moves.  PnL remains the *single-token* market
order outcome, including a 14bp round-trip allowance; this avoids crediting an
unimplemented SPY hedge.
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

from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import CACHE_DIR, aggregate_bars, load_market_data, weekday_sessions  # noqa: E402
from scripts.okx_research_universe import load_symbols  # noqa: E402
from scripts.okx_return_model_research import metric  # noqa: E402

NY = ZoneInfo("America/New_York")
OUTPUT = ROOT / "data" / "okx_intraday_relative_reversion_90d.json"


def records(raw: dict[str, list[list[str]]]) -> pd.DataFrame:
    parts = []
    for symbol, rows in raw.items():
        bars = aggregate_bars(rows, 5)
        if not bars:
            continue
        frame = pd.DataFrame(bars, columns=["ts", "open", "high", "low", "close", "volume", "v1", "v2", "confirm"])
        frame = frame[frame.confirm.astype(str) == "1"].copy()
        frame["stamp"] = pd.to_datetime(frame.ts.astype("int64"), unit="ms", utc=True)
        frame["local"] = frame.stamp.dt.tz_convert(NY)
        frame = frame[(frame.local.dt.weekday < 5) & (frame.local.dt.time >= datetime.strptime("09:30", "%H:%M").time())
                      & (frame.local.dt.time <= datetime.strptime("15:40", "%H:%M").time())].copy()
        for column in ("open", "close", "volume"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame["symbol"] = symbol
        frame["date"] = frame.local.dt.date.astype(str)
        frame["ret_bps"] = frame.groupby("date", sort=False).close.pct_change() * 10_000
        frame["prior_vol"] = frame.groupby("date", sort=False).volume.transform(lambda values: values.shift(1).rolling(12, min_periods=8).mean())
        frame["volume_ratio"] = frame.volume / frame.prior_vol
        parts.append(frame[["symbol", "date", "ts", "open", "close", "ret_bps", "volume_ratio"]])
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def features(raw: dict[str, list[list[str]]]) -> pd.DataFrame:
    data = records(raw)
    spy = data[data.symbol == "SPY-USDT-SWAP"][["date", "ts", "ret_bps"]].rename(columns={"ret_bps": "spy_ret_bps"})
    data = data[~data.symbol.isin(("SPY-USDT-SWAP", "QQQ-USDT-SWAP", "SMH-USDT-SWAP"))].merge(spy, on=["date", "ts"], how="inner")
    data["relative_ret_bps"] = data.ret_bps - data.spy_ret_bps
    grouped = data.groupby(["symbol", "date"], sort=False)
    data["relative_scale"] = grouped.relative_ret_bps.transform(
        lambda values: values.shift(1).rolling(20, min_periods=15).std()
    )
    data["zscore"] = data.relative_ret_bps / data.relative_scale
    data = data.sort_values(["symbol", "date", "ts"]).reset_index(drop=True)
    # The signal's bar has closed.  A market order can only use the following
    # bar's open and exits after the declared number of full five-minute bars.
    data["entry"] = grouped.open.shift(-1)
    for horizon in (1, 2, 3, 6):
        data[f"exit_{horizon}"] = grouped.close.shift(-horizon - 1)
    return data.dropna(subset=["zscore", "entry"])


def trades(rows: pd.DataFrame, *, z_threshold: float, horizon: int, liquid: bool, style: str) -> list[dict]:
    selected = rows[rows.zscore.abs() >= z_threshold].copy()
    if liquid:
        selected = selected[selected.volume_ratio >= 1.0]
    selected = selected[selected[f"exit_{horizon}"].notna()]
    active, outcome = [], []
    for ts, group in selected.groupby("ts", sort=True):
        entry_time = int(ts) + 300_000
        active = [trade for trade in active if trade["exit_time"] > entry_time]
        slots = max(0, 5 - len(active))
        if not slots:
            continue
        # Max five concurrent positions; one signal per token per 5m bar and
        # no score from the exit path participates in ranking.
        for _, row in group.reindex(group.zscore.abs().sort_values(ascending=False).index).head(slots).iterrows():
            direction = (1 if row.zscore > 0 else -1) if style == "continuation" else (-1 if row.zscore > 0 else 1)
            gross_bps = direction * (float(row[f"exit_{horizon}"]) / float(row.entry) - 1) * 10_000
            trade = {"date": row.date, "symbol": row.symbol, "entry_time": entry_time,
                            "exit_time": int(ts) + (horizon + 1) * 300_000,
                            "side": "SHORT" if direction < 0 else "LONG", "net_r": (gross_bps - 14) / 100}
            active.append(trade)
            outcome.append(trade)
    return outcome


def main() -> None:
    end = datetime.now(NY).date() - timedelta(days=1)
    sessions = weekday_sessions(end, 90)
    days = [day.isoformat() for day in sessions]
    symbols = tuple(symbol for symbol in load_symbols("historical_90d")
                    if len(list(CACHE_DIR.glob(f"{symbol}_*_1m.json"))) >= len(sessions))
    rows = features(load_market_data(OKX(settings()), symbols, sessions))
    splits = {"train": set(days[:40]), "validation": set(days[40:50]), "development": set(days[50:60]),
              "final_diagnostic": set(days[60:90])}
    results = []
    for style in ("reversion", "continuation"):
        for z_threshold in (1.5, 2.0, 2.5, 3.0):
            for horizon in (1, 2, 3, 6):
                for liquid in (False, True):
                    value = trades(rows, z_threshold=z_threshold, horizon=horizon, liquid=liquid, style=style)
                    item = {"style": style, "z_threshold": z_threshold, "horizon_minutes": horizon * 5, "liquid_volume": liquid,
                            **{name: metric([trade for trade in value if trade["date"] in target])
                               for name, target in splits.items()}}
                    item["eligible"] = all(item[name]["trades"] >= 40 and item[name]["net_r"] > 0
                                            and (item[name]["profit_factor"] or 0) >= 1.12
                                            for name in ("train", "validation", "development"))
                    results.append(item)
    eligible = [item for item in results if item["eligible"]]
    results.sort(key=lambda item: min(item[name]["profit_factor"] or 0 for name in ("validation", "development")), reverse=True)
    OUTPUT.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(), "symbols": list(symbols),
        "method": "closed 5m relative-to-SPY z-score; next-bar single-token execution; 14bp round-trip; no hedge credit",
        "results": results, "eligible": eligible,
        "warning": "Final diagnostic is non-promotional because all 90 days are research data.",
    }, ensure_ascii=False, indent=2))
    print(json.dumps({"eligible": eligible, "leaderboard": results[:12]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
