#!/usr/bin/env python3
"""/report 关键路径最小自测脚本。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

import agents.report_deep as report_deep
import server


def _check_deep_passthrough() -> None:
    captured = {}
    original_run_full_analysis = report_deep.run_full_analysis
    original_use_chains = report_deep._USE_CHAINS

    def fake_run_full_analysis(ticker, interval="1d", include_prepost=False, backtest_summary=None):
        captured["ticker"] = ticker
        captured["interval"] = interval
        captured["include_prepost"] = include_prepost
        return {"ticker": ticker, "score": 8}

    report_deep.run_full_analysis = fake_run_full_analysis
    report_deep._USE_CHAINS = False
    try:
        card = report_deep.run_one_ticker_deep_report(
            "AAPL",
            interval="15m",
            include_prepost=True,
            backtest_summary={"recent_win_rate_pct": 60},
        )
    finally:
        report_deep.run_full_analysis = original_run_full_analysis
        report_deep._USE_CHAINS = original_use_chains

    assert card is not None, "deep report should return a card"
    assert captured == {
        "ticker": "AAPL",
        "interval": "15m",
        "include_prepost": True,
    }, f"deep passthrough mismatch: {captured!r}"


def _check_progress_by_job_id() -> None:
    client = TestClient(server.app)
    original_get_report_tickers = server.get_report_tickers
    original_run_report_impl = server._run_report_impl

    def fake_get_report_tickers(limit, market, pool):
        return ["AAPL"]

    def fake_run_report_impl(ticker_list, interval, deep, market, prepost, job_id, pool=""):
        with server._report_progress_lock:
            server._report_progress[job_id] = {
                "job_id": job_id,
                "running": False,
                "current_index": 1,
                "total": 1,
                "current_ticker": "",
                "done_count": 1,
                "errors": [],
            }
        return [], "demo", "<html>ok</html>"

    server.get_report_tickers = fake_get_report_tickers
    server._run_report_impl = fake_run_report_impl
    try:
        job_id = "selftest-job-123"
        report_resp = client.get("/report", params={"market": "us", "limit": 1, "save_output": 0, "job_id": job_id})
        assert report_resp.status_code == 200, f"/report failed: {report_resp.status_code}"
        progress_resp = client.get("/report/progress", params={"job_id": job_id})
        assert progress_resp.status_code == 200, f"/report/progress failed: {progress_resp.status_code}"
        payload = progress_resp.json()
    finally:
        server.get_report_tickers = original_get_report_tickers
        server._run_report_impl = original_run_report_impl

    assert payload["job_id"] == job_id, f"unexpected job_id: {payload!r}"
    assert payload["done_count"] == 1, f"unexpected progress payload: {payload!r}"


def main() -> int:
    _check_deep_passthrough()
    _check_progress_by_job_id()
    print("selftest_report: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
