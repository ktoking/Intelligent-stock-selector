from __future__ import annotations

from dataclasses import dataclass, asdict
import math

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class StrategyConfig:
    trend_position: float = 0.40
    range_position: float = 0.25
    panic_position: float = 0.20
    trend_boll_std: float = 1.5
    trend_stop: float = 0.10
    range_rsi: float = 40
    range_boll_std: float = 1.75
    range_tp: float = 0.025
    range_sl: float = 0.02
    panic_rsi: float = 35
    panic_boll_std: float = 1.75
    panic_drop: float = 0.06
    panic_tp: float = 0.015
    panic_sl: float = 0.03


def metrics(equity: pd.DataFrame, trades: pd.DataFrame, initial: float = 100_000.0) -> dict:
    curve = equity["equity"].astype(float) if len(equity) else pd.Series([initial])
    ret = curve.pct_change().dropna()
    dd = 1 - curve / curve.cummax()
    pnls = trades["pnl"].astype(float) if len(trades) else pd.Series(dtype=float)
    wins, losses = pnls[pnls > 0], pnls[pnls < 0]
    years = max((equity.index[-1] - equity.index[0]).total_seconds() / (365.25 * 86400), 1 / 252) if len(equity) > 1 else 1 / 252
    total = curve.iloc[-1] / initial - 1
    sharpe = math.sqrt(252 * 7) * ret.mean() / ret.std() if len(ret) > 2 and ret.std() > 0 else None
    downside = ret[ret < 0].std()
    sortino = math.sqrt(252 * 7) * ret.mean() / downside if downside and downside > 0 else None
    max_dd = float(dd.max()) if len(dd) else 0.0
    return {
        "return_pct": round(total * 100, 4), "cagr_pct": round(((1 + total) ** (1 / years) - 1) * 100, 4),
        "max_drawdown_pct": round(max_dd * 100, 4), "profit_factor": round(wins.sum() / abs(losses.sum()), 4) if len(losses) else None,
        "win_rate_pct": round((pnls > 0).mean() * 100, 2) if len(pnls) else None,
        "trades": int(len(trades)), "sharpe": round(float(sharpe), 4) if sharpe is not None else None,
        "sortino": round(float(sortino), 4) if sortino is not None else None,
        "calmar": round((((1 + total) ** (1 / years) - 1) / max_dd), 4) if max_dd > 0 else None,
        "worst_trade_pct": round(float(trades["return_pct"].min()), 4) if len(trades) else None,
        "top1_contribution_pct": round(float(pnls.nlargest(1).sum() / pnls.sum() * 100), 2) if len(pnls) and pnls.sum() > 0 else None,
        "top3_contribution_pct": round(float(pnls.nlargest(3).sum() / pnls.sum() * 100), 2) if len(pnls) and pnls.sum() > 0 else None,
        "top5_contribution_pct": round(float(pnls.nlargest(5).sum() / pnls.sum() * 100), 2) if len(pnls) and pnls.sum() > 0 else None,
    }


def simulate(frame: pd.DataFrame, regime: pd.Series, daily: pd.DataFrame, config: StrategyConfig, *, cost_bps: float = 10.0) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    data = frame.copy()
    data["regime"] = regime.reindex(data.index).ffill().fillna("TRANSITION")
    dates = data.index.strftime("%Y-%m-%d")
    daily_known = daily.shift(1).copy()
    daily_known.index = daily_known.index.strftime("%Y-%m-%d")
    for column in ["close", "ema20", "ema60", "boll_upper", "boll_lower", "boll_mid"]:
        data[f"daily_{column}"] = pd.Series(dates, index=data.index).map(daily_known[column])
    cost = cost_bps / 10_000
    cash, qty, entry_price, entry_value = 100_000.0, 0, 0.0, 0.0
    mode, entry_time = "NONE", None
    pending: tuple[str, float] | None = None
    trades, curve = [], []
    for i in range(1, len(data)):
        row, prev = data.iloc[i], data.iloc[i - 1]
        ts = data.index[i]
        if pending and qty == 0:
            mode, ratio = pending
            price = float(row.open) * (1 + cost)
            qty = int(cash * ratio / price)
            if qty > 0:
                entry_value = qty * price; cash -= entry_value; entry_price = price; entry_time = ts
            else: mode = "NONE"
            pending = None
        exit_reason = None
        if qty > 0:
            close_return = float(prev.close) / entry_price - 1
            if mode == "TREND" and (prev.daily_close < prev.daily_ema20 or prev.daily_ema20 < prev.daily_ema60 or close_return <= -config.trend_stop):
                exit_reason = "trend_exit"
            elif mode == "RANGE" and (prev.rsi14 >= 55 or prev.close >= prev.boll_mid or close_return >= config.range_tp or close_return <= -config.range_sl or bool(prev.is_last_bar)):
                exit_reason = "range_exit"
            elif mode == "DOWN_PANIC" and (close_return >= config.panic_tp or close_return <= -config.panic_sl or bool(prev.is_last_bar)):
                exit_reason = "panic_exit"
            if exit_reason:
                price = float(row.open if not bool(prev.is_last_bar) else prev.close) * (1 - cost)
                proceeds = qty * price; pnl = proceeds - entry_value; cash += proceeds
                trades.append({"mode": mode, "entry_time": entry_time, "exit_time": ts, "entry_price": entry_price, "exit_price": price, "qty": qty, "pnl": pnl, "return_pct": (price / entry_price - 1) * 100, "exit_reason": exit_reason})
                qty, mode, entry_price, entry_value = 0, "NONE", 0.0, 0.0
        if qty == 0 and pending is None and not bool(prev.is_last_bar):
            reversal = prev.close > data.iloc[i - 2].close if i >= 2 else False
            if prev.regime == "TREND_UP" and prev.daily_close > prev.daily_boll_upper:
                pending = ("TREND", config.trend_position)
            elif prev.regime == "RANGE" and prev.rsi14 < config.range_rsi and prev.close < prev.boll_lower and reversal:
                pending = ("RANGE", config.range_position)
            elif prev.regime == "TREND_DOWN" and prev.rsi14 < config.panic_rsi and prev.close < prev.boll_lower and prev.day_drop >= config.panic_drop and reversal:
                pending = ("DOWN_PANIC", config.panic_position)
        curve.append({"timestamp": ts, "equity": cash + qty * float(row.close), "mode": mode})
    if qty > 0:
        price = float(data.iloc[-1].close) * (1 - cost); proceeds = qty * price; pnl = proceeds - entry_value; cash += proceeds
        trades.append({"mode": mode, "entry_time": entry_time, "exit_time": data.index[-1], "entry_price": entry_price, "exit_price": price, "qty": qty, "pnl": pnl, "return_pct": (price / entry_price - 1) * 100, "exit_reason": "window_end"})
        if curve: curve[-1]["equity"] = cash
    equity = pd.DataFrame(curve).set_index("timestamp") if curve else pd.DataFrame(columns=["equity", "mode"])
    trade_frame = pd.DataFrame(trades)
    result = metrics(equity, trade_frame)
    result["contribution_by_mode"] = trade_frame.groupby("mode")["pnl"].sum().round(2).to_dict() if len(trade_frame) else {}
    result["config"] = asdict(config)
    return equity, trade_frame, result


def buy_hold(frame: pd.DataFrame, cost_bps: float = 10.0) -> dict:
    cost = cost_bps / 10_000
    start = float(frame.iloc[0].open) * (1 + cost); end = float(frame.iloc[-1].close) * (1 - cost)
    curve = frame.close / start * 100_000
    dd = 1 - curve / curve.cummax()
    return {"return_pct": round((end / start - 1) * 100, 4), "max_drawdown_pct": round(float(dd.max()) * 100, 4)}
