"""Completed-session dates and checks for the morning briefing."""
from __future__ import annotations

from typing import Any, Mapping

import exchange_calendars as xcals
import pandas as pd

CALENDARS = {"us": "XNYS", "cn": "XSHG", "hk": "XHKG"}
BENCHMARKS = {
    "us": {"SPY": "标普500ETF", "QQQ": "纳指100ETF", "DIA": "道指ETF", "SOXX": "半导体ETF"},
    "cn": {"000001.SS": "上证指数"},
    "hk": {"^HSI": "恒生指数"},
}
MIN_COVERAGE = 0.8


def utc_now(now: Any = None) -> pd.Timestamp:
    value = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    return value.tz_localize("Asia/Shanghai").tz_convert("UTC") if value.tzinfo is None else value.tz_convert("UTC")


def latest_completed_session(market: str, now: Any = None) -> str:
    moment = utc_now(now)
    calendar = xcals.get_calendar(CALENDARS[market])
    schedule = calendar.schedule.loc[(moment - pd.Timedelta(days=30)).date().isoformat():moment.date().isoformat()]
    closed = schedule[schedule["close"] <= moment]
    if closed.empty:
        raise RuntimeError(f"无法确定 {market} 最近完整交易日")
    return closed.index[-1].date().isoformat()


def completed_bars(frame: pd.DataFrame, session: str) -> pd.DataFrame:
    if frame is None or frame.empty or not isinstance(frame.index, pd.DatetimeIndex):
        return pd.DataFrame()
    out = frame.copy().sort_index()
    out = out[~out.index.duplicated(keep="last")]
    # Daily bar labels are exchange-local trading dates. Never shift them to UTC.
    dates = out.index.tz_localize(None) if out.index.tz is not None else out.index
    out = out.loc[dates.normalize() <= pd.Timestamp(session)]
    required = ["Close", "High", "Low", "Volume"]
    if not all(key in out.columns for key in required):
        return pd.DataFrame()
    out[required] = out[required].apply(pd.to_numeric, errors="coerce")
    out = out.dropna(subset=required)
    return out.loc[(out["Close"] > 0) & (out["Volume"] > 0)]


def data_date(frame: pd.DataFrame) -> str:
    return frame.index[-1].date().isoformat() if frame is not None and not frame.empty else ""


def quality_errors(snapshots: Mapping[str, Mapping[str, Any]]) -> list[str]:
    errors = []
    for snap in snapshots.values():
        label = str(snap.get("label", ""))
        expected = snap.get("expected_session")
        if not expected or snap.get("data_date") != expected:
            errors.append(f"{label}行情日期 {snap.get('data_date') or '缺失'}，应为 {expected or '缺失'}")
        if snap.get("coverage_ratio", 0) < MIN_COVERAGE:
            errors.append(f"{label}有效覆盖率 {snap.get('coverage_ratio', 0):.0%}，低于 {MIN_COVERAGE:.0%}")
        if snap.get("missing_benchmarks"):
            errors.append(f"{label}缺少当日市场基准：{','.join(snap['missing_benchmarks'])}")
        if snap.get("crosscheck_errors"):
            errors.extend(f"{label} {item}" for item in snap["crosscheck_errors"])
    return errors
