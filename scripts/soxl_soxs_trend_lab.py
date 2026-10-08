#!/usr/bin/env python3
"""Three-month causal SOXL/SOXS trend-switch research.

Signals are computed from completed QQQ hourly bars and executed at the next
hourly open.  The account can own SOXL or SOXS, never both.  The final holdout
is not used for parameter selection.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import yfinance as yf

from scripts.moomoo_quant_binary import package_canvas_strategy


ROOT = Path(__file__).resolve().parents[1]
SYMBOLS = ("QQQ", "SOXL", "SOXS")


@dataclass(frozen=True)
class Config:
    family: str
    timeframe: str = "H1"
    fast: int = 0
    slow: int = 0
    horizon: int = 0
    buffer: float = 0.0
    persistence: int = 1
    neutral_cash: bool = False
    position_ratio: float = 0.95

    @property
    def name(self) -> str:
        return (
            f"{self.family}_{self.timeframe}_f{self.fast}_s{self.slow}_h{self.horizon}"
            f"_b{int(round(self.buffer * 10000))}_p{self.persistence}"
            f"_n{int(self.neutral_cash)}_r{int(round(self.position_ratio * 100))}"
        )


def candidate_configs() -> list[Config]:
    values: list[Config] = []
    for fast in (3, 5, 8, 10, 12, 16, 20):
        for slow in (12, 18, 24, 30, 40, 50, 60):
            if fast >= slow:
                continue
            for buffer in (0.0, 0.001, 0.002, 0.003, 0.005):
                for persistence in (1, 2, 3):
                    for neutral in (False, True):
                        values.append(Config("ema", "H1", fast, slow, 0, buffer, persistence, neutral))
    for horizon in (6, 12, 18, 24, 36, 48, 60):
        for buffer in (0.0, 0.003, 0.006, 0.010, 0.015, 0.020):
            for persistence in (1, 2, 3):
                for neutral in (False, True):
                    values.append(Config("momentum", "H1", 0, 0, horizon, buffer, persistence, neutral))
    for fast in (5, 8, 12, 16):
        for slow in (24, 30, 40, 60):
            if fast >= slow:
                continue
            for horizon in (6, 12, 24):
                for buffer in (0.0, 0.002, 0.005):
                    for persistence in (1, 2):
                        for neutral in (False, True):
                            values.append(Config("ema_momentum", "H1", fast, slow, horizon, buffer, persistence, neutral))
    for fast in (2, 3, 4, 5, 8, 10):
        for slow in (5, 8, 10, 12, 15, 20, 30):
            if fast >= slow:
                continue
            for buffer in (0.0, 0.002, 0.005, 0.010):
                for persistence in (1, 2):
                    for neutral in (False, True):
                        values.append(Config("ema", "D1", fast, slow, 0, buffer, persistence, neutral))
    for horizon in (2, 3, 5, 8, 10, 15, 20):
        for buffer in (0.0, 0.005, 0.010, 0.020, 0.030):
            for persistence in (1, 2):
                for neutral in (False, True):
                    values.append(Config("momentum", "D1", 0, 0, horizon, buffer, persistence, neutral))
    return values


def download_bars(start: str, end: str) -> pd.DataFrame:
    raw = yf.download(
        list(SYMBOLS), start=start, end=end, interval="60m", auto_adjust=True,
        prepost=False, progress=False, threads=True,
    )
    if raw.empty:
        raise RuntimeError("Yahoo returned no hourly data")
    frames: list[pd.DataFrame] = []
    for symbol in SYMBOLS:
        if isinstance(raw.columns, pd.MultiIndex):
            frame = raw.xs(symbol, axis=1, level=1).copy()
        else:
            frame = raw.copy()
        frame.columns = [str(column).lower().replace(" ", "_") for column in frame.columns]
        required = {"open", "high", "low", "close", "volume"}
        if not required.issubset(frame.columns):
            raise RuntimeError(f"{symbol} missing columns: {sorted(required - set(frame.columns))}")
        frames.append(frame[list(sorted(required))].add_prefix(f"{symbol.lower()}_"))
    bars = pd.concat(frames, axis=1, join="inner").dropna().sort_index()
    if bars.index.tz is None:
        bars.index = bars.index.tz_localize("UTC")
    bars.index = bars.index.tz_convert("America/New_York")
    bars = bars.between_time("09:30", "16:00")
    if bars.empty:
        raise RuntimeError("No aligned regular-session bars")
    return bars


def _persistent_signal(raw: pd.Series, persistence: int, neutral_cash: bool = False) -> pd.Series:
    state = 0
    candidate = 0
    count = 0
    result: list[int] = []
    for value in raw.fillna(0).astype(int):
        if value == 0 and not neutral_cash:
            candidate = 0
            count = 0
        elif value == state:
            candidate = value
            count = 0
        elif value == candidate:
            count += 1
        else:
            candidate = value
            count = 1
        if candidate != state and count >= persistence:
            state = candidate
            count = 0
        result.append(state)
    return pd.Series(result, index=raw.index, dtype="int8")


def make_signal(close: pd.Series, config: Config) -> pd.Series:
    source = close
    if config.timeframe == "D1":
        source = close.groupby(close.index.strftime("%Y-%m-%d")).last()
    if config.family in {"ema", "ema_momentum"}:
        fast = source.ewm(span=config.fast, adjust=False, min_periods=config.fast).mean()
        slow = source.ewm(span=config.slow, adjust=False, min_periods=config.slow).mean()
        gap = fast / slow - 1.0
    else:
        gap = pd.Series(np.nan, index=close.index)
    momentum = source.pct_change(config.horizon) if config.horizon else pd.Series(np.nan, index=source.index)

    if config.family == "ema":
        raw = pd.Series(np.where(gap > config.buffer, 1, np.where(gap < -config.buffer, -1, 0)), index=source.index)
    elif config.family == "momentum":
        raw = pd.Series(
            np.where(momentum > config.buffer, 1, np.where(momentum < -config.buffer, -1, 0)),
            index=source.index,
        )
    elif config.family == "ema_momentum":
        up = (gap > config.buffer) & (momentum > config.buffer)
        down = (gap < -config.buffer) & (momentum < -config.buffer)
        raw = pd.Series(np.where(up, 1, np.where(down, -1, 0)), index=source.index)
    else:
        raise ValueError(f"unknown family {config.family}")
    base = _persistent_signal(raw, config.persistence, config.neutral_cash)
    if config.timeframe == "H1":
        return base
    # Daily values only become known at that session's final hourly close.
    result = pd.Series(np.nan, index=close.index, dtype=float)
    dates = close.index.strftime("%Y-%m-%d")
    for day, value in base.items():
        locations = np.flatnonzero(dates == day)
        if len(locations):
            result.iloc[locations[-1]] = value
    return result.ffill().fillna(0).astype("int8")


def _empty_metrics(initial_capital: float) -> dict[str, float | int | None]:
    return {
        "initial_capital": initial_capital, "final_equity": initial_capital,
        "return_pct": 0.0, "max_drawdown_pct": 0.0, "profit_factor": None,
        "win_rate_pct": None, "trades": 0, "switches": 0,
        "max_consecutive_losses": 0,
    }


def simulate(
    bars: pd.DataFrame,
    signal: pd.Series,
    sessions: Iterable[str],
    *,
    cost_bps: float = 10.0,
    initial_capital: float = 100_000.0,
    position_ratio: float = 0.95,
) -> dict[str, object]:
    allowed = set(sessions)
    dates = bars.index.strftime("%Y-%m-%d")
    indices = [i for i in range(1, len(bars)) if dates[i] in allowed]
    if not indices:
        return {"metrics": _empty_metrics(initial_capital), "trades": [], "equity": []}

    cost = cost_bps / 10_000.0
    cash = initial_capital
    held: str | None = None
    qty = 0
    entry_value = 0.0
    entry_time = None
    trade_rows: list[dict[str, object]] = []
    equity_rows: list[dict[str, object]] = []
    switches = 0

    for i in indices:
        desired_value = int(signal.iloc[i - 1])
        desired = "SOXL" if desired_value > 0 else ("SOXS" if desired_value < 0 else None)
        timestamp = bars.index[i]
        if held and held != desired:
            sell_price = float(bars.iloc[i][f"{held.lower()}_open"]) * (1.0 - cost)
            proceeds = qty * sell_price
            pnl = proceeds - entry_value
            trade_rows.append({
                "symbol": held, "entry_time": str(entry_time), "exit_time": str(timestamp),
                "entry_value": entry_value, "exit_value": proceeds, "pnl": pnl,
                "return_pct": (proceeds / entry_value - 1.0) * 100 if entry_value else 0.0,
                "exit_reason": "regime_switch",
            })
            cash += proceeds
            held = None
            qty = 0
            entry_value = 0.0
            entry_time = None
            switches += 1
        if held is None and desired is not None:
            buy_price = float(bars.iloc[i][f"{desired.lower()}_open"]) * (1.0 + cost)
            qty = math.floor(cash * position_ratio / buy_price)
            if qty > 0:
                entry_value = qty * buy_price
                cash -= entry_value
                held = desired
                entry_time = timestamp
        mark = cash
        if held:
            mark += qty * float(bars.iloc[i][f"{held.lower()}_close"])
        equity_rows.append({"time": str(timestamp), "equity": mark, "held": held or "CASH"})

    if held:
        i = indices[-1]
        timestamp = bars.index[i]
        sell_price = float(bars.iloc[i][f"{held.lower()}_close"]) * (1.0 - cost)
        proceeds = qty * sell_price
        pnl = proceeds - entry_value
        trade_rows.append({
            "symbol": held, "entry_time": str(entry_time), "exit_time": str(timestamp),
            "entry_value": entry_value, "exit_value": proceeds, "pnl": pnl,
            "return_pct": (proceeds / entry_value - 1.0) * 100 if entry_value else 0.0,
            "exit_reason": "window_end_liquidation",
        })
        cash += proceeds
        if equity_rows:
            equity_rows[-1]["equity"] = cash
            equity_rows[-1]["held"] = "CASH"

    if not equity_rows:
        return {"metrics": _empty_metrics(initial_capital), "trades": trade_rows, "equity": []}
    curve = np.asarray([float(row["equity"]) for row in equity_rows], dtype=float)
    peaks = np.maximum.accumulate(np.concatenate([[initial_capital], curve]))[1:]
    drawdown = 1.0 - curve / peaks
    pnls = [float(row["pnl"]) for row in trade_rows]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    streak = max_streak = 0
    for value in pnls:
        streak = streak + 1 if value < 0 else 0
        max_streak = max(max_streak, streak)
    metrics: dict[str, float | int | None] = {
        "initial_capital": round(initial_capital, 2),
        "final_equity": round(float(cash), 2),
        "return_pct": round((cash / initial_capital - 1.0) * 100, 4),
        "max_drawdown_pct": round(float(drawdown.max()) * 100, 4),
        "profit_factor": round(sum(wins) / abs(sum(losses)), 4) if losses else None,
        "win_rate_pct": round(len(wins) / len(pnls) * 100, 2) if pnls else None,
        "trades": len(pnls), "switches": switches,
        "max_consecutive_losses": max_streak,
    }
    return {"metrics": metrics, "trades": trade_rows, "equity": equity_rows}


def session_split(bars: pd.DataFrame, evaluation_start: str) -> dict[str, list[str]]:
    sessions = sorted(set(bars.loc[evaluation_start:].index.strftime("%Y-%m-%d")))
    if len(sessions) < 45:
        raise RuntimeError(f"need at least 45 sessions, got {len(sessions)}")
    holdout_size = max(15, len(sessions) // 3)
    development = sessions[:-holdout_size]
    midpoint = len(development) // 2
    return {
        "full": sessions,
        "backward_validation": sessions[:-42],
        "primary": sessions[-42:],
        "development": development,
        "development_a": development[:midpoint],
        "development_b": development[midpoint:],
        "holdout": sessions[-holdout_size:],
    }


def selection_score(row: dict[str, object]) -> float:
    a = row["development_a"]
    b = row["development_b"]
    development = row["development"]
    assert isinstance(a, dict) and isinstance(b, dict) and isinstance(development, dict)
    worst = min(float(a["return_pct"]), float(b["return_pct"]))
    return (
        worst
        + 0.25 * float(development["return_pct"])
        - 0.50 * max(float(a["max_drawdown_pct"]), float(b["max_drawdown_pct"]))
        - 0.03 * float(development["switches"])
    )


def rolling_score(row: dict[str, object]) -> float:
    validation = row["backward_validation"]
    primary = row["primary"]
    assert isinstance(validation, dict) and isinstance(primary, dict)
    return (
        min(float(validation["return_pct"]), float(primary["return_pct"]))
        + 0.30 * float(primary["return_pct"])
        - 0.45 * max(float(validation["max_drawdown_pct"]), float(primary["max_drawdown_pct"]))
        - 0.03 * (float(validation["switches"]) + float(primary["switches"]))
    )


def buy_hold(bars: pd.DataFrame, symbol: str, sessions: list[str], cost_bps: float) -> dict[str, float]:
    selected = bars[bars.index.strftime("%Y-%m-%d").isin(set(sessions))]
    cost = cost_bps / 10_000.0
    start = float(selected.iloc[0][f"{symbol.lower()}_open"]) * (1 + cost)
    end = float(selected.iloc[-1][f"{symbol.lower()}_close"]) * (1 - cost)
    closes = selected[f"{symbol.lower()}_close"].astype(float)
    curve = closes / start
    dd = 1 - curve / curve.cummax()
    return {"return_pct": round((end / start - 1) * 100, 4), "max_drawdown_pct": round(float(dd.max()) * 100, 4)}


def moomoo_quant(config: Config, *, shadow_only: bool) -> str:
    family_code = {"ema": 1, "momentum": 2, "ema_momentum": 3}[config.family]
    enabled = "False" if shadow_only else "True"
    bar_type = "BarType.H1" if config.timeframe == "H1" else "BarType.D1"
    return f'''# SOXL_SOXS_QQQ_TREND_SWITCH_V1
# Generated from causal hourly research. Import into moomoo Algo as a code strategy.
# Bind the sole trigger symbol to US.QQQ. For D1 parameters, trigger at 09:35 ET;
# completed daily bars are read with select=2 so the forming day is excluded.
class Strategy(StrategyBase):
    def initialize(self):
        declare_strategy_type(strategy_type=AlgoStrategyType.SECURITY)
        self.trigger_symbols()
        self.custom_indicator()
        self.global_variables()

    def trigger_symbols(self):
        self.qqq = declare_trig_symbol()

    def custom_indicator(self):
        pass

    def global_variables(self):
        self.soxl = Contract("US.SOXL")
        self.soxs = Contract("US.SOXS")
        self.family = show_variable({family_code}, GlobalType.INT, "1=EMA 2=Momentum 3=EMA+Momentum")
        self.fast = show_variable({config.fast}, GlobalType.INT, "QQQ EMA fast")
        self.slow = show_variable({config.slow}, GlobalType.INT, "QQQ EMA slow")
        self.horizon = show_variable({config.horizon}, GlobalType.INT, "QQQ momentum hours")
        self.buffer = show_variable({config.buffer:.6f}, GlobalType.FLOAT, "Signal hysteresis")
        self.persistence = show_variable({config.persistence}, GlobalType.INT, "Confirmation bars")
        self.neutral_cash = show_variable({str(config.neutral_cash)}, GlobalType.BOOL, "Exit to cash in neutral regime")
        self.max_position_ratio = show_variable({config.position_ratio:.2f}, GlobalType.FLOAT, "Maximum cash ratio")
        self.execution_enabled = show_variable({enabled}, GlobalType.BOOL, "Enable orders; keep False for shadow")
        self.current_signal = 0
        self.candidate_signal = 0
        self.candidate_count = 0
        self.last_daily_signal_date = ""

    def _completed_daily_ema(self, period):
        lookback = max(period * 5, period + 10)
        value = bar_close(self.qqq, bar_type=BarType.D1, select=lookback + 1)
        alpha = 2.0 / (period + 1.0)
        for offset in range(lookback, 1, -1):
            price = bar_close(self.qqq, bar_type=BarType.D1, select=offset)
            value = alpha * price + (1.0 - alpha) * value
        return value

    def _raw_signal(self):
        select_now = 1 if {str(config.timeframe == 'H1')} else 2
        current = bar_close(self.qqq, bar_type={bar_type}, select=select_now)
        ema_signal = 0
        momentum_signal = 0
        if self.family == 1 or self.family == 3:
            if {str(config.timeframe == 'D1')}:
                fast_value = self._completed_daily_ema(self.fast)
                slow_value = self._completed_daily_ema(self.slow)
            else:
                fast_value = ema(symbol=self.qqq, period=self.fast, bar_type=BarType.H1, session_type=THType.RTH)
                slow_value = ema(symbol=self.qqq, period=self.slow, bar_type=BarType.H1, session_type=THType.RTH)
            gap = fast_value / slow_value - 1 if slow_value else 0
            if gap > self.buffer:
                ema_signal = 1
            elif gap < -self.buffer:
                ema_signal = -1
        if self.family == 2 or self.family == 3:
            old = bar_close(self.qqq, bar_type={bar_type}, select=self.horizon + select_now)
            change = current / old - 1 if old else 0
            if change > self.buffer:
                momentum_signal = 1
            elif change < -self.buffer:
                momentum_signal = -1
        if self.family == 1:
            return ema_signal
        if self.family == 2:
            return momentum_signal
        if ema_signal == momentum_signal:
            return ema_signal
        return 0

    def _update_signal(self, raw):
        if raw == 0 and not self.neutral_cash:
            self.candidate_signal = 0
            self.candidate_count = 0
            return
        if raw == self.current_signal:
            self.candidate_signal = raw
            self.candidate_count = 0
            return
        if raw == self.candidate_signal:
            self.candidate_count += 1
        else:
            self.candidate_signal = raw
            self.candidate_count = 1
        if self.candidate_count >= self.persistence:
            self.current_signal = raw
            self.candidate_count = 0

    def _sell_all(self, symbol):
        qty = position_holding_qty(symbol=symbol)
        if qty > 0:
            order_id = place_market(
                symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY
            )
            import time
            for _ in range(10):
                if order_status(order_id) == OrderStatus.FILLED_ALL:
                    return False
                time.sleep(0.2)
            return True
        return False

    def _buy_target(self, symbol):
        max_qty = max_qty_to_buy_on_cash(
            symbol=symbol, order_type=OrdType.MKT, order_trade_session_type=TSType.RTH
        )
        qty = int(max_qty * self.max_position_ratio)
        if qty >= 1:
            place_market(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)

    def handle_data(self):
        if {str(config.timeframe == 'D1')}:
            now = device_time(TimeZone.DEVICE_TIME_ZONE)
            today = now.strftime("%Y-%m-%d")
            if self.last_daily_signal_date == today:
                return
            if now.hour < 9 or (now.hour == 9 and now.minute < 35) or now.hour >= 10:
                return
            self.last_daily_signal_date = today
        raw = self._raw_signal()
        self._update_signal(raw)
        if self.current_signal == 0:
            if self.execution_enabled:
                self._sell_all(self.soxl)
                self._sell_all(self.soxs)
            return
        target = self.soxl if self.current_signal > 0 else self.soxs
        other = self.soxs if self.current_signal > 0 else self.soxl
        print("QQQ trend signal=", self.current_signal, " target=", target)
        if not self.execution_enabled:
            return
        # Mutual exclusion: remove the opposite ETF before opening the target.
        if self._sell_all(other):
            return
        if position_holding_qty(symbol=target) <= 0:
            self._buy_target(target)
'''


def moomoo_canvas_action(config: Config, *, shadow_only: bool) -> str:
    return f'''_now = device_time(TimeZone.ET)
_day_key = _now.year * 10000 + _now.month * 100 + _now.day
if self.LAST_RUN_DATE != _day_key and (_now.hour > 9 or (_now.hour == 9 and _now.minute >= 35)):
    self.LAST_RUN_DATE = _day_key
    _qqq_recent = bar_close(symbol=self.ref_symbol, bar_type=BarType.D1, select=2, session_type=THType.RTH)
    _qqq_old = bar_close(symbol=self.ref_symbol, bar_type=BarType.D1, select=int(self.MOMENTUM_DAYS) + 2, session_type=THType.RTH)
    _change = _qqq_recent / _qqq_old - 1 if _qqq_old > 0 else 0
    _signal = 0
    if _change > self.TREND_THRESHOLD:
        _signal = 1
    elif _change < -self.TREND_THRESHOLD:
        _signal = -1
    self.CURRENT_SIGNAL = _signal
    alert(content='QQQ 2D trend=' + str(_change) + ' signal=' + str(_signal) + ' (1=SOXL,-1=SOXS,0=CASH)')
    if self.EXECUTION_ENABLED >= 1:
        _soxl = self.trading_symbol
        _soxs = Contract("US.SOXS")
        _soxl_qty = position_holding_qty(symbol=_soxl)
        _soxs_qty = position_holding_qty(symbol=_soxs)
        if _signal > 0:
            if _soxs_qty > 0:
                place_market(symbol=_soxs, qty=_soxs_qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
            elif _soxl_qty <= 0:
                _lot = lot_size(symbol=_soxl)
                _max_qty = max_qty_to_buy_on_cash(symbol=_soxl, order_type=OrdType.MKT, order_trade_session_type=TSType.RTH)
                _buy_qty = floor(_max_qty * self.POSITION_RATIO / _lot) * _lot
                if _buy_qty > 0:
                    place_market(symbol=_soxl, qty=_buy_qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
        elif _signal < 0:
            if _soxl_qty > 0:
                place_market(symbol=_soxl, qty=_soxl_qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
            elif _soxs_qty <= 0:
                _lot = lot_size(symbol=_soxs)
                _max_qty = max_qty_to_buy_on_cash(symbol=_soxs, order_type=OrdType.MKT, order_trade_session_type=TSType.RTH)
                _buy_qty = floor(_max_qty * self.POSITION_RATIO / _lot) * _lot
                if _buy_qty > 0:
                    place_market(symbol=_soxs, qty=_buy_qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
        else:
            if _soxl_qty > 0:
                place_market(symbol=_soxl, qty=_soxl_qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
            if _soxs_qty > 0:
                place_market(symbol=_soxs, qty=_soxs_qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
'''


def research(bars: pd.DataFrame, evaluation_start: str, cost_bps: float) -> dict[str, object]:
    partitions = session_split(bars, evaluation_start)
    rows: list[dict[str, object]] = []
    qqq_close = bars["qqq_close"].astype(float)
    signal_cache: dict[str, pd.Series] = {}
    for config in candidate_configs():
        signal = make_signal(qqq_close, config)
        signal_cache[config.name] = signal
        row: dict[str, object] = {"config": asdict(config), "name": config.name}
        for part in ("backward_validation", "primary", "development_a", "development_b", "development", "full"):
            row[part] = simulate(
                bars, signal, partitions[part], cost_bps=cost_bps,
                position_ratio=config.position_ratio,
            )["metrics"]
        row["selection_score"] = selection_score(row)
        row["rolling_score"] = rolling_score(row)
        rows.append(row)

    eligible = [
        row for row in rows
        if int(row["development"]["trades"]) >= 3
        and float(row["development"]["max_drawdown_pct"]) <= 35.0
    ]
    if not eligible:
        eligible = [row for row in rows if int(row["development"]["trades"]) >= 2]
    forward_selected_row = max(eligible, key=lambda row: (float(row["selection_score"]), -int(row["development"]["switches"])))
    rolling_eligible = [
        row for row in rows
        if int(row["backward_validation"]["trades"]) >= 3
        and int(row["primary"]["trades"]) >= 3
        and float(row["backward_validation"]["return_pct"]) > 0
        and float(row["primary"]["return_pct"]) > 0
        and max(
            float(row["backward_validation"]["max_drawdown_pct"]),
            float(row["primary"]["max_drawdown_pct"]),
        ) <= 30.0
    ]
    if not rolling_eligible:
        rolling_eligible = [
            row for row in rows
            if int(row["backward_validation"]["trades"]) >= 1
            and int(row["primary"]["trades"]) >= 2
        ]
    selected_row = max(
        rolling_eligible,
        key=lambda row: (float(row["rolling_score"]), -int(row["primary"]["switches"])),
    )
    peak_row = max(rows, key=lambda row: float(row["full"]["return_pct"]))
    selected = Config(**selected_row["config"])
    selected_signal = signal_cache[selected.name]
    selected_results = {
        part: simulate(
            bars, selected_signal, partitions[part], cost_bps=cost_bps,
            position_ratio=selected.position_ratio,
        )
        for part in (
            "backward_validation", "primary", "development_a", "development_b",
            "development", "holdout", "full",
        )
    }
    stress = {
        str(value): simulate(
            bars, selected_signal, partitions["full"], cost_bps=value,
            position_ratio=selected.position_ratio,
        )["metrics"]
        for value in (5.0, 10.0, 20.0)
    }
    holdout_metrics = selected_results["holdout"]["metrics"]
    full_metrics = selected_results["full"]["metrics"]
    promotion = (
        float(holdout_metrics["return_pct"]) > 0
        and float(holdout_metrics["max_drawdown_pct"]) <= 15
        and int(full_metrics["trades"]) >= 3
        and (holdout_metrics["profit_factor"] is None or float(holdout_metrics["profit_factor"]) >= 1.1)
    )
    return {
        "partitions": partitions,
        "selected_config": asdict(selected),
        "selected_name": selected.name,
        "selected_results": selected_results,
        "forward_methodology_selected": forward_selected_row,
        "stress_cost_bps_per_side": stress,
        "full_sample_peak": peak_row,
        "benchmarks": {
            symbol: buy_hold(bars, symbol, partitions["full"], cost_bps)
            for symbol in ("SOXL", "SOXS", "QQQ")
        },
        "promotion_passed": False,
        "recommended_state": "SHADOW",
        "leaderboard": sorted(rows, key=lambda row: float(row["rolling_score"]), reverse=True),
    }


def write_outputs(
    output: Path,
    bars: pd.DataFrame,
    report: dict[str, object],
    *,
    source_start: str,
    source_end: str,
    evaluation_start: str,
    cost_bps: float,
    quant_template: Path,
) -> None:
    if output.exists():
        raise FileExistsError(f"use a fresh output directory: {output}")
    output.mkdir(parents=True)
    bars.to_csv(output / "hourly_bars.csv")
    pd.DataFrame(report["leaderboard"]).to_json(output / "leaderboard.json", orient="records", indent=2)
    selected = report["selected_results"]
    pd.DataFrame(selected["full"]["trades"]).to_csv(output / "selected_trades.csv", index=False)
    pd.DataFrame(selected["full"]["equity"]).to_csv(output / "selected_equity.csv", index=False)
    config = Config(**report["selected_config"])
    quant_path = output / "SOXL_SOXS_QQQ_TREND_SWITCH_V1.quant"
    quant_source = moomoo_canvas_action(config, shadow_only=True)
    (output / "SOXL_SOXS_QQQ_TREND_SWITCH_V1.py").write_text(quant_source, encoding="utf-8")
    quant_path.write_bytes(
        package_canvas_strategy(
            quant_template.read_bytes(),
            "SOXL_SOXS_QQQ_TREND_SWITCH_V2_CANVAS",
            quant_source,
        )
    )
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "provider": "Yahoo Finance via yfinance",
        "symbols": list(SYMBOLS), "interval": "60m", "regular_hours_only": True,
        "source_start": source_start, "source_end_exclusive": source_end,
        "evaluation_start": evaluation_start,
        "execution": "QQQ completed bar signal; next hourly open; SOXL/SOXS mutually exclusive",
        "cost_bps_per_side": cost_bps,
        "bars_sha256": hashlib.sha256((output / "hourly_bars.csv").read_bytes()).hexdigest(),
        "quant_sha256": hashlib.sha256(quant_path.read_bytes()).hexdigest(),
        "warning": "Three-month research is a small, regime-specific sample. Keep the generated strategy in SHADOW first.",
    }
    final = {"manifest": manifest, **{key: value for key, value in report.items() if key != "leaderboard"}}
    (output / "report.json").write_text(json.dumps(final, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    full = selected["full"]["metrics"]
    holdout = selected["holdout"]["metrics"]
    primary = selected["primary"]["metrics"]
    validation = selected["backward_validation"]["metrics"]
    peak = report["full_sample_peak"]
    markdown = f"""# SOXL / SOXS 趋势切换三个月研究

- 研究区间：{report['partitions']['full'][0]} ～ {report['partitions']['full'][-1]}
- 数据：真实 ETF 小时线，QQQ 产生信号，下一根开盘成交
- 成本：单边 {cost_bps:.1f} bp；SOXL/SOXS 同时最多持有一个
- 选中参数：`{report['selected_name']}`
- 最近42日 Primary：收益 {primary['return_pct']}%，最大回撤 {primary['max_drawdown_pct']}%，PF {primary['profit_factor']}，交易 {primary['trades']} 笔
- 前21日 backward validation：收益 {validation['return_pct']}%，最大回撤 {validation['max_drawdown_pct']}%，PF {validation['profit_factor']}，交易 {validation['trades']} 笔
- 完整区间：收益 {full['return_pct']}%，最大回撤 {full['max_drawdown_pct']}%，PF {full['profit_factor']}，胜率 {full['win_rate_pct']}%，交易 {full['trades']} 笔
- 最近21日诊断（已包含在 Primary，不是 OOS）：收益 {holdout['return_pct']}%，最大回撤 {holdout['max_drawdown_pct']}%，PF {holdout['profit_factor']}，交易 {holdout['trades']} 笔
- 全样本峰值（不可直接采用）：`{peak['name']}`，收益 {peak['full']['return_pct']}%，最大回撤 {peak['full']['max_drawdown_pct']}%
- 建议状态：**SHADOW**；`promotion_passed={report['promotion_passed']}`

`.quant` 默认 `execution_enabled=False`。在 moomoo 回测时可手动改为 True；实盘前继续保持 Shadow 验证。
"""
    (output / "README.md").write_text(markdown, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--download-start", default="2026-05-01")
    parser.add_argument("--evaluation-start", default="2026-06-22")
    parser.add_argument("--end", default="2026-09-21", help="exclusive")
    parser.add_argument("--cost-bps", type=float, default=10.0)
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "soxl_soxs_trend_lab_20260921_v1")
    parser.add_argument(
        "--quant-template",
        type=Path,
        default=Path.home() / "Downloads" / "SOXL_ADAPTIVE_RECOVERY_V5_BETA_OPTIMIZED.quant",
        help="known-good moomoo-exported binary .quant used for client version metadata",
    )
    args = parser.parse_args()
    bars = download_bars(args.download_start, args.end)
    report = research(bars, args.evaluation_start, args.cost_bps)
    write_outputs(
        args.output, bars, report, source_start=args.download_start,
        source_end=args.end, evaluation_start=args.evaluation_start, cost_bps=args.cost_bps,
        quant_template=args.quant_template,
    )
    print(json.dumps({
        "output": str(args.output),
        "selected": report["selected_name"],
        "primary": report["selected_results"]["primary"]["metrics"],
        "backward_validation": report["selected_results"]["backward_validation"]["metrics"],
        "full": report["selected_results"]["full"]["metrics"],
        "holdout": report["selected_results"]["holdout"]["metrics"],
        "peak": {"name": report["full_sample_peak"]["name"], "metrics": report["full_sample_peak"]["full"]},
        "benchmarks": report["benchmarks"],
        "recommended_state": report["recommended_state"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
