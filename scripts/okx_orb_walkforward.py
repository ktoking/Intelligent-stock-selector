#!/usr/bin/env python3
"""Cost-aware opening-range-breakout research with a sealed final window."""
from __future__ import annotations

import argparse
import glob
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "okx_backtest_cache"
NY = ZoneInfo("America/New_York")
UTC = timezone.utc
DEFAULT_SYMBOLS = (
    "NVDA", "AMD", "MU", "INTC", "TSM", "AAPL", "MSFT",
    "AMZN", "META", "GOOGL", "TSLA", "MSTR", "HOOD",
)


@dataclass(frozen=True)
class Config:
    opening_minutes: int
    entry_cutoff: int
    volume_ratio: float
    breakout_bps: float
    stop_range: float
    target_r: float
    market_filter: str


def _cash_rows(path: str, expected_day: str) -> list[list[str]]:
    rows = json.loads(Path(path).read_text())
    result = []
    for row in rows:
        local = datetime.fromtimestamp(int(row[0]) / 1000, UTC).astimezone(NY)
        if local.date().isoformat() != expected_day:
            continue
        if (9, 30) <= (local.hour, local.minute) < (16, 0):
            result.append(row)
    return sorted(result, key=lambda row: int(row[0]))


def load_cache(symbols: tuple[str, ...]) -> dict[str, dict[str, list[list[str]]]]:
    result: dict[str, dict[str, list[list[str]]]] = {}
    wanted = set(symbols) | {"SPY", "QQQ"}
    pattern = re.compile(r"^([A-Z]+)-USDT-SWAP_(\d{4}-\d{2}-\d{2})_1m\.json$")
    for file_name in glob.glob(str(CACHE / "*-USDT-SWAP_*_1m.json")):
        match = pattern.match(Path(file_name).name)
        if not match or match.group(1) not in wanted:
            continue
        symbol, day = match.groups()
        rows = _cash_rows(file_name, day)
        if len(rows) >= 350:
            result.setdefault(day, {})[symbol] = rows
    return result


def metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(row["net_r"]) for row in trades]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    gross_profit, gross_loss = sum(wins), -sum(losses)
    peak = equity = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "trades": len(values), "wins": len(wins),
        "win_rate": round(len(wins) / len(values) * 100, 2) if values else 0.0,
        "net_r": round(sum(values), 3),
        "expectancy_r": round(sum(values) / len(values), 4) if values else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
        "max_drawdown_r": round(drawdown, 3),
    }


def _market_allowed(side: str, market_return_bps: float, mode: str) -> bool:
    signed = market_return_bps if side == "LONG" else -market_return_bps
    if mode == "sign":
        return signed > 0
    if mode == "strong":
        return signed > 5
    return True


def trade_for_day(symbol: str, day: str, rows: list[list[str]], market_rows: list[list[str]],
                  cfg: Config, cost_bps: float) -> dict[str, Any] | None:
    if len(rows) <= cfg.opening_minutes + 2 or len(market_rows) <= cfg.opening_minutes:
        return None
    opening = rows[:cfg.opening_minutes]
    opening_high = max(float(row[2]) for row in opening)
    opening_low = min(float(row[3]) for row in opening)
    opening_range = opening_high - opening_low
    if opening_range <= 0:
        return None
    market_open = float(market_rows[0][1])
    market_return_bps = (float(market_rows[cfg.opening_minutes - 1][4]) / market_open - 1) * 10_000
    end = min(cfg.entry_cutoff, len(rows) - 2)
    for index in range(cfg.opening_minutes, end):
        row = rows[index]
        close = float(row[4])
        buffer = close * cfg.breakout_bps / 10_000
        side = "LONG" if close > opening_high + buffer else "SHORT" if close < opening_low - buffer else ""
        if not side or not _market_allowed(side, market_return_bps, cfg.market_filter):
            continue
        previous = rows[max(0, index - 20):index]
        average_volume = sum(float(item[5]) for item in previous) / max(len(previous), 1)
        volume_ratio = float(row[5]) / max(average_volume, 1e-9)
        if volume_ratio < cfg.volume_ratio:
            continue
        direction = 1 if side == "LONG" else -1
        entry_index = index + 1
        entry = float(rows[entry_index][1])
        risk = max(opening_range * cfg.stop_range, entry * 0.0025)
        stop, target = entry - direction * risk, entry + direction * risk * cfg.target_r
        exit_price, reason = float(rows[-1][4]), "cash_close"
        exit_time = int(rows[-1][0])
        for future in rows[entry_index:]:
            high, low = float(future[2]), float(future[3])
            stop_hit = low <= stop if direction > 0 else high >= stop
            target_hit = high >= target if direction > 0 else low <= target
            if stop_hit:  # Conservative when both levels print inside one minute.
                exit_price, reason, exit_time = stop, "stop", int(future[0])
                break
            if target_hit:
                exit_price, reason, exit_time = target, "target", int(future[0])
                break
        risk_bps = risk / entry * 10_000
        gross_r = (exit_price - entry) * direction / risk
        net_r = gross_r - cost_bps / risk_bps
        return {
            "date": day, "symbol": symbol, "side": side,
            "entry_time": int(rows[entry_index][0]), "exit_time": exit_time,
            "entry_price": entry, "exit_price": exit_price, "exit_reason": reason,
            "volume_ratio": volume_ratio, "market_return_bps": market_return_bps,
            "opening_range_bps": opening_range / float(opening[0][1]) * 10_000,
            "breakout_strength": abs(close - (opening_high if side == "LONG" else opening_low)) / opening_range,
            "gross_r": gross_r, "net_r": net_r,
        }
    return None


def replay(data: dict[str, dict[str, list[list[str]]]], days: list[str], symbols: tuple[str, ...],
           cfg: Config, cost_bps: float, max_daily: int = 5) -> list[dict[str, Any]]:
    trades = []
    for day in days:
        market = data.get(day, {}).get("SPY")
        if not market:
            continue
        daily = []
        for symbol in symbols:
            rows = data.get(day, {}).get(symbol)
            if rows:
                trade = trade_for_day(symbol, day, rows, market, cfg, cost_bps)
                if trade:
                    daily.append(trade)
        daily.sort(key=lambda row: (row["volume_ratio"] * (1 + row["breakout_strength"])), reverse=True)
        trades.extend(sorted(daily[:max_daily], key=lambda row: row["entry_time"]))
    return trades


def configs() -> list[Config]:
    return [Config(opening, cutoff, volume, buffer, stop, target, market)
            for opening in (15, 30, 45, 60)
            for cutoff in (120, 240)
            for volume in (1.0, 1.5, 2.5)
            for buffer in (0.0, 5.0)
            for stop in (0.5, 1.0, 1.5)
            for target in (1.5, 2.0, 3.0, 4.0)
            for market in ("none", "sign")]


def passes(result: dict[str, Any], minimum: int = 25) -> bool:
    return (result["trades"] >= minimum and result["net_r"] > 0
            and (result["profit_factor"] or 0) > 1.1 and result["expectancy_r"] > 0.05)


def train_passes(result: dict[str, Any]) -> bool:
    """Reject parameter sets whose apparent validation edge contradicts the base sample."""
    return (result["trades"] >= 100 and result["net_r"] > 0
            and (result["profit_factor"] or 0) > 1.0 and result["expectancy_r"] > 0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cost-bps", type=float, default=14.0)
    parser.add_argument("--output", default=str(ROOT / "data" / "okx_orb_walkforward.json"))
    args = parser.parse_args()
    symbols = DEFAULT_SYMBOLS
    data = load_cache(symbols)
    days = sorted(day for day, rows in data.items() if "SPY" in rows)[-90:]
    if len(days) < 90:
        raise RuntimeError(f"need 90 SPY sessions, found {len(days)}")
    train_days, validation_days = days[:40], days[40:50]
    development_days, holdout_days = days[50:60], days[60:90]
    leaderboard = []
    candidates = configs()
    for index, cfg in enumerate(candidates, 1):
        train = metrics(replay(data, train_days, symbols, cfg, args.cost_bps))
        validation = metrics(replay(data, validation_days, symbols, cfg, args.cost_bps))
        development = metrics(replay(data, development_days, symbols, cfg, args.cost_bps))
        if train_passes(train) and passes(validation) and passes(development):
            leaderboard.append({"config": asdict(cfg), "train": train,
                                "validation": validation, "development": development})
        if index % 1000 == 0:
            print(f"evaluated {index}/{len(candidates)}; eligible={len(leaderboard)}", flush=True)
    leaderboard.sort(key=lambda row: (
        min(row["validation"]["profit_factor"] or 0, row["development"]["profit_factor"] or 0),
        row["validation"]["net_r"] + row["development"]["net_r"],
    ), reverse=True)
    selected = leaderboard[0] if leaderboard else None
    holdout = None
    passed = False
    if selected:
        cfg = Config(**selected["config"])
        final_trades = replay(data, holdout_days, symbols, cfg, args.cost_bps)
        holdout = metrics(final_trades)
        holdout["breakdown"] = {
            side: metrics([row for row in final_trades if row["side"] == side])
            for side in ("LONG", "SHORT")
        }
        passed = (holdout["trades"] >= 100 and holdout["net_r"] > 0
                  and (holdout["profit_factor"] or 0) > 1.2
                  and holdout["expectancy_r"] > 0.1)
    report = {
        "generated_at": datetime.now(UTC).isoformat(), "cost_bps": args.cost_bps,
        "symbols": list(symbols), "available_sessions": len(days),
        "split": {"train": [train_days[0], train_days[-1]],
                  "validation": [validation_days[0], validation_days[-1]],
                  "development": [development_days[0], development_days[-1]],
                  "holdout": [holdout_days[0], holdout_days[-1]]},
        "selection_gate": "validation and development each n>=25, PF>1.1, netR>0, expectancy>0.05R",
        "deployment_gate": "sealed holdout n>=100, PF>1.2, netR>0, expectancy>0.1R",
        "selected": selected, "holdout": holdout, "passed": passed,
        "leaderboard": leaderboard[:20],
    }
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({"selected": selected, "holdout": holdout, "passed": passed}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
