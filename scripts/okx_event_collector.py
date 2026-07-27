#!/usr/bin/env python3
"""Build a read-only event-label feed for OKX equity-token research.

Earnings/news come from yfinance and are advisory labels only.  CPI/FOMC dates
are sourced from the official BLS/Federal Reserve calendars and are stored as
event-risk windows rather than directional predictions.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_research_universe import load_symbols  # noqa: E402

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
OUTPUT = ROOT / "data" / "okx_event_calendar.json"
# Official 2026 release/decision calendar.  Times are New York local time.
# Sources: https://www.bls.gov/schedule/news_release/cpi.htm and
# https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
MACRO_EVENTS = [
    ("2026-03-11T08:30:00", "CPI"), ("2026-04-10T08:30:00", "CPI"),
    ("2026-05-12T08:30:00", "CPI"), ("2026-06-10T08:30:00", "CPI"),
    ("2026-07-14T08:30:00", "CPI"), ("2026-08-12T08:30:00", "CPI"),
    ("2026-09-11T08:30:00", "CPI"), ("2026-10-14T08:30:00", "CPI"),
    ("2026-11-10T08:30:00", "CPI"), ("2026-12-10T08:30:00", "CPI"),
    ("2026-03-18T14:00:00", "FOMC_DECISION"), ("2026-04-29T14:00:00", "FOMC_DECISION"),
    ("2026-06-17T14:00:00", "FOMC_DECISION"), ("2026-07-29T14:00:00", "FOMC_DECISION"),
    ("2026-09-16T14:00:00", "FOMC_DECISION"), ("2026-10-28T14:00:00", "FOMC_DECISION"),
    ("2026-12-09T14:00:00", "FOMC_DECISION"),
]


def parse_earnings(value: object) -> str | None:
    if isinstance(value, (list, tuple)) and value:
        value = value[0]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value) if value else None


def main() -> None:
    symbols = load_symbols("forward_observation")
    equities = [symbol.split("-", 1)[0] for symbol in symbols if symbol not in {"SPY-USDT-SWAP", "QQQ-USDT-SWAP", "SMH-USDT-SWAP"}]
    earnings, news, errors = [], [], []
    for symbol in equities:
        try:
            ticker = yf.Ticker(symbol)
            calendar = ticker.calendar or {}
            date = parse_earnings(calendar.get("Earnings Date"))
            if date:
                earnings.append({"symbol": symbol, "event_type": "EARNINGS", "scheduled_at": date, "source": "yfinance_calendar"})
            # Historical dates are kept separately from the upcoming calendar.
            # A missing/unsupported ticker simply contributes no label.
            try:
                history = ticker.get_earnings_dates(limit=12)
                for stamp, _row in history.iterrows():
                    earnings.append({"symbol": symbol, "event_type": "EARNINGS_REPORTED",
                                     "scheduled_at": stamp.isoformat(), "source": "yfinance_earnings_dates"})
            except Exception:
                pass
            for item in (ticker.news or [])[:3]:
                news.append({"symbol": symbol, "event_type": "NEWS", "published_at": item.get("providerPublishTime"),
                             "title": item.get("title", ""), "publisher": item.get("publisher", ""),
                             "link": item.get("link", "")})
        except Exception as exc:
            errors.append({"symbol": symbol, "error": str(exc)[:160]})
    macro = [{"event_type": kind, "scheduled_at": NY.localize(datetime.fromisoformat(stamp)).astimezone(UTC).isoformat()
              if hasattr(NY, "localize") else datetime.fromisoformat(stamp).replace(tzinfo=NY).astimezone(UTC).isoformat(),
              "risk_window_minutes": 90, "source": "official_calendar"} for stamp, kind in MACRO_EVENTS]
    # zoneinfo does not provide localize; retain the expression above for
    # compatibility with historical Python environments.
    now = datetime.now(UTC)
    report = {"generated_at": now.isoformat(), "mode": "research_labels_only", "symbols": equities,
              "macro_events": macro, "earnings": earnings, "news": news, "errors": errors,
              "sources": {"cpi": "https://www.bls.gov/schedule/news_release/cpi.htm",
                          "fomc": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
                          "earnings_news": "yfinance"},
              "usage": "Event windows are filters/features for shadow research, never a standalone trade signal."}
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({"output": str(OUTPUT), "earnings": len(earnings), "news": len(news), "macro": len(macro), "errors": len(errors)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
