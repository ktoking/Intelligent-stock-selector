#!/usr/bin/env python3
"""Cost-aware 10m -> 5m -> 1m backtest for OKX equity perpetuals."""
from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
import json
import math
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_intraday_agent import OKX, atr, ema, rsi, settings, vwap  # noqa: E402
from scripts.okx_trade_replay import candle_window, closed_chronological  # noqa: E402

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
CACHE_DIR = ROOT / "data" / "okx_backtest_cache"
DEFAULT_SYMBOLS = (
    "SPY-USDT-SWAP", "QQQ-USDT-SWAP", "NVDA-USDT-SWAP", "AMD-USDT-SWAP",
    "MU-USDT-SWAP", "MRVL-USDT-SWAP", "INTC-USDT-SWAP", "TSM-USDT-SWAP",
    "AAPL-USDT-SWAP", "MSFT-USDT-SWAP", "AMZN-USDT-SWAP", "META-USDT-SWAP",
    "GOOGL-USDT-SWAP", "TSLA-USDT-SWAP", "MSTR-USDT-SWAP", "HOOD-USDT-SWAP",
)


@dataclass(frozen=True)
class StrategyConfig:
    name: str
    min_10m_volume: float = 1.0
    min_5m_volume: float = 0.8
    max_extension_atr: float = 0.8
    confirmation_bars: int = 2
    min_1m_volume: float = 0.0
    require_confirmation_body: bool = False
    require_ema_slope: bool = True
    require_rsi_band: bool = True
    require_5m_extension: bool = True
    min_reward_cost_multiple: float = 3.0
    round_trip_cost_bps: float = 14.0
    max_holding_minutes: int = 60
    cooldown_minutes: int = 30
    stop_atr_multiplier: float = 1.2
    min_stop_bps: float = 20.0
    breakeven_r: float = 1.0
    scale_target_r: float = 1.5
    scale_fraction: float = 0.4
    trail_stop_r: float = 1.0
    enable_5m_reversal: bool = True


def aggregate_bars(rows_1m: list[list[str]], minutes: int) -> list[list[str]]:
    width = minutes * 60_000
    buckets: dict[int, list[list[str]]] = {}
    for row in closed_chronological(rows_1m):
        bucket = int(row[0]) // width * width
        buckets.setdefault(bucket, []).append(row)
    result = []
    for bucket, rows in sorted(buckets.items()):
        rows.sort(key=lambda row: int(row[0]))
        expected = {bucket + index * 60_000 for index in range(minutes)}
        if {int(row[0]) for row in rows} != expected:
            continue
        result.append([
            str(bucket), rows[0][1], str(max(float(row[2]) for row in rows)),
            str(min(float(row[3]) for row in rows)), rows[-1][4],
            str(sum(float(row[5]) for row in rows)),
            str(sum(float(row[6]) for row in rows)),
            str(sum(float(row[7]) for row in rows)), "1",
        ])
    return result


def _metrics(rows: list[list[str]], index: int) -> dict[str, float] | None:
    if index < 21:
        return None
    window = rows[:index + 1]
    closes = [float(row[4]) for row in window]
    volumes = [float(row[5]) for row in window]
    atr14 = atr(window)
    if atr14 <= 0:
        return None
    fast = ema(closes[-35:], 9)
    slow = ema(closes[-35:], 21)
    previous_fast = ema(closes[-36:-1], 9) if len(closes) >= 36 else fast
    return {
        "price": closes[-1], "ema9": fast, "ema21": slow,
        "ema9_slope": fast - previous_fast, "rsi14": rsi(closes),
        "atr14": atr14, "vwap20": vwap(window),
        "volume_ratio": volumes[-1] / max(sum(volumes[-21:-1]) / 20, 1e-9),
    }


def _session_name(stamp_ms: int) -> str:
    local = datetime.fromtimestamp(stamp_ms / 1000, UTC).astimezone(NY)
    value = local.time()
    if clock_time(9, 30) <= value < clock_time(10, 0):
        return "open30"
    if clock_time(10, 0) <= value < clock_time(16, 0):
        return "cash_rest"
    if clock_time(6, 0) <= value < clock_time(9, 30):
        return "premarket"
    return "afterhours"


def _entry_allowed(stamp_ms: int) -> bool:
    local = datetime.fromtimestamp(stamp_ms / 1000, UTC).astimezone(NY)
    return local.weekday() < 5 and clock_time(9, 35) <= local.time() < clock_time(15, 30)


def _bar_index_at_or_after(rows: list[list[str]], stamp_ms: int,
                           stamps: list[int] | None = None) -> int | None:
    stamps = stamps or [int(row[0]) for row in rows]
    index = bisect_left(stamps, stamp_ms)
    return index if index < len(rows) else None


def _five_min_reversal(side: str, bars: list[list[str]], stamp_ms: int,
                       close_stamps: list[int] | None = None,
                       metrics_cache: dict[int, dict[str, float] | None] | None = None) -> bool:
    close_stamps = close_stamps or [int(row[0]) + 300_000 for row in bars]
    index = bisect_right(close_stamps, stamp_ms) - 1
    if index < 0:
        return False
    metrics_cache = metrics_cache if metrics_cache is not None else {}
    if index not in metrics_cache:
        metrics_cache[index] = _metrics(bars, index)
    metrics = metrics_cache[index]
    if not metrics:
        return False
    if side == "LONG":
        return metrics["ema9"] < metrics["ema21"] and metrics["price"] < metrics["vwap20"]
    return metrics["ema9"] > metrics["ema21"] and metrics["price"] > metrics["vwap20"]


def simulate_exit(side: str, entry: float, entry_index: int, rows_1m: list[list[str]],
                  rows_5m: list[list[str]], stop_distance: float,
                  cfg: StrategyConfig, five_close_stamps: list[int] | None = None,
                  five_metrics_cache: dict[int, dict[str, float] | None] | None = None) -> dict[str, Any]:
    risk_pct = stop_distance / entry
    stop = entry - stop_distance if side == "LONG" else entry + stop_distance
    breakeven_target = entry + cfg.breakeven_r * stop_distance if side == "LONG" else entry - cfg.breakeven_r * stop_distance
    scale_target = entry + cfg.scale_target_r * stop_distance if side == "LONG" else entry - cfg.scale_target_r * stop_distance
    scaled = False
    scale_return = 0.0
    exit_price = entry
    exit_reason = "time"
    exit_index = min(entry_index + cfg.max_holding_minutes, len(rows_1m) - 1)
    for index in range(entry_index, min(entry_index + cfg.max_holding_minutes + 1, len(rows_1m))):
        row = rows_1m[index]
        high, low, close = float(row[2]), float(row[3]), float(row[4])
        stamp = int(row[0]) + 60_000
        stop_hit = low <= stop if side == "LONG" else high >= stop
        if stop_hit:
            exit_price, exit_reason, exit_index = stop, "stop", index
            break
        if not scaled:
            reached_breakeven = high >= breakeven_target if side == "LONG" else low <= breakeven_target
            reached_scale = high >= scale_target if side == "LONG" else low <= scale_target
            if reached_scale:
                scaled = True
                scale_return = cfg.scale_fraction * cfg.scale_target_r * risk_pct
                stop = entry  # The runner cannot turn a scaled winner into a large loser.
            elif reached_breakeven:
                stop = entry
        else:
            trail = cfg.trail_stop_r * stop_distance
            stop = max(stop, close - trail) if side == "LONG" else min(stop, close + trail)
        if cfg.enable_5m_reversal and index > entry_index and stamp % 300_000 == 0 and _five_min_reversal(
            side, rows_5m, stamp, five_close_stamps, five_metrics_cache
        ):
            exit_price, exit_reason, exit_index = close, "5m_reversal", index
            break
        exit_price, exit_index = close, index
    directional_return = (exit_price - entry) / entry if side == "LONG" else (entry - exit_price) / entry
    gross_return = scale_return + directional_return * ((1 - cfg.scale_fraction) if scaled else 1.0)
    net_return = gross_return - cfg.round_trip_cost_bps / 10_000
    return {
        "exit_index": exit_index, "exit_time": int(rows_1m[exit_index][0]) + 60_000,
        "exit_price": exit_price, "exit_reason": exit_reason, "scaled": scaled,
        "gross_r": gross_return / risk_pct, "net_r": net_return / risk_pct,
        "gross_return_bps": gross_return * 10_000, "net_return_bps": net_return * 10_000,
    }


def backtest_symbol(inst_id: str, rows_1m: list[list[str]], cfg: StrategyConfig) -> list[dict[str, Any]]:
    rows_1m = closed_chronological(rows_1m)
    rows_5m, rows_10m = aggregate_bars(rows_1m, 5), aggregate_bars(rows_1m, 10)
    one_stamps = [int(row[0]) for row in rows_1m]
    five_stamps = [int(row[0]) for row in rows_5m]
    five_close_stamps = [stamp + 300_000 for stamp in five_stamps]
    five_metrics_cache: dict[int, dict[str, float] | None] = {}
    trades: list[dict[str, Any]] = []
    blocked_until = 0
    for ten_index, ten_bar in enumerate(rows_10m):
        metrics10 = _metrics(rows_10m, ten_index)
        if not metrics10:
            continue
        ten_close_time = int(ten_bar[0]) + 600_000
        if ten_close_time < blocked_until or not _entry_allowed(ten_close_time):
            continue
        long = metrics10["ema9"] > metrics10["ema21"] and metrics10["price"] > metrics10["vwap20"]
        short = metrics10["ema9"] < metrics10["ema21"] and metrics10["price"] < metrics10["vwap20"]
        if not (long or short) or metrics10["volume_ratio"] < cfg.min_10m_volume:
            continue
        side = "LONG" if long else "SHORT"
        if cfg.require_ema_slope and ((side == "LONG" and metrics10["ema9_slope"] <= 0) or (side == "SHORT" and metrics10["ema9_slope"] >= 0)):
            continue
        if cfg.require_rsi_band and ((side == "LONG" and not 48 <= metrics10["rsi14"] <= 72)
                                     or (side == "SHORT" and not 28 <= metrics10["rsi14"] <= 52)):
            continue
        previous = rows_10m[max(0, ten_index - 6):ten_index]
        if not previous:
            continue
        breakout = max(float(row[2]) for row in previous) if side == "LONG" else min(float(row[3]) for row in previous)
        extension = abs(metrics10["price"] - metrics10["ema9"]) / metrics10["atr14"]
        if extension > cfg.max_extension_atr:
            continue
        five_index = _bar_index_at_or_after(rows_5m, ten_close_time, five_stamps)
        if five_index is None:
            continue
        metrics5 = _metrics(rows_5m, five_index)
        if not metrics5 or metrics5["volume_ratio"] < cfg.min_5m_volume:
            continue
        five_valid = (
            metrics5["ema9"] > metrics5["ema21"] and metrics5["price"] > metrics5["vwap20"]
            if side == "LONG" else metrics5["ema9"] < metrics5["ema21"] and metrics5["price"] < metrics5["vwap20"]
        )
        if not five_valid or (cfg.require_5m_extension
                              and abs(metrics5["price"] - metrics5["ema9"]) / metrics5["atr14"] > cfg.max_extension_atr):
            continue
        confirm_start = int(rows_5m[five_index][0]) + 300_000
        start_index = _bar_index_at_or_after(rows_1m, confirm_start, one_stamps)
        if start_index is None:
            continue
        breakout_index: int | None = None
        confirmation_index: int | None = None
        for index in range(start_index, min(start_index + 10, len(rows_1m) - 1)):
            row = rows_1m[index]
            close = float(row[4])
            broke = close > breakout if side == "LONG" else close < breakout
            if breakout_index is None:
                if broke:
                    breakout_index = index
                    if cfg.confirmation_bars <= 1:
                        confirmation_index = index
                        break
                continue
            body_ok = float(row[4]) > float(row[1]) if side == "LONG" else float(row[4]) < float(row[1])
            held = close > breakout if side == "LONG" else close < breakout
            prior_volume = [float(item[5]) for item in rows_1m[max(0, index - 20):index]]
            volume_ratio = float(row[5]) / max(sum(prior_volume) / len(prior_volume), 1e-9) if prior_volume else 0
            enough_bars = index - breakout_index + 1 >= cfg.confirmation_bars
            if held and enough_bars and (body_ok or not cfg.require_confirmation_body) and volume_ratio >= cfg.min_1m_volume:
                confirmation_index = index
                break
            if not held:
                breakout_index = None
        if confirmation_index is None or confirmation_index + 1 >= len(rows_1m):
            continue
        entry_index = confirmation_index + 1
        entry_time = int(rows_1m[entry_index][0])
        if entry_time < blocked_until or not _entry_allowed(entry_time):
            continue
        entry = float(rows_1m[entry_index][1])
        confirmation_row = rows_1m[confirmation_index]
        prior_1m_volume = [float(item[5]) for item in rows_1m[max(0, confirmation_index - 20):confirmation_index]]
        confirmation_volume = (
            float(confirmation_row[5]) / max(sum(prior_1m_volume) / len(prior_1m_volume), 1e-9)
            if prior_1m_volume else 0.0
        )
        stop_distance = max(metrics5["atr14"] * cfg.stop_atr_multiplier, entry * cfg.min_stop_bps / 10_000)
        reward_bps = cfg.scale_target_r * stop_distance / entry * 10_000
        if reward_bps < cfg.round_trip_cost_bps * cfg.min_reward_cost_multiple:
            continue
        result = simulate_exit(
            side, entry, entry_index, rows_1m, rows_5m, stop_distance, cfg,
            five_close_stamps, five_metrics_cache,
        )
        trade = {
            "inst_id": inst_id, "side": side, "entry_time": entry_time,
            "session": _session_name(entry_time), "entry_price": entry,
            "risk_bps": stop_distance / entry * 10_000,
            "score_inputs": {
                "volume10": metrics10["volume_ratio"], "volume5": metrics5["volume_ratio"],
                "volume1": confirmation_volume, "extension_atr": extension,
                "trend10_atr": abs(metrics10["ema9"] - metrics10["ema21"]) / metrics10["atr14"],
                "slope10_atr": abs(metrics10["ema9_slope"]) / metrics10["atr14"],
                "vwap10_atr": abs(metrics10["price"] - metrics10["vwap20"]) / metrics10["atr14"],
                "rsi_directional": metrics10["rsi14"] if side == "LONG" else 100 - metrics10["rsi14"],
                "trend5_atr": abs(metrics5["ema9"] - metrics5["ema21"]) / metrics5["atr14"],
                "vwap5_atr": abs(metrics5["price"] - metrics5["vwap20"]) / metrics5["atr14"],
                "breakout_buffer_atr": abs(float(confirmation_row[4]) - breakout) / metrics5["atr14"],
                "confirmation_body_atr": abs(float(confirmation_row[4]) - float(confirmation_row[1])) / metrics5["atr14"],
            },
            **result,
        }
        trades.append(trade)
        blocked_until = result["exit_time"] + cfg.cooldown_minutes * 60_000
    return trades


def stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    wins = [trade for trade in trades if trade["net_r"] > 0]
    losses = [trade for trade in trades if trade["net_r"] <= 0]
    equity = peak = drawdown = 0.0
    for trade in sorted(trades, key=lambda row: row["exit_time"]):
        equity += trade["net_r"]
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    gross_win = sum(trade["net_r"] for trade in wins)
    gross_loss = -sum(trade["net_r"] for trade in losses)
    return {
        "trades": len(trades), "wins": len(wins),
        "win_rate": round(len(wins) / len(trades) * 100, 2) if trades else 0,
        "net_r": round(sum(trade["net_r"] for trade in trades), 3),
        "avg_r": round(sum(trade["net_r"] for trade in trades) / len(trades), 4) if trades else 0,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "max_drawdown_r": round(drawdown, 3),
        "scaled_trades": sum(bool(trade["scaled"]) for trade in trades),
    }


def _seed_daily_cache(inst_id: str, requested: set[date]) -> None:
    """Reuse older range caches so extending 30d to 90 sessions does not redownload them."""
    missing = {day for day in requested if not (CACHE_DIR / f"{inst_id}_{day}_1m.json").exists()}
    if not missing:
        return
    for source in sorted(CACHE_DIR.glob(f"{inst_id}_*_1m.json")):
        # Daily files are both the destination and the preferred source.
        if source.stem.count("_") == 2:
            continue
        try:
            rows = json.loads(source.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        grouped: dict[date, list[list[str]]] = {}
        for row in rows:
            day = datetime.fromtimestamp(int(row[0]) / 1000, UTC).astimezone(NY).date()
            if day in missing:
                grouped.setdefault(day, []).append(row)
        for day, day_rows in grouped.items():
            (CACHE_DIR / f"{inst_id}_{day}_1m.json").write_text(json.dumps(day_rows, ensure_ascii=False))
            missing.discard(day)
        if not missing:
            return


def load_market_data(client: OKX, symbols: tuple[str, ...], days: list[date]) -> dict[str, list[list[str]]]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    result = {}
    requested = set(days)
    for symbol_index, inst_id in enumerate(symbols, 1):
        _seed_daily_cache(inst_id, requested)
        collected: dict[int, list[str]] = {}
        for day_index, day in enumerate(days, 1):
            cache = CACHE_DIR / f"{inst_id}_{day}_1m.json"
            if cache.exists():
                rows = json.loads(cache.read_text())
                collected.update({int(row[0]): row for row in rows})
                continue
            session_start = datetime.combine(day, clock_time(6, 0), NY).astimezone(UTC)
            session_end = datetime.combine(day, clock_time(20, 0), NY).astimezone(UTC)
            rows = candle_window(client, inst_id, "1m", int(session_start.timestamp() * 1000), int(session_end.timestamp() * 1000))
            cache.write_text(json.dumps(rows, ensure_ascii=False))
            collected.update({int(row[0]): row for row in rows})
            if day_index % 5 == 0 or day_index == len(days):
                print(f"[{symbol_index}/{len(symbols)}] {inst_id}: {day_index}/{len(days)} sessions", file=sys.stderr, flush=True)
            time.sleep(0.02)
        rows = [collected[key] for key in sorted(collected)]
        print(f"[{symbol_index}/{len(symbols)}] ready {inst_id}: {len(rows)} bars", file=sys.stderr, flush=True)
        result[inst_id] = rows
    return result


def weekday_sessions(end: date, count: int) -> list[date]:
    sessions: list[date] = []
    cursor = end
    while len(sessions) < count:
        if cursor.weekday() < 5:
            sessions.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(sessions)


def split_trades(trades: list[dict[str, Any]], split_date: date) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    train, validation = [], []
    for trade in trades:
        local_date = datetime.fromtimestamp(trade["entry_time"] / 1000, UTC).astimezone(NY).date()
        (train if local_date < split_date else validation).append(trade)
    return train, validation


def deployment_viable(candidate: dict[str, Any] | None) -> bool:
    if not candidate:
        return False
    return all(
        candidate[split]["net_r"] > 0 and (candidate[split]["profit_factor"] or 0) > 1
        for split in ("train", "validation")
    )


def run_research(data: dict[str, list[list[str]]], start: date, end: date) -> dict[str, Any]:
    split_date = start + timedelta(days=20)
    configs = [
        StrategyConfig(
            "baseline", min_10m_volume=0.8, min_5m_volume=0.8,
            max_extension_atr=2.0, confirmation_bars=1,
            require_ema_slope=False, min_reward_cost_multiple=0,
        ),
    ]
    for volume in (0.6, 0.8, 1.0):
        for extension in (0.8, 1.2):
            configs.append(StrategyConfig(
                f"hold2_v{volume}_e{extension}", min_10m_volume=volume,
                min_5m_volume=0.6, max_extension_atr=extension,
                confirmation_bars=2,
            ))
    configs.extend([
        StrategyConfig(
            "quality_hold2", min_10m_volume=0.8, min_5m_volume=0.8,
            max_extension_atr=1.0, confirmation_bars=2,
            min_1m_volume=0.8, require_confirmation_body=True,
        ),
        StrategyConfig(
            "quality_hold3", min_10m_volume=0.8, min_5m_volume=0.8,
            max_extension_atr=1.0, confirmation_bars=3,
            min_1m_volume=0.8, require_confirmation_body=True,
        ),
    ])
    outcomes = []
    for cfg in configs:
        trades = [trade for inst_id, rows in data.items() for trade in backtest_symbol(inst_id, rows, cfg)]
        train, validation = split_trades(trades, split_date)
        outcomes.append({"config": asdict(cfg), "train": stats(train), "validation": stats(validation), "all": stats(trades), "trades": trades})
    eligible = [
        row for row in outcomes[1:]
        if row["train"]["trades"] >= 30 and row["validation"]["trades"] >= 10
    ]
    best_candidate = max(
        eligible,
        key=lambda row: (row["train"]["profit_factor"] or 0, row["train"]["net_r"]),
        default=None,
    )
    selected = best_candidate if deployment_viable(best_candidate) else None
    compact = [{key: value for key, value in row.items() if key != "trades"} for row in outcomes]
    return {
        "period": {"start": start.isoformat(), "end": end.isoformat(), "split_date": split_date.isoformat()},
        "symbols": list(data), "cost_model": "10 bps round-trip fee + 4 bps slippage",
        "selection_rule": (
            "train trades >= 30 and validation trades >= 10; rank on train only; "
            "deployment requires positive net R and profit factor > 1 in both splits"
        ),
        "configs": compact,
        "best_candidate": (
            {key: value for key, value in best_candidate.items() if key != "trades"}
            if best_candidate else None
        ),
        "selected": ({key: value for key, value in selected.items() if key != "trades"} if selected else None),
        "selected_trades": best_candidate["trades"] if best_candidate else [],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30, help="number of weekday sessions")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--output", default=str(ROOT / "data" / "okx_multitimeframe_backtest_30d.json"))
    parser.add_argument("--download-only", action="store_true")
    args = parser.parse_args()
    end = datetime.now(NY).date() - timedelta(days=1)
    days = weekday_sessions(end, args.days)
    start = days[0]
    symbols = tuple(item.strip().upper() for item in args.symbols.split(",") if item.strip())
    data = load_market_data(OKX(settings()), symbols, days)
    if args.download_only:
        print(json.dumps({"sessions": len(days), "start": start.isoformat(), "end": end.isoformat(), "bars": {key: len(value) for key, value in data.items()}}, ensure_ascii=False, indent=2))
        return
    result = run_research(data, start, end)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({"output": str(output), "selected": result["selected"], "configs": result["configs"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
