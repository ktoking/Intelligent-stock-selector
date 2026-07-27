#!/usr/bin/env python3
"""Research convergence of OKX equity perpetuals to their underlying US stocks."""
from __future__ import annotations

import json
import math
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import aggregate_bars, load_market_data, weekday_sessions  # noqa: E402

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
SYMBOLS = ("NVDA", "AMD", "MU", "INTC", "TSM", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA", "MSTR", "HOOD")
YF_CACHE = ROOT / "data" / "yfinance_underlying_5m_60d.pkl"
OUTPUT = ROOT / "data" / "okx_basis_research_60d.json"


@dataclass(frozen=True)
class Config:
    z_threshold: float
    residual_bps: float
    holding_bars: int
    stop_bps: float
    target_r: float


def underlying_data() -> dict[str, pd.DataFrame]:
    if YF_CACHE.exists():
        return pd.read_pickle(YF_CACHE)
    result = {}
    for index, symbol in enumerate(SYMBOLS, 1):
        frame = yf.download(symbol, period="60d", interval="5m", auto_adjust=False,
                            prepost=False, progress=False, threads=False)
        if isinstance(frame.columns, pd.MultiIndex):
            frame.columns = frame.columns.get_level_values(0)
        frame = frame.rename(columns=str.lower).dropna(subset=["open", "high", "low", "close"])
        result[symbol] = frame
        print(f"downloaded {index}/{len(SYMBOLS)} {symbol}: {len(frame)}", flush=True)
    pd.to_pickle(result, YF_CACHE)
    return result


def okx_frame(rows: list[list[str]]) -> pd.DataFrame:
    bars = aggregate_bars(rows, 5)
    frame = pd.DataFrame(bars, columns=["stamp", "open", "high", "low", "close", "volume", "v1", "v2", "confirm"])
    frame.index = pd.to_datetime(frame.pop("stamp").astype("int64"), unit="ms", utc=True)
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame[["open", "high", "low", "close", "volume"]]


def bases(okx: dict[str, list[list[str]]], underlying: dict[str, pd.DataFrame]) -> list[dict[str, Any]]:
    result = []
    for symbol in SYMBOLS:
        token = okx_frame(okx[f"{symbol}-USDT-SWAP"])
        stock = underlying[symbol].copy()
        if stock.index.tz is None:
            stock.index = stock.index.tz_localize("UTC")
        else:
            stock.index = stock.index.tz_convert("UTC")
        joined = token.join(stock[["open", "high", "low", "close"]], how="inner", lsuffix="_token", rsuffix="_stock").dropna()
        if joined.empty:
            continue
        local = joined.index.tz_convert(NY)
        joined = joined[(local.hour > 9) | ((local.hour == 9) & (local.minute >= 30))]
        local = joined.index.tz_convert(NY)
        joined = joined[local.hour < 16].copy()
        token_return = np.log(joined["close_token"]).diff()
        stock_return = np.log(joined["close_stock"]).diff()
        joined["residual"] = token_return - stock_return
        history = joined["residual"].shift(1).rolling(24, min_periods=12)
        joined["z"] = (joined["residual"] - history.mean()) / history.std().replace(0, np.nan)
        joined["residual_bps"] = joined["residual"] * 10_000
        joined = joined.reset_index(names="stamp")
        for index in range(len(joined) - 13):
            row = joined.iloc[index]
            z, residual = float(row["z"]), float(row["residual_bps"])
            if not math.isfinite(z) or abs(z) < 1.25 or abs(residual) < 8:
                continue
            entry = joined.iloc[index + 1]
            local_entry = entry["stamp"].tz_convert(NY)
            result.append({
                "symbol": symbol, "inst_id": f"{symbol}-USDT-SWAP",
                "date": local_entry.date().isoformat(), "side": "SHORT" if z > 0 else "LONG",
                "entry_index": index + 1, "entry_time": int(entry["stamp"].timestamp() * 1000),
                "entry_price": float(entry["open_token"]), "z": z, "residual_bps": residual,
                "frame": joined,
            })
        print(f"aligned {symbol}: {len(joined)} bars", flush=True)
    return result


def price(base: dict[str, Any], cfg: Config, cost_bps: float = 14) -> dict[str, Any]:
    frame, start = base["frame"], base["entry_index"]
    entry, direction = base["entry_price"], 1 if base["side"] == "LONG" else -1
    risk = entry * cfg.stop_bps / 10_000
    stop, target = entry - direction * risk, entry + direction * risk * cfg.target_r
    end = min(start + cfg.holding_bars, len(frame) - 1)
    exit_price, reason, exit_index = float(frame.iloc[end]["close_token"]), "time", end
    for index in range(start, end + 1):
        row = frame.iloc[index]
        stop_hit = float(row["low_token"]) <= stop if direction > 0 else float(row["high_token"]) >= stop
        target_hit = float(row["high_token"]) >= target if direction > 0 else float(row["low_token"]) <= target
        if stop_hit:
            exit_price, reason, exit_index = stop, "stop", index
            break
        if target_hit:
            exit_price, reason, exit_index = target, "target", index
            break
    gross_r = (exit_price - entry) * direction / risk
    return {**{key: value for key, value in base.items() if key != "frame"},
            "exit_time": int(frame.iloc[exit_index]["stamp"].timestamp() * 1000) + 300_000,
            "exit_reason": reason, "gross_r": gross_r, "net_r": gross_r - cost_bps / cfg.stop_bps}


def portfolio(rows: list[dict[str, Any]], days: list[str], cfg: Config) -> list[dict[str, Any]]:
    candidates = [price(row, cfg) for row in rows if row["date"] in days
                  and abs(row["z"]) >= cfg.z_threshold and abs(row["residual_bps"]) >= cfg.residual_bps]
    candidates.sort(key=lambda row: (row["entry_time"], -abs(row["z"])))
    active: list[dict[str, Any]] = []
    last_symbol: dict[str, int] = {}
    selected = []
    for row in candidates:
        active = [item for item in active if item["exit_time"] > row["entry_time"]]
        if len(active) >= 5 or any(item["symbol"] == row["symbol"] for item in active):
            continue
        if row["entry_time"] - last_symbol.get(row["symbol"], 0) < 30 * 60_000:
            continue
        active.append(row)
        last_symbol[row["symbol"]] = row["exit_time"]
        selected.append(row)
    return selected


def metric(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(row["net_r"]) for row in rows]
    positive, negative = sum(v for v in values if v > 0), -sum(v for v in values if v <= 0)
    peak = equity = drawdown = 0.0
    for row in sorted(rows, key=lambda item: item["exit_time"]):
        equity += row["net_r"]
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {"trades": len(rows), "wins": sum(v > 0 for v in values),
            "win_rate": round(sum(v > 0 for v in values) / len(values) * 100, 2) if values else 0,
            "net_r": round(sum(values), 3), "expectancy_r": round(sum(values) / len(values), 4) if values else 0,
            "profit_factor": round(positive / negative, 3) if negative else None,
            "max_drawdown_r": round(drawdown, 3)}


def main() -> None:
    end = datetime.now(NY).date() - timedelta(days=1)
    requested = weekday_sessions(end, 60)
    okx = load_market_data(OKX(settings()), tuple(f"{symbol}-USDT-SWAP" for symbol in SYMBOLS), requested)
    base = bases(okx, underlying_data())
    available_days = sorted({row["date"] for row in base})
    if len(available_days) < 55:
        raise RuntimeError(f"only {len(available_days)} aligned sessions")
    days = available_days[-60:]
    train_days, validation_days = days[:20], days[20:30]
    development_days, final_days = days[30:40], days[40:60]
    eligible = []
    for z in (1.25, 1.5, 2.0, 2.5):
        for residual in (8, 12, 20, 30):
            for holding in (3, 6, 12):
                for stop in (40, 60, 100):
                    for target in (1.0, 1.5, 2.0):
                        cfg = Config(z, residual, holding, stop, target)
                        train = metric(portfolio(base, train_days, cfg))
                        validation = metric(portfolio(base, validation_days, cfg))
                        development = metric(portfolio(base, development_days, cfg))
                        if (train["trades"] >= 100 and train["net_r"] > 0 and (train["profit_factor"] or 0) > 1
                                and validation["trades"] >= 40 and validation["net_r"] > 0 and (validation["profit_factor"] or 0) > 1.1
                                and development["trades"] >= 40 and development["net_r"] > 0 and (development["profit_factor"] or 0) > 1.1):
                            eligible.append({"config": asdict(cfg), "train": train,
                                             "validation": validation, "development": development})
    eligible.sort(key=lambda row: (min(row["validation"]["profit_factor"], row["development"]["profit_factor"]),
                                   row["validation"]["net_r"] + row["development"]["net_r"]), reverse=True)
    selected = eligible[0] if eligible else None
    final_rows = portfolio(base, final_days, Config(**selected["config"])) if selected else []
    final = metric(final_rows) if selected else None
    passed = bool(final and final["trades"] >= 100 and final["net_r"] > 0
                  and (final["profit_factor"] or 0) > 1.2 and final["expectancy_r"] > 0.1)
    report = {"generated_at": datetime.now(UTC).isoformat(), "source": "OKX token 5m + yfinance underlying 5m",
              "aligned_sessions": len(days), "base_candidates": len(base),
              "split": {"train": [train_days[0], train_days[-1]], "validation": [validation_days[0], validation_days[-1]],
                        "development": [development_days[0], development_days[-1]],
                        "contaminated_diagnostic": [final_days[0], final_days[-1]]},
              "selected": selected, "final_diagnostic": final, "passed": passed,
              "warning": "date range overlaps prior OKX studies; use only to choose a forward shadow hypothesis",
              "leaderboard": eligible[:20]}
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
