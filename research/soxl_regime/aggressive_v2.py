from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from .features import add_features


@dataclass(frozen=True)
class AggressiveConfig:
    trend_position: float = 0.65
    range_position: float = 0.35
    panic_position: float = 0.0
    trend_hard_stop: float = 0.10
    trend_trailing_stop: float = 0.08
    trend_cooldown_days: int = 5
    trend_entry_max_rsi: float = 80.0
    trend_entry_max_extension: float = 0.08
    trend_entry_hour: int = 9
    trend_require_intraday_confirm: bool = False
    trend_max_intraday_gain: float = 1.0
    range_rsi: float = 35.0
    range_boll_std: float = 1.75
    range_take_profit: float = 0.03
    range_stop_loss: float = 0.02
    panic_rsi: float = 25.0
    panic_boll_std: float = 2.0
    panic_day_drop: float = 0.06
    panic_take_profit: float = 0.015
    panic_stop_loss: float = 0.03
    max_trades_day: int = 3
    trend_intraday_only: bool = False


def _normalize_yahoo(frame: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if isinstance(frame.columns, pd.MultiIndex):
        frame = frame.xs(symbol, axis=1, level=1)
    frame = frame.rename(columns=lambda value: str(value).lower().replace(" ", "_"))
    return frame[["open", "high", "low", "close", "volume"]].dropna().sort_index()


def load_inputs(source_cache: Path, output_cache: Path, *, refresh: bool = False) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    output_cache.mkdir(parents=True, exist_ok=True)
    soxl_hourly_path = source_cache / "soxl_hourly.csv"
    soxl_daily_path = source_cache / "soxl_daily.csv"
    if not soxl_hourly_path.exists() or not soxl_daily_path.exists():
        raise FileNotFoundError("SOXL cache is missing; run research.soxl_regime.run_research first")

    hourly = pd.read_csv(soxl_hourly_path, index_col=0)
    hourly.index = pd.to_datetime(hourly.index, utc=True).tz_convert("America/New_York")
    daily = pd.read_csv(soxl_daily_path, index_col=0)
    daily.index = pd.to_datetime(daily.index)

    qqq_path = output_cache / "qqq_daily.csv"
    if refresh or not qqq_path.exists():
        raw = yf.download(
            "QQQ",
            start="2024-01-01",
            end="2026-09-19",
            interval="1d",
            auto_adjust=True,
            prepost=False,
            progress=False,
            threads=False,
        )
        if raw.empty:
            raise RuntimeError("Yahoo returned no QQQ daily data")
        qqq = _normalize_yahoo(raw, "QQQ")
        qqq.to_csv(qqq_path)
    else:
        qqq = pd.read_csv(qqq_path, index_col=0)
        qqq.index = pd.to_datetime(qqq.index)

    audit = {
        "source": "Yahoo Finance via yfinance; no OpenD",
        "price_adjustment": "auto_adjust=True for the original cached downloads",
        "soxl_hourly": {"rows": len(hourly), "start": str(hourly.index.min()), "end": str(hourly.index.max())},
        "soxl_daily": {"rows": len(daily), "start": str(daily.index.min()), "end": str(daily.index.max())},
        "qqq_daily": {"rows": len(qqq), "start": str(qqq.index.min()), "end": str(qqq.index.max())},
        "downloaded_or_checked_at": datetime.now(timezone.utc).isoformat(),
    }
    return hourly, daily, qqq, audit


def _daily_regime(soxl_daily: pd.DataFrame, qqq_daily: pd.DataFrame) -> pd.Series:
    common = soxl_daily.index.intersection(qqq_daily.index)
    soxl = add_features(soxl_daily.loc[common], slope_bars=3)
    qqq = add_features(qqq_daily.loc[common], slope_bars=3)
    trend_up = (qqq["ema20"] > qqq["ema60"]) & (qqq["close"] > qqq["ema20"]) & (soxl["close"] > soxl["ema20"])
    trend_down = (qqq["ema20"] < qqq["ema60"]) & (qqq["close"] < qqq["ema20"])
    values = np.select([trend_up, trend_down], ["TREND_UP", "TREND_DOWN"], default="RANGE")
    return pd.Series(values, index=common, name="regime")


def _metrics(equity: pd.DataFrame, trades: pd.DataFrame, *, initial: float = 100_000.0) -> dict:
    if equity.empty:
        return {"return_pct": 0.0, "cagr_pct": 0.0, "max_drawdown_pct": 0.0, "sharpe": None, "sortino": None, "calmar": None, "profit_factor": None, "win_rate_pct": None, "trades": 0}
    curve = equity["equity"].astype(float)
    returns = curve.pct_change().dropna()
    drawdown = 1 - curve / curve.cummax()
    years = max((curve.index[-1] - curve.index[0]).total_seconds() / (365.25 * 86400), 1 / 252)
    total = curve.iloc[-1] / initial - 1
    cagr = (1 + total) ** (1 / years) - 1 if total > -1 else -1
    max_dd = float(drawdown.max())
    downside = returns[returns < 0].std()
    sharpe = math.sqrt(252 * 7) * returns.mean() / returns.std() if len(returns) > 2 and returns.std() > 0 else None
    sortino = math.sqrt(252 * 7) * returns.mean() / downside if downside is not None and downside > 0 else None
    pnls = trades["pnl"].astype(float) if not trades.empty else pd.Series(dtype=float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    return {
        "return_pct": float(round(total * 100, 4)),
        "cagr_pct": float(round(cagr * 100, 4)),
        "max_drawdown_pct": float(round(max_dd * 100, 4)),
        "sharpe": round(float(sharpe), 4) if sharpe is not None else None,
        "sortino": round(float(sortino), 4) if sortino is not None else None,
        "calmar": round(float(cagr / max_dd), 4) if max_dd > 0 else None,
        "profit_factor": round(float(wins.sum() / abs(losses.sum())), 4) if len(losses) else None,
        "win_rate_pct": round(float((pnls > 0).mean() * 100), 2) if len(pnls) else None,
        "trades": int(len(trades)),
        "worst_trade_pct": round(float(trades["return_pct"].min()), 4) if not trades.empty else None,
        "contribution_by_mode": trades.groupby("mode")["pnl"].sum().round(2).to_dict() if not trades.empty else {},
    }


def simulate(
    hourly: pd.DataFrame,
    soxl_daily: pd.DataFrame,
    qqq_daily: pd.DataFrame,
    config: AggressiveConfig,
    *,
    start: str,
    end: str,
    cost_bps: float = 10.0,
    enable_range: bool = True,
    enable_panic: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    data = add_features(hourly, boll_std=config.range_boll_std)
    median_spacing = data.index.to_series().diff().median()
    is_five_minute = median_spacing <= pd.Timedelta(minutes=10)
    breakout_bars = 6 if is_five_minute else 3
    data["trend_breakout_high"] = data["high"].rolling(
        breakout_bars, min_periods=breakout_bars
    ).max().shift(1)
    dates = data.index.strftime("%Y-%m-%d")
    data = data[(dates >= start) & (dates <= end)].copy()
    if data.empty:
        return pd.DataFrame(), pd.DataFrame(), _metrics(pd.DataFrame(), pd.DataFrame())

    regime = _daily_regime(soxl_daily, qqq_daily).shift(1)
    regime.index = regime.index.strftime("%Y-%m-%d")
    data["regime"] = pd.Series(data.index.strftime("%Y-%m-%d"), index=data.index).map(regime).fillna("RANGE")
    soxl_daily_features = add_features(soxl_daily)
    daily_rsi = soxl_daily_features["rsi14"].shift(1)
    daily_rsi.index = daily_rsi.index.strftime("%Y-%m-%d")
    data["daily_rsi14"] = pd.Series(data.index.strftime("%Y-%m-%d"), index=data.index).map(daily_rsi).fillna(50.0)
    daily_extension = (soxl_daily_features["close"] / soxl_daily_features["ema20"] - 1).shift(1)
    daily_extension.index = daily_extension.index.strftime("%Y-%m-%d")
    data["daily_extension"] = pd.Series(data.index.strftime("%Y-%m-%d"), index=data.index).map(daily_extension).fillna(0.0)
    cost = cost_bps / 10_000

    cash = 100_000.0
    qty = 0
    entry_price = 0.0
    entry_value = 0.0
    entry_time = None
    mode = "NONE"
    peak = 0.0
    cooldown_until = None
    current_session = None
    trades_today = 0
    last_trend_entry_session = None
    pending_exit: str | None = None
    trades: list[dict] = []
    curve: list[dict] = []

    def close_position(price: float, timestamp, reason: str) -> None:
        nonlocal cash, qty, entry_price, entry_value, entry_time, mode, peak, cooldown_until
        fill = float(price) * (1 - cost)
        proceeds = qty * fill
        pnl = proceeds - entry_value
        trades.append({
            "mode": mode,
            "entry_time": entry_time,
            "exit_time": timestamp,
            "entry_price": entry_price,
            "exit_price": fill,
            "qty": qty,
            "pnl": pnl,
            "return_pct": (fill / entry_price - 1) * 100,
            "exit_reason": reason,
        })
        cash += proceeds
        if mode == "TREND" and reason in {"hard_stop", "trailing_stop"}:
            cooldown_until = timestamp.date() + pd.Timedelta(days=config.trend_cooldown_days)
        qty = 0
        entry_price = 0.0
        entry_value = 0.0
        entry_time = None
        mode = "NONE"
        peak = 0.0

    for i in range(1, len(data)):
        row = data.iloc[i]
        prev = data.iloc[i - 1]
        timestamp = data.index[i]
        session = timestamp.date()
        if current_session != session:
            current_session = session
            trades_today = 0
        exited_this_bar = False

        if pending_exit is not None and qty > 0:
            close_position(float(row.open), timestamp, pending_exit)
            pending_exit = None
            exited_this_bar = True

        # Intraday branches are flattened at the opening of the final hourly bar.
        session_flat = (
            timestamp.hour > 15
            or (timestamp.hour == 15 and timestamp.minute >= 45)
        ) if is_five_minute else bool(row.is_last_bar)
        if (
            qty > 0
            and (mode in {"RANGE", "DOWN_PANIC"} or config.trend_intraday_only)
            and session_flat
        ):
            close_position(float(row.open), timestamp, "session_flat")
            exited_this_bar = True

        if qty > 0:
            position_return = float(prev.close) / entry_price - 1
            if mode == "TREND":
                peak = max(peak, float(prev.high))
                if prev.regime != "TREND_UP":
                    pending_exit = "regime_exit"
                elif position_return <= -config.trend_hard_stop:
                    pending_exit = "hard_stop"
                elif float(prev.close) <= peak * (1 - config.trend_trailing_stop):
                    pending_exit = "trailing_stop"
            elif mode == "RANGE":
                if prev.rsi14 >= 55 or prev.close >= prev.boll_mid or position_return >= config.range_take_profit or position_return <= -config.range_stop_loss:
                    pending_exit = "range_exit"
            elif mode == "DOWN_PANIC":
                if position_return >= config.panic_take_profit or position_return <= -config.panic_stop_loss:
                    pending_exit = "panic_exit"

        entry_window = (
            (timestamp.hour > 9 or (timestamp.hour == 9 and timestamp.minute >= 45))
            and (timestamp.hour < 15 or (timestamp.hour == 15 and timestamp.minute <= 30))
        )
        if (
            qty == 0
            and not exited_this_bar
            and not bool(row.is_last_bar)
            and entry_window
            and trades_today < config.max_trades_day
        ):
            reversal = float(prev.close) > float(data.iloc[i - 2].close) if i >= 2 else False
            selected_mode = "NONE"
            ratio = 0.0
            if (
                prev.regime == "TREND_UP"
                and prev.daily_rsi14 <= config.trend_entry_max_rsi
                and prev.daily_extension <= config.trend_entry_max_extension
                and (cooldown_until is None or timestamp.date() > cooldown_until)
                and last_trend_entry_session != session
                and timestamp.hour >= config.trend_entry_hour
                and prev.day_drop >= -config.trend_max_intraday_gain
                and (
                    not config.trend_require_intraday_confirm
                    or (
                        prev.close > prev.ema20
                        and prev.ema20 > prev.ema60
                        and prev.close > prev.trend_breakout_high
                    )
                )
            ):
                selected_mode = "TREND"
                ratio = config.trend_position
            elif enable_range and prev.regime == "RANGE" and prev.rsi14 < config.range_rsi and prev.close < prev.boll_lower and reversal:
                selected_mode = "RANGE"
                ratio = config.range_position
            elif enable_panic and prev.regime == "TREND_DOWN" and prev.rsi14 < config.panic_rsi and prev.close < prev.boll_lower and prev.day_drop >= config.panic_day_drop and reversal:
                selected_mode = "DOWN_PANIC"
                ratio = config.panic_position

            if ratio > 0:
                fill = float(row.open) * (1 + cost)
                target_qty = int(cash * ratio / fill)
                if target_qty > 0:
                    qty = target_qty
                    entry_value = qty * fill
                    cash -= entry_value
                    entry_price = fill
                    entry_time = timestamp
                    mode = selected_mode
                    peak = float(row.high)
                    trades_today += 1
                    if selected_mode == "TREND":
                        last_trend_entry_session = session

        curve.append({"timestamp": timestamp, "equity": cash + qty * float(row.close), "mode": mode, "regime": row.regime})

    if qty > 0:
        close_position(float(data.iloc[-1].close), data.index[-1], "window_end")
        if curve:
            curve[-1]["equity"] = cash

    equity = pd.DataFrame(curve).set_index("timestamp") if curve else pd.DataFrame()
    trade_frame = pd.DataFrame(trades)
    result = _metrics(equity, trade_frame)
    result["config"] = asdict(config)
    return equity, trade_frame, result


def _buy_hold(hourly: pd.DataFrame, start: str, end: str, cost_bps: float = 10.0) -> dict:
    dates = hourly.index.strftime("%Y-%m-%d")
    frame = hourly[(dates >= start) & (dates <= end)]
    cost = cost_bps / 10_000
    initial = float(frame.iloc[0].open) * (1 + cost)
    curve = frame.close / initial
    return {
        "return_pct": float(round((float(frame.iloc[-1].close) * (1 - cost) / initial - 1) * 100, 4)),
        "max_drawdown_pct": float(round(float((1 - curve / curve.cummax()).max()) * 100, 4)),
    }


def _write_report(output: Path, result: dict) -> None:
    n = result["near_year"]
    lines = [
        "# SOXL Regime Aggressive V2 研究报告",
        "",
        "## 结论",
        "",
        f"近一年 Yahoo 60m 代理回测：收益 **{n['return_pct']}%**，最大回撤 **{n['max_drawdown_pct']}%**，Sharpe **{n['sharpe']}**，胜率 **{n['win_rate_pct']}%**，完成交易 **{n['trades']}** 笔。",
        "",
        f"研究门禁：**{result['final_status']}**。这是近一年激进候选，不代表跨周期稳定；两年扩展窗口最大回撤为 {result['full_two_year']['max_drawdown_pct']}%。",
        "",
        "## 核心逻辑",
        "",
        "- QQQ 前一完整日 EMA20 > EMA60、QQQ 收盘 > EMA20，且 SOXL 前一完整日收盘 > EMA20：TREND_UP，目标仓位 65%。",
        "- TREND_UP 使用 10% 硬止损、8% 跟踪止损；止损后冷却 5 个自然日，允许隔夜持有；新开仓要求 SOXL 距日线 EMA20 不超过 8%，避免高位追涨。",
        "- 其余中性区间使用 35% 的 RSI/BOLL 超跌反弹并日内平仓；TREND_DOWN 的恐慌反弹在扩展样本为负，正式候选保持现金。",
        "- 所有日线信号均 shift(1)，小时信号在下一根开盘成交；单边成本基准 10 bps。",
        "",
        "## 分段与压力",
        "",
        "| 窗口 | 收益 | 最大回撤 | Sharpe | 交易数 |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in result["segments"]:
        lines.append(f"| {row['name']} | {row['return_pct']}% | {row['max_drawdown_pct']}% | {row['sharpe']} | {row['trades']} |")
    lines += ["", "成本压力（单边）："]
    for bps, metrics in result["cost_stress_bps_per_side"].items():
        lines.append(f"- {bps} bps：收益 {metrics['return_pct']}%，回撤 {metrics['max_drawdown_pct']}%。")
    lines += [
        "",
        "## 限制",
        "",
        "- 未使用 OpenD；行情来自 Yahoo，不是富途 5m 撮合数据。",
        "- 一年主结果使用 60m 代理执行，必须在富途导入后用 5m 回测复核成交、费用与滑点。",
        "- 2026 上涨幅度异常强，Buy & Hold 同期收益更高；策略优势是降低暴露与回撤，不是追平满仓 SOXL。",
        "- 两年扩展窗口回撤仍较大，因此文件命名为 AGGRESSIVE_BACKTEST，不应直接加入实盘。",
        "",
    ]
    (output / "SOXL_REGIME_AGGRESSIVE_V2_REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def run(output: Path, *, refresh: bool = False) -> dict:
    source_cache = Path("outputs/soxl_regime_switch_v1/cache")
    hourly, daily, qqq, audit = load_inputs(source_cache, output / "cache", refresh=refresh)
    config = AggressiveConfig()
    windows = [
        ("near_year", "2025-09-22", "2026-09-18"),
        ("H1", "2025-09-22", "2026-03-20"),
        ("H2", "2026-03-23", "2026-09-18"),
        ("Q1", "2025-09-22", "2025-12-19"),
        ("Q2", "2025-12-22", "2026-03-20"),
        ("Q3", "2026-03-23", "2026-06-19"),
        ("Q4", "2026-06-22", "2026-09-18"),
        ("prior_year", "2024-09-25", "2025-09-19"),
        ("full_two_year", "2024-09-25", "2026-09-18"),
    ]
    metrics_by_window: dict[str, dict] = {}
    near_equity = pd.DataFrame()
    near_trades = pd.DataFrame()
    for name, start, end in windows:
        equity, trades, metrics = simulate(hourly, daily, qqq, config, start=start, end=end, cost_bps=10)
        metrics_by_window[name] = metrics
        if name == "near_year":
            near_equity, near_trades = equity, trades

    cost_stress = {}
    for bps in (5, 10, 20):
        _, _, metrics = simulate(hourly, daily, qqq, config, start="2025-09-22", end="2026-09-18", cost_bps=bps)
        cost_stress[str(bps)] = metrics

    neighborhood_rows = []
    for position in (0.55, 0.65, 0.75):
        for trailing in (0.06, 0.08, 0.10):
            for max_extension in (0.06, 0.08, 0.10):
                candidate = replace(config, trend_position=position, trend_trailing_stop=trailing, trend_entry_max_extension=max_extension)
                _, _, metrics = simulate(hourly, daily, qqq, candidate, start="2025-09-22", end="2026-09-18", cost_bps=10)
                neighborhood_rows.append({"trend_position": position, "trend_trailing_stop": trailing, "trend_entry_max_extension": max_extension, **metrics})

    near = metrics_by_window["near_year"]
    halves_positive = metrics_by_window["H1"]["return_pct"] > 0 and metrics_by_window["H2"]["return_pct"] > 0
    neighborhood_positive = sum(row["return_pct"] > 0 for row in neighborhood_rows)
    passed = bool(
        near["return_pct"] >= 40
        and near["max_drawdown_pct"] <= 25
        and halves_positive
        and cost_stress["20"]["return_pct"] > 0
        and neighborhood_positive >= 22
    )

    result = {
        "data_audit": audit,
        "config": asdict(config),
        **metrics_by_window,
        "segments": [{"name": name, **metrics_by_window[name]} for name in ("H1", "H2", "Q1", "Q2", "Q3", "Q4", "prior_year")],
        "cost_stress_bps_per_side": cost_stress,
        "parameter_neighborhood_positive": f"{neighborhood_positive}/{len(neighborhood_rows)}",
        "benchmark_buy_hold_near_year": _buy_hold(hourly, "2025-09-22", "2026-09-18"),
        "provided_futu_v4_down_interval": {"return_pct": 10.41, "max_drawdown_pct": 6.90, "orders": 93},
        "provided_futu_v4_up_interval": {"return_pct": 0.92, "max_drawdown_pct": 11.96, "orders": 154},
        "final_status": "RESEARCH PASS" if passed else "FAIL",
        "acceptance": {
            "near_year_return_at_least_40": bool(near["return_pct"] >= 40),
            "near_year_drawdown_at_most_25": bool(near["max_drawdown_pct"] <= 25),
            "both_halves_positive": bool(halves_positive),
            "20bps_cost_positive": bool(cost_stress["20"]["return_pct"] > 0),
            "neighborhood_at_least_22_of_27_positive": bool(neighborhood_positive >= 22),
        },
        "limitations": [
            "No OpenD was used.",
            "The primary execution proxy is Yahoo 60-minute data, not Futu 5-minute data.",
            "The result is selected for the latest one-year regime and is not robust over the full two-year extension.",
        ],
    }

    output.mkdir(parents=True, exist_ok=True)
    near_equity.to_csv(output / "near_year_equity.csv")
    near_trades.to_csv(output / "near_year_trades.csv", index=False)
    pd.DataFrame(neighborhood_rows).to_csv(output / "parameter_neighborhood.csv", index=False)
    (output / "research_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    _write_report(output, result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("outputs/soxl_regime_aggressive_v2"))
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    result = run(args.output, refresh=args.refresh)
    print(json.dumps({key: result[key] for key in ("near_year", "H1", "H2", "prior_year", "full_two_year", "cost_stress_bps_per_side", "parameter_neighborhood_positive", "final_status", "acceptance")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
