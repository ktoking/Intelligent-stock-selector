"""
yfinance 历史数据 SQLite TTL 缓存。

每次调用 get_history() 时先查 SQLite，命中且未过期直接返回；
否则从 yfinance 拉取后写入缓存。

TTL 策略：
  - 日线（1d）：300 秒（5 分钟）
  - 分线（1m/5m/15m 等）：60 秒（1 分钟）

缓存文件：项目 data/cache.db（自动创建）
"""
import json
import math
import sqlite3
import time
from pathlib import Path
from typing import Optional

import pandas as pd
import yfinance as yf

_DB_PATH = Path(__file__).parent.parent / "data" / "cache.db"
_TTL_DAILY = 300    # 5 min
_TTL_INTRADAY = 60  # 1 min

# 建表 SQL（首次运行自动初始化）
_DDL = """
CREATE TABLE IF NOT EXISTS hist_cache (
    cache_key TEXT PRIMARY KEY,
    fetched_at REAL NOT NULL,
    payload    TEXT NOT NULL
);
"""


def _get_conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.execute(_DDL)
    conn.commit()
    return conn


def _ttl(interval: str) -> int:
    return _TTL_DAILY if interval == "1d" else _TTL_INTRADAY


def _cache_key(ticker: str, period: str, interval: str, prepost: bool) -> str:
    return f"{ticker.upper()}|{period}|{interval}|{int(prepost)}"


def _df_to_json(df: pd.DataFrame) -> str:
    """序列化 DataFrame，兼容带时区的 DatetimeIndex。"""
    df2 = df.copy()
    if hasattr(df2.index, "tz") and df2.index.tz is not None:
        df2.index = df2.index.tz_convert("UTC").tz_localize(None)
    return df2.to_json(date_format="iso", orient="split")


def _json_to_df(payload: str) -> pd.DataFrame:
    df = pd.read_json(payload, orient="split")
    df.index = pd.to_datetime(df.index)
    return df


def get_history(
    ticker: str,
    period: str = "6mo",
    interval: str = "1d",
    prepost: bool = False,
) -> Optional[pd.DataFrame]:
    """
    拉取 yfinance 历史 K 线，命中缓存则直接返回，否则请求网络后写入缓存。
    返回 None 表示拉取失败（yfinance 无数据 / 网络异常）。
    """
    key = _cache_key(ticker, period, interval, prepost)
    ttl = _ttl(interval)

    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT fetched_at, payload FROM hist_cache WHERE cache_key = ?", (key,)
        ).fetchone()
        if row is not None:
            age = time.time() - row[0]
            if age < ttl:
                df = _json_to_df(row[1])
                if df is not None and len(df) > 0:
                    return df
    except Exception:
        pass

    # 缓存未命中或已过期，从 yfinance 拉取
    try:
        hist = yf.Ticker(ticker).history(period=period, interval=interval, prepost=prepost)
    except Exception:
        hist = None

    if hist is None or len(hist) == 0:
        return None

    try:
        payload = _df_to_json(hist)
        with _get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO hist_cache VALUES (?, ?, ?)",
                (key, time.time(), payload),
            )
    except Exception:
        pass

    return hist


def invalidate(ticker: str, period: str, interval: str, prepost: bool = False) -> None:
    """手动使某条缓存失效（调试用）。"""
    key = _cache_key(ticker, period, interval, prepost)
    try:
        with _get_conn() as conn:
            conn.execute("DELETE FROM hist_cache WHERE cache_key = ?", (key,))
    except Exception:
        pass


def cache_stats() -> dict:
    """返回缓存基本统计信息（条目数、DB 大小）。"""
    try:
        conn = _get_conn()
        count = conn.execute("SELECT COUNT(*) FROM hist_cache").fetchone()[0]
        db_size_kb = _DB_PATH.stat().st_size // 1024 if _DB_PATH.exists() else 0
        return {"entries": count, "db_size_kb": db_size_kb}
    except Exception:
        return {"entries": -1, "db_size_kb": -1}
