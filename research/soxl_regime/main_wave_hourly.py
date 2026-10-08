"""SOXL main-wave research on hourly bars; completed signals fill next open."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import json

import pandas as pd


@dataclass(frozen=True)
class Config:
    regime: str = "broad"  # broad or strict
    entry: str = "trend"  # trend or breakout
    position: float = 0.8
    trail_pct: float = 0.10
    stop_pct: float = 0.08
    qqq_fast: int = 20
    qqq_slow: int = 60
    soxl_slope_days: int = 0
    cooldown_sessions: int = 0
    max_extension: float = 1.0
    cost_bps: float = 10.0
    early_entry: bool = False
    exit_mode: str = "same"  # same or two_daily_closes; experimental only
    runner_trail_pct: float = 0.0  # Optional wider trail only after sufficient gain.
    runner_trigger_pct: float = 0.20


def load() -> tuple[pd.DataFrame, pd.DataFrame]:
    base = Path("outputs/soxl_regime_switch_v1/cache")
    hourly = pd.read_csv(base / "soxl_hourly.csv", index_col=0)
    hourly.index = pd.to_datetime(hourly.index, utc=True).tz_convert("America/New_York")
    soxl = pd.read_csv(base / "soxl_daily.csv", index_col=0, parse_dates=True)
    qqq = pd.read_csv("outputs/soxl_regime_aggressive_v2/cache/qqq_daily.csv", index_col=0, parse_dates=True)
    daily = soxl.add_prefix("s_").join(qqq.add_prefix("q_"), how="inner")
    if hourly.empty or daily.empty or hourly.index.has_duplicates or daily.index.has_duplicates:
        raise ValueError("invalid cached data")
    return hourly, daily


def features(hourly: pd.DataFrame, daily: pd.DataFrame, config: Config) -> pd.DataFrame:
    d = daily.copy()
    d["q_fast"] = d.q_close.ewm(span=config.qqq_fast, adjust=False).mean()
    d["q_slow"] = d.q_close.ewm(span=config.qqq_slow, adjust=False).mean()
    d["s_ema20"] = d.s_close.ewm(span=20, adjust=False).mean()
    d["s_ema60"] = d.s_close.ewm(span=60, adjust=False).mean()
    d["s_ema10"] = d.s_close.ewm(span=10, adjust=False).mean()
    d["extension"] = d.s_close / d.s_ema20 - 1
    d["broad"] = (d.q_fast > d.q_slow) & (d.q_close > d.q_slow)
    d["strict"] = d.broad & (d.q_close > d.q_fast) & (d.s_close > d.s_ema20)
    if config.soxl_slope_days:
        slope_ok = (d.s_ema20 > d.s_ema20.shift(config.soxl_slope_days)) & (d.s_ema20 > d.s_ema60)
        d["broad"] &= slope_ok
        d["strict"] &= slope_ok
    h = hourly.copy()
    h["ema20"] = h.close.ewm(span=20, adjust=False, min_periods=20).mean()
    h["ema60"] = h.close.ewm(span=60, adjust=False, min_periods=60).mean()
    h["breakout"] = h.high.shift(1).rolling(3).max()
    h["day"] = h.index.strftime("%Y-%m-%d")
    regime = d[config.regime].shift(1)
    if config.early_entry:
        regime = ((d.q_close > d.q_fast) & (d.q_fast > d.q_slow)
                  & (d.s_close > d.s_ema10) & (d.s_ema10 > d.s_ema20)
                  & (d.s_ema20 > d.s_ema20.shift(2))).shift(1)
    regime.index = regime.index.strftime("%Y-%m-%d")
    h["regime"] = h.day.map(regime).fillna(False).astype(bool)
    hold = regime.copy()
    if config.exit_mode == "two_daily_closes":
        below = d.s_close < d.s_ema20
        hold = ((d.q_close > d.q_slow) & ~(below & below.shift(1, fill_value=False))).shift(1)
        hold.index = hold.index.strftime("%Y-%m-%d")
    elif config.exit_mode != "same":
        raise ValueError(f"unsupported exit mode: {config.exit_mode}")
    h["hold_regime"] = h.day.map(hold).fillna(False).astype(bool)
    extension = d.extension.shift(1)
    extension.index = extension.index.strftime("%Y-%m-%d")
    h["extension"] = h.day.map(extension)
    h["ready"] = h.index >= h.index[100]
    return h


def simulate(hourly: pd.DataFrame, daily: pd.DataFrame, config: Config, start: str, end: str):
    h = features(hourly, daily, config)
    h = h[(h.day >= start) & (h.day <= end)]
    if len(h) < 2:
        raise ValueError("empty simulation")
    cost = config.cost_bps / 10000
    cash, qty, entry_cash, entry_price, signal_entry_price, entry_time, peak = 100000.0, 0.0, 0.0, 0.0, 0.0, None, 0.0
    rows, trades = [], []
    entry_day = None
    last_exit_session = None
    sessions = {day: number for number, day in enumerate(h.day.unique())}
    for i in range(1, len(h)):
        now, prev = h.iloc[i], h.iloc[i - 1]
        timestamp = h.index[i]
        exited = False
        if qty:
            peak = max(peak, float(prev.close))
            trail = config.trail_pct
            if config.runner_trail_pct > 0 and peak >= signal_entry_price * (1 + config.runner_trigger_pct):
                trail = config.runner_trail_pct
            reason = None
            if not now.hold_regime:
                reason = "regime"
            elif prev.close < peak * (1 - trail):
                reason = "trail"
            elif prev.close < signal_entry_price * (1 - config.stop_pct):
                reason = "stop"
            if reason:
                fill = float(now.open) * (1 - cost)
                proceeds = qty * fill
                cash += proceeds
                trades.append({"entry": entry_time, "exit": timestamp, "pnl": proceeds - entry_cash,
                               "trade_return_pct": (fill / entry_price - 1) * 100, "reason": reason})
                qty = 0.0
                exited = True
                last_exit_session = sessions[now.day]
        window = timestamp.hour >= 10 and timestamp.hour <= 14
        in_regime = bool(now.regime)
        confirm = (prev.close > prev.ema20 and prev.ema20 > prev.ema60)
        breakout = prev.close > prev.breakout if pd.notna(prev.breakout) else False
        enter = confirm and (config.entry == "trend" or breakout)
        cooled = last_exit_session is None or sessions[now.day] - last_exit_session >= config.cooldown_sessions
        if not qty and not exited and cooled and in_regime and enter and window and entry_day != now.day and prev.ready and now.extension <= config.max_extension:
            fill = float(now.open) * (1 + cost)
            entry_cash = cash * config.position
            qty = entry_cash / fill
            cash -= entry_cash
            entry_price = fill
            signal_entry_price = float(now.open)
            entry_time = timestamp
            entry_day = now.day
            peak = float(prev.close)
        rows.append({"date": timestamp, "equity": cash + qty * float(now.close), "holding": int(qty > 0)})
    equity = pd.DataFrame(rows).set_index("date")
    if qty:
        fill = float(h.iloc[-1].close) * (1 - cost)
        proceeds = qty * fill
        cash += proceeds
        trades.append({"entry": entry_time, "exit": equity.index[-1], "pnl": proceeds - entry_cash,
                       "trade_return_pct": (fill / entry_price - 1) * 100, "reason": "window_end"})
        equity.loc[equity.index[-1], "equity"] = cash
    frame = pd.DataFrame(trades)
    wins = frame.loc[frame.pnl > 0, "pnl"] if len(frame) else pd.Series(dtype=float)
    losses = frame.loc[frame.pnl < 0, "pnl"] if len(frame) else pd.Series(dtype=float)
    metrics = {"return_pct": round((cash / 100000 - 1) * 100, 3),
               "max_drawdown_pct": round(float((1 - equity.equity / equity.equity.cummax()).max()) * 100, 3),
               "trades": len(frame), "win_rate_pct": round(float((frame.pnl > 0).mean()) * 100, 2) if len(frame) else None,
               "profit_factor": round(float(wins.sum() / -losses.sum()), 3) if len(losses) else None,
               "holding_bars": int(equity.holding.sum())}
    return equity, frame, metrics


def research(output: Path) -> dict:
    hourly, daily = load()
    windows = {"prior_year": ("2024-09-25", "2025-09-19"),
               "recent_year": ("2025-09-22", "2026-09-18"),
               "user_up": ("2026-01-02", "2026-06-30"),
               "user_down": ("2026-06-22", "2026-09-18"),
               "full": ("2024-09-25", "2026-09-18")}
    configs = [Config(regime=r, entry=e, trail_pct=t, soxl_slope_days=s, cooldown_sessions=c)
               for r in ("broad", "strict") for e in ("trend", "breakout")
               for t in (0.08, 0.12) for s in (0, 5) for c in (0, 5)]
    rows = []
    for c in configs:
        rows.append({"config": asdict(c), **{name: simulate(hourly, daily, c, *dates)[2] for name, dates in windows.items()}})
    eligible = [r for r in rows if r["prior_year"]["trades"] >= 3]
    selected = max(eligible, key=lambda r: r["prior_year"]["return_pct"] - r["prior_year"]["max_drawdown_pct"] / 2)
    config = Config(**selected["config"])
    stress = {str(bps): {w: simulate(hourly, daily, Config(**{**asdict(config), "cost_bps": bps}), *windows[w])[2]
                         for w in ("prior_year", "recent_year", "user_up", "full")}
              for bps in (10, 20, 40)}
    equity, trades, _ = simulate(hourly, daily, config, *windows["full"])
    output.mkdir(parents=True, exist_ok=True)
    equity.to_csv(output / "equity.csv")
    trades.to_csv(output / "trades.csv", index=False)
    result = {"data_start": str(hourly.index[0]), "data_end": str(hourly.index[-1]),
              "selected": selected, "cost_stress": stress, "candidate_results": rows}
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def delivery_evidence(output: Path) -> dict:
    """Recompute the fixed file parameters and disclose their selection limits."""
    hourly, daily = load()
    config = Config(regime="strict", entry="trend", position=0.8, trail_pct=0.12,
                    stop_pct=0.08, soxl_slope_days=5, cooldown_sessions=7,
                    max_extension=0.25, cost_bps=10.0)
    windows = {"prior_year": ("2024-09-25", "2025-09-19"),
               "recent_year": ("2025-09-22", "2026-09-18"),
               "user_up": ("2026-01-02", "2026-06-30"),
               "user_down": ("2026-06-22", "2026-09-18"),
               "full": ("2024-09-25", "2026-09-18")}
    results = {name: simulate(hourly, daily, config, *dates)[2] for name, dates in windows.items()}
    stress = {str(bps): {name: simulate(hourly, daily, Config(**{**asdict(config), "cost_bps": bps}), *dates)[2]
                         for name, dates in windows.items()}
              for bps in (20.0, 40.0)}
    near = pd.read_csv("outputs/soxl_regime_switch_v1/cache/soxl_five_minute_recent.csv", index_col=0)
    near.index = pd.to_datetime(near.index, utc=True).tz_convert("America/New_York")
    recent_5m = simulate(near, daily, config, "2026-07-27", "2026-09-18")[2]
    equity, trades, _ = simulate(hourly, daily, config, *windows["full"])
    output.mkdir(parents=True, exist_ok=True)
    equity.to_csv(output / "delivery_equity.csv")
    trades.to_csv(output / "delivery_trades.csv", index=False)
    result = {"status": "exploratory_offline_proxy_only", "config": asdict(config),
              "data": {"source": "Yahoo cached adjusted bars", "hourly_start": str(hourly.index.min()),
                       "hourly_end": str(hourly.index.max()), "daily_end": str(daily.index.max()),
                       "recent_5m_start": str(near.index.min()), "recent_5m_end": str(near.index.max())},
              "windows": results, "cost_stress_bps_per_side": stress, "recent_5m": recent_5m,
              "limitations": ["Parameters were inspected across all reported windows; none is a clean untouched out-of-sample result.",
                              "Hourly fills are a proxy for the 5-minute Futu trigger and 60-minute signal.",
                              "The down-window result is negative and the 5-minute check has no trades.",
                              "Native Futu import and Futu backtest were not run; user requested no desktop operation or OpenD."]}
    (output / "delivery_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    result = delivery_evidence(Path("outputs/soxl_main_wave_hourly"))
    print(json.dumps(result, ensure_ascii=False, indent=2))
