"""Causal hourly research for distinct trend-continuation entry styles.

Yahoo adjusted bars are a proxy, not a Futu Canvas/client backtest.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from research.soxl_regime.active_wave_research import _download


@dataclass(frozen=True)
class Settings:
    style: str
    position: float = 0.8
    cost_bps: float = 10.0


def prepare(hourly: pd.DataFrame, target: pd.DataFrame, reference: pd.DataFrame) -> pd.DataFrame:
    daily = target.add_prefix("t_").join(reference.add_prefix("r_"), how="inner")
    daily["t_ema20"] = daily.t_close.ewm(span=20, adjust=False, min_periods=20).mean()
    daily["t_ema60"] = daily.t_close.ewm(span=60, adjust=False, min_periods=60).mean()
    daily["r_ema20"] = daily.r_close.ewm(span=20, adjust=False, min_periods=20).mean()
    daily["r_ema60"] = daily.r_close.ewm(span=60, adjust=False, min_periods=60).mean()
    daily["broad"] = ((daily.r_close > daily.r_ema20)
                      & (daily.r_ema20 > daily.r_ema60)
                      & (daily.t_close > daily.t_ema20)
                      & (daily.t_ema20 > daily.t_ema60))
    daily["strict"] = daily.broad & (daily.t_ema20 > daily.t_ema20.shift(5))
    h = hourly.copy()
    h["ema10"] = h.close.ewm(span=10, adjust=False, min_periods=10).mean()
    h["ema20"] = h.close.ewm(span=20, adjust=False, min_periods=20).mean()
    h["ema30"] = h.close.ewm(span=30, adjust=False, min_periods=30).mean()
    h["ema60"] = h.close.ewm(span=60, adjust=False, min_periods=60).mean()
    h["prior_high6"] = h.high.shift(1).rolling(6, min_periods=6).max()
    h["day"] = h.index.strftime("%Y-%m-%d")
    previous_day = daily[["broad", "strict"]].shift(1)
    previous_day.index = previous_day.index.strftime("%Y-%m-%d")
    h["broad"] = h.day.map(previous_day.broad).fillna(False).astype(bool)
    h["strict"] = h.day.map(previous_day.strict).fillna(False).astype(bool)
    return h


def simulate(features: pd.DataFrame, settings: Settings, start: str, end: str) -> dict:
    h = features[(features.day >= start) & (features.day <= end)]
    if len(h) < 2:
        return {"return_pct": None, "max_drawdown_pct": None, "trades": 0,
                "win_rate_pct": None, "profit_factor": None}
    cash, qty, basis, entry_price, peak = 100000.0, 0.0, 0.0, 0.0, 0.0
    equity, pnls = [100000.0], []
    last_entry_day = None
    cost = settings.cost_bps / 10000
    for i in range(1, len(h)):
        previous = h.iloc[i - 1]
        current = h.iloc[i]
        exited = False
        if qty:
            peak = max(peak, float(previous.close))
            if settings.style == "reclaim":
                leave = (not current.broad or previous.close < previous.ema20
                         or previous.close < entry_price * .95)
            elif settings.style == "channel":
                leave = (not current.broad or previous.close < previous.ema20
                         or previous.close < peak * .90)
            else:
                leave = (not current.strict or previous.close < peak * .88)
            if leave:
                proceeds = qty * float(current.open) * (1 - cost)
                pnls.append(proceeds - basis)
                cash += proceeds
                qty, basis, exited = 0.0, 0.0, True
        allowed_time = 10 <= h.index[i].hour <= 14
        if settings.style == "reclaim":
            enter = bool(i >= 2 and current.broad and previous.close > previous.ema10
                         and previous.ema10 > previous.ema30
                         and h.iloc[i - 2].close <= h.iloc[i - 2].ema10)
        elif settings.style == "channel":
            enter = bool(current.broad and previous.close > previous.prior_high6
                         and previous.ema20 > previous.ema60)
        else:
            enter = bool(current.strict and previous.close > previous.ema10
                         and previous.ema10 > previous.ema30)
        if (not qty and not exited and allowed_time and enter
                and last_entry_day != current.day and pd.notna(previous.ema60)):
            basis = cash * settings.position
            entry_price = float(current.open) * (1 + cost)
            qty = basis / entry_price
            cash -= basis
            peak = float(previous.close)
            last_entry_day = current.day
        equity.append(cash + qty * float(current.close))
    if qty:
        proceeds = qty * float(h.iloc[-1].close) * (1 - cost)
        pnls.append(proceeds - basis)
        cash += proceeds
        equity[-1] = cash
    curve = pd.Series(equity)
    wins = sum(pnl for pnl in pnls if pnl > 0)
    losses = -sum(pnl for pnl in pnls if pnl < 0)
    return {
        "return_pct": round((cash / 100000 - 1) * 100, 2),
        "max_drawdown_pct": round(float((1 - curve / curve.cummax()).max()) * 100, 2),
        "trades": len(pnls),
        "win_rate_pct": round(100 * sum(pnl > 0 for pnl in pnls) / len(pnls), 2) if pnls else None,
        "profit_factor": round(wins / losses, 2) if losses else None,
    }


def evaluate() -> dict:
    pairs = {"SOXL": "QQQ", "ARMG": "ARM", "SNXX": "SNDK"}
    periods = {"prior": ("2024-09-25", "2025-09-19"),
               "up": ("2026-01-02", "2026-06-30"),
               "down": ("2026-07-01", "2026-09-22")}
    result = {}
    for symbol, reference in pairs.items():
        features = prepare(_download(symbol, "60m"), _download(symbol, "1d"),
                           _download(reference, "1d"))
        result[symbol] = {
            style: {period: simulate(features, Settings(style), *bounds)
                    for period, bounds in periods.items()}
            for style in ("reclaim", "channel", "strict_hold")
        }
    return result


if __name__ == "__main__":
    import json
    print(json.dumps(evaluate(), ensure_ascii=False, indent=2))
