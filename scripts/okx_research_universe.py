#!/usr/bin/env python3
"""Discover liquid OKX equity perpetuals for research and shadow observation.

This module never changes the execution universe.  It records a separate
research universe with a 90-session-history cohort and a broader forward-only
observation cohort ranked by public liquidity and spread.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.okx_intraday_agent import OKX, settings  # noqa: E402
from scripts.okx_multitimeframe_backtest import weekday_sessions  # noqa: E402

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
OUTPUT = ROOT / "data" / "okx_research_universe.json"
BENCHMARKS = ("SPY-USDT-SWAP", "QQQ-USDT-SWAP", "SMH-USDT-SWAP")


def discover(days: int = 90, max_forward: int = 40) -> dict:
    client = OKX(settings())
    instruments = client.request("GET", "/api/v5/public/instruments", {"instType": "SWAP"})["data"]
    tickers = {row.get("instId"): row for row in client.request("GET", "/api/v5/market/tickers", {"instType": "SWAP"})["data"]}
    end = datetime.now(NY).date() - timedelta(days=1)
    first_session = weekday_sessions(end, days)[0]
    first_180_session = weekday_sessions(end, 180)[0]
    cutoff = int(datetime.combine(first_session, datetime.min.time(), NY).timestamp() * 1000)
    cutoff_180 = int(datetime.combine(first_180_session, datetime.min.time(), NY).timestamp() * 1000)
    candidates = []
    for item in instruments:
        if item.get("instCategory") != "3" or item.get("state") != "live":
            continue
        ticker = tickers.get(item.get("instId"), {})
        bid, ask = float(ticker.get("bidPx") or 0), float(ticker.get("askPx") or 0)
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0
        spread = (ask - bid) / mid * 10_000 if mid else 9_999.0
        candidates.append({
            "inst_id": item["instId"], "underlying": item.get("ctValCcy"), "listed_at": item.get("listTime"),
            "has_90d_history": int(item.get("listTime") or 0) <= cutoff,
            "has_180d_history": int(item.get("listTime") or 0) <= cutoff_180,
            "volume_24h_usdt": float(ticker.get("volCcy24h") or 0), "spread_bps": round(spread, 3),
            "last": float(ticker.get("last") or 0), "max_leverage": int(float(item.get("lever") or 0)),
        })
    candidates.sort(key=lambda row: (row["volume_24h_usdt"], -row["spread_bps"]), reverse=True)
    forward = [row for row in candidates if row["spread_bps"] <= 10][:max_forward]
    by_id = {row["inst_id"]: row for row in forward}
    all_by_id = {row["inst_id"]: row for row in candidates}
    for symbol in BENCHMARKS:
        if symbol in all_by_id:
            by_id[symbol] = all_by_id[symbol]
    forward = sorted(by_id.values(), key=lambda row: row["inst_id"])
    historical = sorted((row for row in candidates if row["has_90d_history"]), key=lambda row: row["inst_id"])
    return {
        "generated_at": datetime.now(UTC).isoformat(), "history_cutoff_session": first_session.isoformat(),
        "history_180d_cutoff_session": first_180_session.isoformat(),
        "criteria": "OKX instCategory=3, state=live; forward pool ranks public 24h quote volume and requires spread<=10bp",
        "benchmarks": list(BENCHMARKS), "historical_90d": historical, "forward_observation": forward,
        "historical_180d": sorted((row for row in candidates if row["has_180d_history"]), key=lambda row: row["inst_id"]),
    }


def load_symbols(kind: str = "forward_observation") -> tuple[str, ...]:
    try:
        data = json.loads(OUTPUT.read_text())
        rows = data.get(kind, [])
        # An existing 90-day cache can predate the 180-day cohort field.  Build
        # that narrower cohort from its immutable listing timestamps so a brief
        # public-API/proxy outage never discards the queued long-history work.
        match = re.fullmatch(r"historical_(\d+)d", kind)
        if match and not rows:
            sessions = int(match.group(1))
            cutoff = int(datetime.combine(
                weekday_sessions(datetime.now(NY).date() - timedelta(days=1), sessions)[0],
                datetime.min.time(), NY,
            ).timestamp() * 1000)
            rows = [row for row in data.get("historical_90d", []) if int(row.get("listed_at") or 0) <= cutoff]
        symbols = tuple(row["inst_id"] for row in rows if row.get("inst_id"))
        if symbols:
            return symbols
    except (OSError, ValueError, KeyError):
        pass
    return () if kind.startswith("historical_") else BENCHMARKS


def main() -> None:
    result = discover()
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({"output": str(OUTPUT), "historical_90d": len(result["historical_90d"]),
                      "forward_observation": len(result["forward_observation"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
