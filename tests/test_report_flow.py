"""/report 关键流程回归测试。"""
from fastapi.testclient import TestClient

import agents.report_deep as report_deep
import server


def _use_temp_analysis_memory(monkeypatch, tmp_path):
    monkeypatch.setenv("STOCK_AGENT_ANALYSIS_MEMORY_DB", str(tmp_path / "analysis_memory.sqlite"))
    monkeypatch.setenv("STOCK_AGENT_MEM0_SYNC", "0")


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


def test_report_progress_reads_by_job_id(monkeypatch, tmp_path):
    _use_temp_analysis_memory(monkeypatch, tmp_path)
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


def test_agent_analyze_brief_returns_readable_summary(monkeypatch, tmp_path):
    _use_temp_analysis_memory(monkeypatch, tmp_path)
    client = TestClient(server.app)

    def fake_run_full_analysis(ticker, interval="1d", include_prepost=False, backtest_summary=None):
        return {
            "ticker": ticker,
            "name": "Monster Beverage",
            "score": 8,
            "action": "买入",
            "core_conclusion": "趋势偏强，资金面配合。",
            "analysis_reason": "基本面稳定，品牌优势明显。",
            "trend_structure": "日线多头排列。",
            "score_reason": "综合得分较高。",
            "tech_entry_note": "回踩均线可留意。",
            "tech_exit_note": "跌破 MA20 需谨慎。",
        }

    monkeypatch.setattr(server, "run_full_analysis", fake_run_full_analysis)
    monkeypatch.setattr(server, "run_full_deep_combo", lambda ticker, include_narrative=True: {"moat": "品牌壁垒较强"})

    response = client.get("/agent/analyze/brief", params={"ticker": "MNST", "deep": 1})
    assert response.status_code == 200

    payload = response.json()
    assert payload["brief"]["score_text"] == "8.0/10"
    assert "Monster Beverage" in payload["brief"]["summary_text"]
    assert "买入/关注理由" in payload["brief"]["summary_text"]
    assert "主要风险" in payload["brief"]["summary_text"]
    assert payload["analysis_memory"]["recorded"] is True
    assert payload["analysis_memory"]["run_id"]

    history = client.get("/agent/memory/history", params={"ticker": "MNST", "limit": 5})
    assert history.status_code == 200
    history_payload = history.json()
    assert history_payload["count"] == 1
    assert history_payload["records"][0]["ticker"] == "MNST"
    assert history_payload["records"][0]["action"] == "买入"


def test_agent_report_returns_summary_and_latest_snapshot(monkeypatch, tmp_path):
    _use_temp_analysis_memory(monkeypatch, tmp_path)
    client = TestClient(server.app)

    def fake_get_report_tickers(limit, market, pool):
        return ["NVDA", "MSFT"]

    def fake_run_report_impl(ticker_list, interval, deep, market, prepost, job_id, pool=""):
        with server._report_progress_lock:
            server._report_progress[job_id] = {
                "job_id": job_id,
                "running": False,
                "current_index": len(ticker_list),
                "total": len(ticker_list),
                "current_ticker": "",
                "done_count": len(ticker_list),
                "errors": [],
            }
        cards = [
            {"ticker": "NVDA", "name": "NVIDIA", "score": 9, "action": "买入", "core_conclusion": "AI 主线维持强势。"},
            {"ticker": "MSFT", "name": "Microsoft", "score": 8, "action": "观察", "core_conclusion": "基本面稳健，等待更好位置。"},
        ]
        return cards, "美股纳指报告", "<html>ok</html>"

    monkeypatch.setattr(server, "get_report_tickers", fake_get_report_tickers)
    monkeypatch.setattr(server, "_run_report_impl", fake_run_report_impl)

    response = client.get("/agent/report", params={"market": "us", "pool": "nasdaq100", "limit": 2, "save_output": 0})
    assert response.status_code == 200
    payload = response.json()
    assert payload["card_count"] == 2
    assert "重点关注" in payload["summary_text"]
    assert payload["top_picks"][0]["ticker"] == "NVDA"
    assert payload["analysis_memory"]["recorded_count"] == 2
    assert len(payload["analysis_memory"]["run_ids"]) == 2

    latest = client.get("/agent/report/latest")
    assert latest.status_code == 200
    assert latest.json()["job_id"] == payload["job_id"]
    assert latest.json()["analysis_memory"]["recorded_count"] == 2


def test_agent_memory_update_outcomes_parses_horizons(monkeypatch, tmp_path):
    _use_temp_analysis_memory(monkeypatch, tmp_path)
    client = TestClient(server.app)

    captured = {}

    def fake_update_analysis_outcomes(max_runs=200, horizons=(1, 3, 5, 10, 20)):
        captured["max_runs"] = max_runs
        captured["horizons"] = tuple(horizons)
        return {"updated": 3, "skipped": 1, "errors": [], "horizons": list(horizons)}

    monkeypatch.setattr(server, "update_analysis_outcomes", fake_update_analysis_outcomes)

    response = client.get("/agent/memory/update-outcomes", params={"max_runs": 50, "horizons": "1,5,20"})
    assert response.status_code == 200
    assert response.json()["updated"] == 3
    assert captured == {"max_runs": 50, "horizons": (1, 5, 20)}
