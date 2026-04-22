"""/report 关键流程回归测试。"""
from fastapi.testclient import TestClient

import agents.report_deep as report_deep
import server


def test_run_one_ticker_deep_report_passes_interval_and_prepost(monkeypatch):
    captured = {}

    def fake_run_full_analysis(ticker, interval="1d", include_prepost=False, backtest_summary=None):
        captured["ticker"] = ticker
        captured["interval"] = interval
        captured["include_prepost"] = include_prepost
        return {"ticker": ticker, "score": 8}

    monkeypatch.setattr(report_deep, "run_full_analysis", fake_run_full_analysis)
    monkeypatch.setattr(report_deep, "_USE_CHAINS", False)

    card = report_deep.run_one_ticker_deep_report(
        "AAPL",
        interval="15m",
        include_prepost=True,
        backtest_summary={"recent_win_rate_pct": 60},
    )

    assert card is not None
    assert captured == {"ticker": "AAPL", "interval": "15m", "include_prepost": True}


def test_report_progress_reads_by_job_id(monkeypatch):
    client = TestClient(server.app)

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

    monkeypatch.setattr(server, "get_report_tickers", fake_get_report_tickers)
    monkeypatch.setattr(server, "_run_report_impl", fake_run_report_impl)

    job_id = "job-test-123"
    response = client.get("/report", params={"market": "us", "limit": 1, "save_output": 0, "job_id": job_id})
    assert response.status_code == 200

    progress = client.get("/report/progress", params={"job_id": job_id})
    assert progress.status_code == 200
    assert progress.json()["job_id"] == job_id
    assert progress.json()["done_count"] == 1
