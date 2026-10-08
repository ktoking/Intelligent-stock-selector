from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf


@dataclass(frozen=True)
class DataAudit:
    interval: str
    source: str
    feed: str
    requested_start: str
    requested_end: str
    actual_start: str
    actual_end: str
    rows: int
    duplicate_rows: int
    nonpositive_prices: int
    missing_expected_rth_bars: int | None
    downloaded_at: str


def _normalize(frame: pd.DataFrame, interval: str) -> pd.DataFrame:
    if isinstance(frame.columns, pd.MultiIndex):
        frame = frame.xs("SOXL", axis=1, level=1)
    frame = frame.rename(columns=lambda value: str(value).lower().replace(" ", "_"))
    frame = frame[["open", "high", "low", "close", "volume"]].copy()
    frame = frame[~frame.index.duplicated(keep="last")].sort_index().dropna()
    if interval != "1d":
        if frame.index.tz is None:
            frame.index = frame.index.tz_localize("UTC")
        frame.index = frame.index.tz_convert("America/New_York")
        frame = frame.between_time("09:30", "16:00")
    return frame


def download_soxl(cache_dir: Path, *, refresh: bool = False) -> tuple[dict[str, pd.DataFrame], dict[str, dict]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    specs = {
        "hourly": ("60m", "2024-09-25", "2026-09-19"),
        "five_minute_recent": ("5m", "2026-07-25", "2026-09-19"),
        "daily": ("1d", "2024-01-01", "2026-09-19"),
    }
    frames: dict[str, pd.DataFrame] = {}
    audits: dict[str, dict] = {}
    for name, (interval, start, end) in specs.items():
        path = cache_dir / f"soxl_{name}.csv"
        if path.exists() and not refresh:
            raw = pd.read_csv(path, index_col=0)
            if interval != "1d":
                raw.index = pd.to_datetime(raw.index, utc=True).tz_convert("America/New_York")
            else:
                raw.index = pd.to_datetime(raw.index)
            frame = raw
        else:
            raw = yf.download(
                "SOXL", start=start, end=end, interval=interval, auto_adjust=True,
                prepost=False, progress=False, threads=False,
            )
            if raw.empty:
                raise RuntimeError(f"Yahoo returned no {interval} SOXL data")
            frame = _normalize(raw, interval)
            frame.to_csv(path)
        duplicates = int(frame.index.duplicated().sum())
        bad_prices = int((frame[["open", "high", "low", "close"]] <= 0).any(axis=1).sum())
        missing = None
        if interval in {"60m", "5m"}:
            expected = 7 if interval == "60m" else 78
            counts = frame.groupby(frame.index.strftime("%Y-%m-%d")).size()
            missing = int((expected - counts.clip(upper=expected)).sum())
        audit = DataAudit(
            interval=interval, source="Yahoo Finance via yfinance", feed="consolidated Yahoo vendor feed",
            requested_start=start, requested_end=end, actual_start=str(frame.index.min()),
            actual_end=str(frame.index.max()), rows=len(frame), duplicate_rows=duplicates,
            nonpositive_prices=bad_prices, missing_expected_rth_bars=missing,
            downloaded_at=datetime.now(timezone.utc).isoformat(),
        )
        frames[name] = frame
        audits[name] = asdict(audit)
    return frames, audits
