#!/usr/bin/env python3
"""
Minimal MCP server for local stock-agent access.

This server is intentionally thin: it calls the local stock-agent HTTP service
and returns structured JSON results that Hermes can use directly.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from mcp.server.fastmcp import FastMCP


DEFAULT_BASE_URL = os.environ.get("STOCK_AGENT_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
DEFAULT_TIMEOUT_SECONDS = float(os.environ.get("STOCK_AGENT_TIMEOUT_SECONDS", "180"))

server = FastMCP("stock-agent")


def _http_get_json(path: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    query = urlencode({k: v for k, v in (params or {}).items() if v is not None})
    url = f"{DEFAULT_BASE_URL}{path}"
    if query:
        url = f"{url}?{query}"
    req = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(req, timeout=DEFAULT_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")
        except Exception:
            body = str(e)
        raise RuntimeError(f"HTTP {e.code} from {url}: {body[:1200]}") from e
    except URLError as e:
        raise RuntimeError(f"Failed to reach stock-agent at {url}: {e}") from e


@server.tool(
    description="Check whether the local stock-agent HTTP service is healthy and reachable."
)
def stock_agent_health() -> Dict[str, Any]:
    return _http_get_json("/health")


@server.tool(
    description=(
        "Analyze a single ticker with the local stock-agent service. "
        "Use this for requests like '分析MNST' or '分析NVDA'. "
        "Returns structured JSON including score, action, summary, and optional deep sections."
    )
)
def stock_agent_analyze(
    ticker: str,
    deep: bool = True,
    narrative: bool = True,
    interval: str = "1d",
    prepost: bool = False,
) -> Dict[str, Any]:
    ticker = (ticker or "").upper().strip()
    if not ticker:
        raise RuntimeError("ticker is required")
    return _http_get_json(
        "/agent/analyze",
        {
            "ticker": ticker,
            "deep": 1 if deep else 0,
            "narrative": 1 if narrative else 0,
            "interval": interval,
            "prepost": 1 if prepost else 0,
        },
    )


@server.tool(
    description=(
        "Analyze a single ticker and return a human-readable Chinese brief with score, "
        "risks, reasons, and action. Prefer this for chat replies to users."
    )
)
def stock_agent_analyze_brief(
    ticker: str,
    deep: bool = False,
    narrative: bool = True,
    interval: str = "1d",
    prepost: bool = False,
) -> Dict[str, Any]:
    ticker = (ticker or "").upper().strip()
    if not ticker:
        raise RuntimeError("ticker is required")
    return _http_get_json(
        "/agent/analyze/brief",
        {
            "ticker": ticker,
            "deep": 1 if deep else 0,
            "narrative": 1 if narrative else 0,
            "interval": interval,
            "prepost": 1 if prepost else 0,
        },
    )


@server.tool(
    description=(
        "Run a stock-agent report and return a structured summary suitable for chat replies. "
        "Defaults to Nasdaq daily mode: US market, nasdaq100 pool, limit 100, daily candles, "
        "non-deep analysis, and save_output enabled. Use this directly for prompts like "
        "'跑纳指 report'."
    )
)
def stock_agent_run_report(
    market: str = "us",
    pool: str = "nasdaq100",
    limit: int = 100,
    deep: bool = False,
    interval: str = "1d",
    prepost: bool = False,
    tickers: str = "",
    save_output: bool = True,
) -> Dict[str, Any]:
    return _http_get_json(
        "/agent/report",
        {
            "market": market,
            "pool": pool,
            "limit": limit,
            "deep": 1 if deep else 0,
            "interval": interval,
            "prepost": 1 if prepost else 0,
            "tickers": tickers or None,
            "save_output": 1 if save_output else 0,
        },
    )


@server.tool(
    description=(
        "Read the latest generated stock-agent report summary, including local file path "
        "and top picks."
    )
)
def stock_agent_latest_report_summary() -> Dict[str, Any]:
    return _http_get_json("/agent/report/latest")


@server.tool(
    description=(
        "Read local stock-agent analysis memory. Use this when the user asks for "
        "previous analysis, historical decisions, run ids, or what stock-agent "
        "remembered about a ticker."
    )
)
def stock_agent_analysis_history(
    ticker: str = "",
    limit: int = 10,
    include_payload: bool = False,
) -> Dict[str, Any]:
    return _http_get_json(
        "/agent/memory/history",
        {
            "ticker": (ticker or "").upper().strip() or None,
            "limit": limit,
            "include_payload": 1 if include_payload else 0,
        },
    )


@server.tool(
    description=(
        "Update local backtest outcomes for stored stock-agent analysis records. "
        "Use this after market close or before review to calculate 1/3/5/10/20-day returns."
    )
)
def stock_agent_update_outcomes(
    max_runs: int = 200,
    horizons: str = "1,3,5,10,20",
) -> Dict[str, Any]:
    return _http_get_json(
        "/agent/memory/update-outcomes",
        {
            "max_runs": max_runs,
            "horizons": horizons,
        },
    )


@server.tool(
    description=(
        "Summarize local stock-agent backtest outcomes and win rates. "
        "Use this when the user asks how prior analyses performed."
    )
)
def stock_agent_outcome_summary(
    ticker: str = "",
    since_days: int = 180,
) -> Dict[str, Any]:
    return _http_get_json(
        "/agent/memory/outcomes",
        {
            "ticker": (ticker or "").upper().strip() or None,
            "since_days": since_days,
        },
    )


if __name__ == "__main__":
    server.run("stdio")
