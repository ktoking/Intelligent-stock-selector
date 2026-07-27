#!/usr/bin/env python3
"""Long-history hourly regime research for the OKX equity-token universe."""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc
SYMBOLS = ("NVDA", "AMD", "MU", "INTC", "TSM", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA", "MSTR", "HOOD")
CACHE = ROOT / "data" / "yfinance_hourly_2y.pkl"


@dataclass(frozen=True)
class Config:
    observation_bars: int
    mode: str
    rank_count: int
    minimum_move_bps: int
    market_filter: str
    stop_bps: int
    target_r: int


def download() -> dict[str, pd.DataFrame]:
    if CACHE.exists():
        return pd.read_pickle(CACHE)
    result = {}
    for index, symbol in enumerate((*SYMBOLS, "SPY", "QQQ"), 1):
        frame = yf.download(symbol, period="2y", interval="60m", auto_adjust=False,
                            prepost=False, progress=False, threads=False)
        if isinstance(frame.columns, pd.MultiIndex):
            frame.columns = frame.columns.get_level_values(0)
        frame = frame.rename(columns=str.lower).dropna(subset=["open", "high", "low", "close"])
        result[symbol] = frame
        print(f"downloaded {index}/{len(SYMBOLS) + 2} {symbol}: {len(frame)}", flush=True)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    pd.to_pickle(result, CACHE)
    return result


def sessions(data: dict[str, pd.DataFrame]) -> dict[str, dict[str, list[dict[str, float]]]]:
    result: dict[str, dict[str, list[dict[str, float]]]] = {}
    for symbol, frame in data.items():
        local = frame.copy()
        if local.index.tz is None:
            local.index = local.index.tz_localize("UTC")
        local.index = local.index.tz_convert("America/New_York")
        local = local[(local.index.hour >= 9) & (local.index.hour < 16)]
        for day, rows in local.groupby(local.index.date):
            records = [{key: float(row[key]) for key in ("open", "high", "low", "close", "volume")}
                       for _, row in rows.iterrows()]
            if len(records) >= 6:
                result.setdefault(day.isoformat(), {})[symbol] = records
    return result


def metric(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(row["net_r"]) for row in rows]
    positive, negative = sum(v for v in values if v > 0), -sum(v for v in values if v < 0)
    peak = equity = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "trades": len(values), "wins": sum(value > 0 for value in values),
        "win_rate": round(sum(value > 0 for value in values) / len(values) * 100, 2) if values else 0,
        "net_r": round(sum(values), 3),
        "expectancy_r": round(sum(values) / len(values), 4) if values else 0,
        "profit_factor": round(positive / negative, 3) if negative else None,
        "max_drawdown_r": round(drawdown, 3),
    }


def _outcome(rows: list[dict[str, float]], entry_index: int, side: str,
             stop_bps: int, target_r: int, cost_bps: float) -> tuple[float, str]:
    entry = rows[entry_index]["open"]
    direction = 1 if side == "LONG" else -1
    risk = entry * stop_bps / 10_000
    stop, target = entry - direction * risk, entry + direction * risk * target_r
    exit_price, reason = rows[-1]["close"], "close"
    for row in rows[entry_index:]:
        stop_hit = row["low"] <= stop if direction > 0 else row["high"] >= stop
        target_hit = row["high"] >= target if direction > 0 else row["low"] <= target
        if stop_hit:
            exit_price, reason = stop, "stop"
            break
        if target_hit:
            exit_price, reason = target, "target"
            break
    return (exit_price - entry) * direction / risk - cost_bps / stop_bps, reason


def replay(data: dict[str, dict[str, list[dict[str, float]]]], days: list[str],
           cfg: Config, cost_bps: float) -> list[dict[str, Any]]:
    trades = []
    for day in days:
        market = data[day].get("SPY")
        if not market or len(market) <= cfg.observation_bars:
            continue
        market_return = market[cfg.observation_bars - 1]["close"] / market[0]["open"] - 1
        ranked = []
        for symbol in SYMBOLS:
            rows = data[day].get(symbol)
            if not rows or len(rows) <= cfg.observation_bars:
                continue
            observed = rows[cfg.observation_bars - 1]["close"] / rows[0]["open"] - 1
            ranked.append((observed - market_return, symbol, rows))
        if len(ranked) < 8:
            continue
        ranked.sort()
        chosen = [(item, "SHORT") for item in ranked[:cfg.rank_count]]
        chosen += [(item, "LONG") for item in reversed(ranked[-cfg.rank_count:])]
        if cfg.mode == "reversal":
            chosen = [(item, "LONG" if side == "SHORT" else "SHORT") for item, side in chosen]
        daily = []
        for (relative, symbol, rows), side in chosen:
            signed_relative = relative if side == "LONG" else -relative
            if signed_relative * 10_000 < cfg.minimum_move_bps:
                continue
            signed_market = market_return if side == "LONG" else -market_return
            if cfg.market_filter == "sign" and signed_market <= 0:
                continue
            net_r, reason = _outcome(rows, cfg.observation_bars, side, cfg.stop_bps, cfg.target_r, cost_bps)
            daily.append({"date": day, "symbol": symbol, "side": side,
                          "relative_bps": relative * 10_000, "market_bps": market_return * 10_000,
                          "exit_reason": reason, "net_r": net_r})
        trades.extend(daily[:5])
    return trades


def configs() -> list[Config]:
    return [Config(observation, mode, ranks, move, market, stop, target)
            for observation in (1, 2)
            for mode in ("momentum", "reversal")
            for ranks in (1, 2)
            for move in (0, 20, 40)
            for market in ("none", "sign")
            for stop in (60, 100, 150, 200)
            for target in (2, 4, 99)]


def gate(row: dict[str, Any], minimum: int, pf: float, expectancy: float) -> bool:
    return (row["trades"] >= minimum and row["net_r"] > 0
            and (row["profit_factor"] or 0) > pf and row["expectancy_r"] > expectancy)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(ROOT / "data" / "yfinance_regime_walkforward.json"))
    parser.add_argument("--cost-bps", type=float, default=14.0)
    args = parser.parse_args()
    data = sessions(download())
    days = sorted(day for day, rows in data.items() if "SPY" in rows and "QQQ" in rows)
    if len(days) < 450:
        raise RuntimeError(f"need at least 450 common sessions, found {len(days)}")
    days = days[-500:]
    holdout_days = days[-100:]
    development_days = days[-160:-100]
    validation_days = days[-220:-160]
    train_days = days[:-220]
    eligible = []
    for cfg in configs():
        train = metric(replay(data, train_days, cfg, args.cost_bps))
        validation = metric(replay(data, validation_days, cfg, args.cost_bps))
        development = metric(replay(data, development_days, cfg, args.cost_bps))
        if gate(train, 150, 1.0, 0) and gate(validation, 50, 1.1, 0.05) and gate(development, 50, 1.1, 0.05):
            eligible.append({"config": asdict(cfg), "train": train,
                             "validation": validation, "development": development})
    eligible.sort(key=lambda row: (min(row["validation"]["profit_factor"], row["development"]["profit_factor"]),
                                   row["validation"]["net_r"] + row["development"]["net_r"]), reverse=True)
    selected = eligible[0] if eligible else None
    final_trades = replay(data, holdout_days, Config(**selected["config"]), args.cost_bps) if selected else []
    holdout = metric(final_trades) if selected else None
    if holdout:
        holdout["breakdown"] = {side: metric([row for row in final_trades if row["side"] == side])
                                for side in ("LONG", "SHORT")}
    passed = bool(holdout and gate(holdout, 100, 1.2, 0.1))
    report = {
        "generated_at": datetime.now(UTC).isoformat(), "source": "Yahoo Finance 60m adjusted=false",
        "cost_bps": args.cost_bps, "sessions": len(days),
        "split": {"train": [train_days[0], train_days[-1]],
                  "validation": [validation_days[0], validation_days[-1]],
                  "development": [development_days[0], development_days[-1]],
                  "holdout": [holdout_days[0], holdout_days[-1]]},
        "selected": selected, "holdout": holdout, "passed": passed,
        "leaderboard": eligible[:20],
        "scope": "regime research only; not a 10m/5m/1m execution backtest",
    }
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({"selected": selected, "holdout": holdout, "passed": passed}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
