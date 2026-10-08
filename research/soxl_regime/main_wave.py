"""Daily SOXL main-wave research with completed-close signals and next-open fills."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import json

import pandas as pd


@dataclass(frozen=True)
class Config:
    breakout_days: int = 20
    exit_ema_days: int = 10
    qqq_fast_days: int = 20
    qqq_slow_days: int = 60
    position: float = 0.80
    trail_pct: float = 0.12
    hard_stop_pct: float = 0.09
    cost_bps: float = 10.0


def load(cache: Path) -> pd.DataFrame:
    soxl = pd.read_csv(cache / "soxl_daily.csv", index_col=0, parse_dates=True)
    qqq = pd.read_csv(Path("outputs/soxl_regime_aggressive_v2/cache/qqq_daily.csv"), index_col=0, parse_dates=True)
    soxl = soxl.add_prefix("s_")
    qqq = qqq.add_prefix("q_")
    data = soxl.join(qqq, how="inner").sort_index()
    if data.empty or data.index.has_duplicates or (data[["s_open", "s_close", "q_close"]] <= 0).any().any():
        raise ValueError("daily price data invalid")
    return data


def indicators(data: pd.DataFrame, config: Config) -> pd.DataFrame:
    f = data.copy()
    f["q_fast"] = f.q_close.ewm(span=config.qqq_fast_days, adjust=False).mean()
    f["q_slow"] = f.q_close.ewm(span=config.qqq_slow_days, adjust=False).mean()
    f["s_ema20"] = f.s_close.ewm(span=20, adjust=False).mean()
    f["s_exit_ema"] = f.s_close.ewm(span=config.exit_ema_days, adjust=False).mean()
    f["breakout"] = f.s_high.shift(1).rolling(config.breakout_days).max()
    f["regime_up"] = (f.q_fast > f.q_slow) & (f.q_close > f.q_fast) & (f.s_close > f.s_ema20)
    f["entry"] = f.regime_up & (f.s_close > f.breakout)
    f["ready"] = f.index >= f.index[100]
    return f


def simulate(data: pd.DataFrame, config: Config, start: str, end: str) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    f = indicators(data, config)
    begin, finish = pd.Timestamp(start), pd.Timestamp(end)
    indices = [i for i in range(1, len(f)) if begin <= f.index[i] <= finish]
    if not indices:
        raise ValueError("empty backtest window")
    cost = config.cost_bps / 10000
    cash, qty, entry_fill, entry_time, peak, entry_cash = 100000.0, 0.0, 0.0, None, 0.0, 0.0
    equity_rows, trade_rows = [], []
    for i in indices:
        today, prev = f.iloc[i], f.iloc[i - 1]
        timestamp = f.index[i]
        if qty:
            reason = None
            if not prev.regime_up:
                reason = "regime_break"
            elif prev.s_close < prev.s_exit_ema:
                reason = "ema_exit"
            elif prev.s_close <= peak * (1 - config.trail_pct):
                reason = "trail_exit"
            elif prev.s_close <= entry_fill * (1 - config.hard_stop_pct):
                reason = "hard_stop"
            if reason:
                exit_fill = float(today.s_open) * (1 - cost)
                proceeds = qty * exit_fill
                cash += proceeds
                trade_rows.append({"entry": entry_time, "exit": timestamp, "entry_price": entry_fill,
                                   "exit_price": exit_fill, "pnl": proceeds - entry_cash,
                                   "trade_return_pct": (exit_fill / entry_fill - 1) * 100, "reason": reason})
                qty = 0.0
                peak = 0.0
        if not qty and prev.ready and prev.entry:
            entry_fill = float(today.s_open) * (1 + cost)
            entry_cash = cash * config.position
            qty = entry_cash / entry_fill
            cash -= entry_cash
            entry_time = timestamp
            peak = float(prev.s_close)
        if qty:
            peak = max(peak, float(prev.s_close))
        equity_rows.append({"date": timestamp, "equity": cash + qty * float(today.s_close), "holding": int(qty > 0)})

    equity = pd.DataFrame(equity_rows).set_index("date")
    trades = pd.DataFrame(trade_rows)
    if qty:
        last = f.loc[equity.index[-1]]
        exit_fill = float(last.s_close) * (1 - cost)
        proceeds = qty * exit_fill
        cash += proceeds
        trades = pd.concat([trades, pd.DataFrame([{"entry": entry_time, "exit": equity.index[-1],
            "entry_price": entry_fill, "exit_price": exit_fill, "pnl": proceeds - entry_cash,
            "trade_return_pct": (exit_fill / entry_fill - 1) * 100, "reason": "window_end"}])], ignore_index=True)
        equity.loc[equity.index[-1], "equity"] = cash
    dd = 1 - equity.equity / equity.equity.cummax()
    winners = trades.loc[trades.pnl > 0, "pnl"] if not trades.empty else pd.Series(dtype=float)
    losers = trades.loc[trades.pnl < 0, "pnl"] if not trades.empty else pd.Series(dtype=float)
    metrics = {"return_pct": round((cash / 100000 - 1) * 100, 3),
               "max_drawdown_pct": round(float(dd.max()) * 100, 3),
               "trades": len(trades), "win_rate_pct": round(float((trades.pnl > 0).mean()) * 100, 2) if len(trades) else None,
               "profit_factor": round(float(winners.sum() / -losers.sum()), 3) if len(losers) else None,
               "holding_days": int(equity.holding.sum())}
    return equity, trades, metrics


def research(output: Path) -> dict:
    data = load(Path("outputs/soxl_regime_switch_v1/cache"))
    windows = {"full": ("2024-09-25", "2026-09-18"),
               "prior_year": ("2024-09-25", "2025-09-19"),
               "recent_year": ("2025-09-22", "2026-09-18"),
               "user_up": ("2026-01-02", "2026-06-30"),
               "user_down": ("2026-06-22", "2026-09-18")}
    candidates = [Config(breakout_days=days, exit_ema_days=exit_days, trail_pct=trail)
                  for days in (10, 20, 40) for exit_days in (10, 20) for trail in (0.10, 0.15)]
    rows = []
    for c in candidates:
        results = {name: simulate(data, c, *dates)[2] for name, dates in windows.items()}
        rows.append({"config": asdict(c), **results})
    # Preselect on the earlier year, then evaluate the subsequent year without retuning.
    eligible = [r for r in rows if r["prior_year"]["trades"] >= 2]
    selected = max(eligible, key=lambda r: (r["prior_year"]["return_pct"] - 0.5 * r["prior_year"]["max_drawdown_pct"]))
    config = Config(**selected["config"])
    stress = {}
    for bps in (10, 20, 40):
        c = Config(**{**asdict(config), "cost_bps": float(bps)})
        stress[str(bps)] = {name: simulate(data, c, *windows[name])[2] for name in ("prior_year", "recent_year", "user_up", "full")}
    output.mkdir(parents=True, exist_ok=True)
    equity, trades, _ = simulate(data, config, *windows["full"])
    equity.to_csv(output / "equity.csv")
    trades.to_csv(output / "trades.csv", index=False)
    result = {"data_start": str(data.index.min().date()), "data_end": str(data.index.max().date()),
              "selection": "12 fixed candidate rules selected on prior_year return minus half drawdown; recent_year is untouched holdout",
              "selected": selected, "cost_stress": stress, "candidate_results": rows}
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    result = research(Path("outputs/soxl_main_wave"))
    print(json.dumps({k: v for k, v in result.items() if k != "candidate_results"}, ensure_ascii=False, indent=2))
