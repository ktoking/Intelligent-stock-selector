#!/usr/bin/env python3
"""Cash-funded TQQQ martingale study with a chronological holdout.

Read-only Futunn five-minute candles. Signals use a completed bar and market
orders fill at the next regular-hours bar's open. All open positions are marked
to market; no historical order endpoint is used.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parent.parent
NY = ZoneInfo("America/New_York")
UTC = timezone.utc
CAPITAL = 10_000.0
FIRST_ORDER = 1_000.0
BASE_COST_BPS = 10.0  # one way; includes fee and execution-price allowance
STRESS_COST_BPS = 25.0
SYMBOLS = ("US.TQQQ", "US.SOXL")


@dataclass(frozen=True)
class Rule:
    drop_pct: float
    take_profit_pct: float
    multiplier: float
    max_adds: int


def _signed_get(path: str, params: dict[str, str | int], private_key: Ed25519PrivateKey, app_key: str) -> dict:
    query = urllib.parse.urlencode(params)
    for attempt in range(8):
        timestamp = str(int(time.time() * 1000))
        signing_text = "\n".join((timestamp, "GET", path, query, "")).encode()
        request = urllib.request.Request(
            f"https://webapi.futunn.com{path}?{query}",
            headers={
                "X-Api-Key": app_key,
                "X-Timestamp": timestamp,
                "X-Nonce": secrets.token_urlsafe(24),
                "Authorization": base64.b64encode(private_key.sign(signing_text)).decode(),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code != 429 or attempt == 7:
                raise
            result = {"ret_code": -11}
        if result.get("ret_code") == -11 and attempt < 7:
            delay = min(60, 2 ** (attempt + 1))
            print(f"Futunn rate limit; retrying in {delay}s", flush=True)
            time.sleep(delay)
            continue
        if result.get("ret_code") != 0:
            raise RuntimeError(f"Futunn history error: {result.get('ret_code')} {result.get('ret_msg')}")
        return result.get("data") or {}
    raise RuntimeError("Futunn history rate limit persisted after retries")


def fetch_history(symbol: str, start: date, end: date, cache_dir: Path | None = None) -> list[dict]:
    app_key: str | None = None
    private_key: Ed25519PrivateKey | None = None
    rows: dict[int, dict] = {}
    cursor = start
    while cursor <= end:
        # Four calendar days have at most 312 regular five-minute US bars.
        chunk_end = min(cursor + timedelta(days=4), end + timedelta(days=1))
        chunk_cache = (cache_dir / f"{symbol}_5m_{cursor}_{chunk_end}.json") if cache_dir else None
        if chunk_cache and chunk_cache.exists():
            data = json.loads(chunk_cache.read_text())
        else:
            if private_key is None:
                app_key = os.environ.get("FUTUNN_APP_KEY")
                key_file = os.environ.get("FUTUNN_PRIVATE_KEY_FILE")
                if not app_key or not key_file:
                    raise RuntimeError("Set FUTUNN_APP_KEY and FUTUNN_PRIVATE_KEY_FILE for read-only history")
                loaded = serialization.load_pem_private_key(Path(key_file).read_bytes(), password=None)
                if not isinstance(loaded, Ed25519PrivateKey):
                    raise RuntimeError("Futunn private key must use Ed25519")
                private_key = loaded
            data = _signed_get(
                f"/api/v1.0/quote/{symbol}/history-kline",
                {"start": cursor.isoformat(), "end": chunk_end.isoformat(),
                 "ktype": 6, "autype": 1, "num": 370, "extended_time": 0},
                private_key, app_key,
            )
            if chunk_cache:
                chunk_cache.write_text(json.dumps(data, ensure_ascii=False))
            time.sleep(0.7)
        chunk = data.get("kline_list") or []
        if len(chunk) >= 370 or data.get("next_time"):
            raise RuntimeError(f"Futunn page truncated for {symbol} {cursor}: {len(chunk)} rows")
        for row in chunk:
            stamp = int(row["time_key"])
            local = datetime.fromtimestamp(stamp / 1000, UTC).astimezone(NY)
            # Futunn labels a completed five-minute bar by its end time:
            # the regular session runs from 09:35 through 16:00 (78 bars).
            if start <= local.date() <= end and local.weekday() < 5 and (9, 35) <= (local.hour, local.minute) <= (16, 0):
                rows[stamp] = {
                    "ts": stamp, "date": local.date().isoformat(),
                    "open": float(row["open"]), "high": float(row["high"]),
                    "low": float(row["low"]), "close": float(row["close"]),
                    "volume": float(row["volume"]),
                }
        cursor = chunk_end
    result = [rows[key] for key in sorted(rows)]
    if not result:
        raise RuntimeError(f"No Futunn five-minute candles for {symbol}")
    return result


def _quality(rows: list[dict]) -> dict:
    by_day: dict[str, list[int]] = {}
    for row in rows:
        by_day.setdefault(row["date"], []).append(int(row["ts"]))
    gaps = []
    for day, stamps in by_day.items():
        for before, after in zip(stamps, stamps[1:]):
            if after - before != 300_000:
                gaps.append({"date": day, "from": before, "to": after})
    return {
        "bars": len(rows), "sessions": len(by_day),
        "first": rows[0]["date"], "last": rows[-1]["date"],
        "bars_per_session_min": min(map(len, by_day.values())),
        "bars_per_session_max": max(map(len, by_day.values())),
        "short_sessions_under_78_bars": sum(len(stamps) < 78 for stamps in by_day.values()),
        "first_bar_ny": datetime.fromtimestamp(rows[0]["ts"] / 1000, UTC).astimezone(NY).strftime("%H:%M"),
        "last_bar_ny": datetime.fromtimestamp(rows[-1]["ts"] / 1000, UTC).astimezone(NY).strftime("%H:%M"),
        "intra_session_gaps": gaps[:20], "gap_count": len(gaps),
    }


def backtest(bars: list[dict], rule: Rule, *, cost_bps: float = BASE_COST_BPS) -> dict:
    if not bars:
        raise ValueError("No bars")
    cash, shares, basis = CAPITAL, 0, 0.0
    last_buy = 0.0
    add_count = cycles = winning_cycles = 0
    realized = 0.0
    pending: str | None = None
    peak = CAPITAL
    max_drawdown = max_deployed = max_exposure = 0.0
    worst_shock_equity = CAPITAL
    worst_shock_date = bars[0]["date"]
    daily: dict[str, float] = {}
    trades: list[dict] = []
    roundtrip_cost = cost_bps / 10_000
    for bar in bars:
        day, opened, closed = bar["date"], float(bar["open"]), float(bar["close"])
        if opened <= 0 or closed <= 0:
            raise ValueError("Nonpositive price")
        just_sold = False
        if pending == "sell" and shares:
            fill = opened * (1 - roundtrip_cost)
            proceeds = shares * fill
            pnl = proceeds - basis
            cash += proceeds
            realized += pnl
            cycles += 1
            winning_cycles += pnl > 0
            trades.append({"date": day, "side": "SELL", "shares": shares, "price": fill,
                           "pnl": round(pnl, 4), "adds": add_count})
            shares, basis, last_buy, add_count = 0, 0.0, 0.0, 0
            just_sold = True
        elif pending == "add" and shares:
            fill = opened * (1 + roundtrip_cost)
            intended = FIRST_ORDER * rule.multiplier ** (add_count + 1)
            quantity = min(math.floor(intended / fill), math.floor(cash / fill))
            if quantity > 0:
                cash -= quantity * fill
                shares += quantity
                basis += quantity * fill
                last_buy = fill
                add_count += 1
                trades.append({"date": day, "side": "ADD", "shares": quantity, "price": fill})
        pending = None
        if shares == 0 and not just_sold:
            fill = opened * (1 + roundtrip_cost)
            quantity = min(math.floor(FIRST_ORDER / fill), math.floor(cash / fill))
            if quantity > 0:
                cash -= quantity * fill
                shares, basis, last_buy, add_count = quantity, quantity * fill, fill, 0
                trades.append({"date": day, "side": "BUY", "shares": quantity, "price": fill})

        equity = cash + shares * closed
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, 1 - equity / peak)
        max_deployed = max(max_deployed, basis / CAPITAL)
        max_exposure = max(max_exposure, shares * closed / equity)
        shocked_equity = cash + shares * closed * 0.8
        if shocked_equity < worst_shock_equity:
            worst_shock_equity = shocked_equity
            worst_shock_date = day
        daily[day] = equity
        if shares:
            avg = basis / shares
            if closed >= avg * (1 + rule.take_profit_pct):
                pending = "sell"
            elif add_count < rule.max_adds and closed <= last_buy * (1 - rule.drop_pct):
                pending = "add"

    final_close = float(bars[-1]["close"])
    final_equity = cash + shares * final_close
    unrealized = shares * final_close - basis
    marked_liquidation = cash + shares * final_close * (1 - roundtrip_cost)
    daily_values = list(daily.values())
    longest_underwater = current_underwater = 0
    high = CAPITAL
    for value in daily_values:
        if value < high:
            current_underwater += 1
            longest_underwater = max(longest_underwater, current_underwater)
        else:
            high = value
            current_underwater = 0
    return {
        "return_pct": round((final_equity / CAPITAL - 1) * 100, 4),
        "liquidation_return_pct": round((marked_liquidation / CAPITAL - 1) * 100, 4),
        "max_drawdown_pct": round(max_drawdown * 100, 4),
        "max_deployed_pct": round(max_deployed * 100, 4),
        "max_exposure_pct": round(max_exposure * 100, 4),
        "cycles": cycles, "winning_cycles": winning_cycles,
        "open_shares": shares, "open_basis_usd": round(basis, 2),
        "open_unrealized_usd": round(unrealized, 2),
        "realized_usd": round(realized, 2),
        "longest_underwater_sessions": longest_underwater,
        "worst_daily_change_pct": round(min(
            ((current / previous - 1) * 100)
            for previous, current in zip([CAPITAL] + daily_values[:-1], daily_values)
        ), 4),
        "shock_minus_20pct_return_pct": round((cash + shares * final_close * 0.8) / CAPITAL * 100 - 100, 4),
        "worst_historical_20pct_shock_return_pct": round((worst_shock_equity / CAPITAL - 1) * 100, 4),
        "worst_historical_20pct_shock_date": worst_shock_date,
        "trades": trades,
    }


def buy_and_hold(bars: list[dict], cost_bps: float) -> dict:
    entry = float(bars[0]["open"]) * (1 + cost_bps / 10_000)
    shares = math.floor(CAPITAL / entry)
    cash = CAPITAL - shares * entry
    peak = CAPITAL
    drawdown = 0.0
    for bar in bars:
        equity = cash + shares * float(bar["close"])
        peak = max(peak, equity)
        drawdown = max(drawdown, 1 - equity / peak)
    final = cash + shares * float(bars[-1]["close"]) * (1 - cost_bps / 10_000)
    return {"liquidation_return_pct": round((final / CAPITAL - 1) * 100, 4),
            "max_drawdown_pct": round(drawdown * 100, 4)}


def summarize(value: dict) -> dict:
    return {key: item for key, item in value.items() if key != "trades"}


def analyze(data: dict[str, list[dict]], split: date) -> dict:
    baseline = Rule(0.05, 0.05, 1.25, 5)
    grid = [Rule(drop, take, multiple, adds)
            for drop in (0.03, 0.05, 0.07, 0.10)
            for take in (0.03, 0.05, 0.07, 0.10)
            for multiple in (1.0, 1.25, 1.5, 2.0)
            for adds in (3, 5, 8)]
    report = {"source": "Futunn REST history-kline; 5m; autype=1 (split-adjusted, excluding dividends)",
              "fill_rule": "RTH 5m completed close signal -> next RTH 5m open market fill; integer ETF shares",
              "capital_usd": CAPITAL, "first_order_usd": FIRST_ORDER,
              "base_cost_bps_per_side": BASE_COST_BPS,
              "stress_cost_bps_per_side": STRESS_COST_BPS,
              "split": split.isoformat(), "tested_rules": len(grid), "symbols": {}}
    for symbol, bars in data.items():
        develop = [bar for bar in bars if bar["date"] < split.isoformat()]
        holdout = [bar for bar in bars if bar["date"] >= split.isoformat()]
        if len(develop) < 2000 or len(holdout) < 2000:
            raise RuntimeError(f"Insufficient development/holdout bars for {symbol}")
        entries = []
        for rule in grid:
            value = backtest(develop, rule)
            entries.append((rule, summarize(value)))
        by_return = max(entries, key=lambda x: x[1]["liquidation_return_pct"])
        qualified = [x for x in entries if x[1]["max_drawdown_pct"] <= 20]
        by_risk = max(qualified or entries, key=lambda x: x[1]["liquidation_return_pct"] - 1.5 * x[1]["max_drawdown_pct"])
        selected = {"fixed_baseline": baseline,
                    "development_highest_return": by_return[0],
                    "development_risk_adjusted": by_risk[0]}
        evaluations = {}
        for label, rule in selected.items():
            dev = backtest(develop, rule)
            test = backtest(holdout, rule)
            stressed = backtest(holdout, rule, cost_bps=STRESS_COST_BPS)
            evaluations[label] = {
                "rule": asdict(rule),
                "development": summarize(dev),
                "holdout": summarize(test),
                "holdout_stress": summarize(stressed),
                "holdout_trades": test["trades"],
            }
        report["symbols"][symbol] = {
            "coverage": _quality(bars),
            "development": _quality(develop), "holdout": _quality(holdout),
            "buy_and_hold_holdout": buy_and_hold(holdout, BASE_COST_BPS),
            "evaluations": evaluations,
        }
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=date.fromisoformat, default=date(2026, 4, 7))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 10, 7))
    parser.add_argument("--split", type=date.fromisoformat, default=date(2026, 7, 8))
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "tqqq_martingale_research_20261008")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    data = {}
    for symbol in SYMBOLS:
        cache = args.output / f"{symbol}_5m_{args.start}_{args.end}.json"
        if cache.exists() and not args.refresh:
            rows = json.loads(cache.read_text())
        else:
            rows = fetch_history(symbol, args.start, args.end, args.output)
            cache.write_text(json.dumps(rows, ensure_ascii=False))
        data[symbol] = rows
        print(f"{symbol}: {_quality(rows)}", flush=True)
    result = analyze(data, args.split)
    target = args.output / "report.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"Report: {target}")
    for symbol, item in result["symbols"].items():
        for label, value in item["evaluations"].items():
            test = value["holdout"]
            print(symbol, label, "rule", value["rule"], "return", test["liquidation_return_pct"],
                  "max_dd", test["max_drawdown_pct"], "open", test["open_shares"], flush=True)


if __name__ == "__main__":
    main()
