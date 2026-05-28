"""
股票分析 HTTP 服务。默认使用本地 Ollama（免费），无需 API Key。

启动：python server.py  或  uvicorn server:app --reload --host 0.0.0.0 --port 8000
"""
# 最早执行：抑制 yfinance 拉取失败时的噪音（404、possibly delisted、timezone 等）
from config.yf_suppress import suppress_yf_noise
suppress_yf_noise()

import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Query, Body, WebSocket
from fastapi.responses import PlainTextResponse, HTMLResponse, FileResponse

# Report 进度：按 job_id 维护，避免并发请求互相覆盖
_report_progress: Dict[str, Dict[str, Any]] = {}
_latest_report_job_id: str = ""
_latest_report_snapshot: Dict[str, Any] = {}
_report_progress_lock = threading.Lock()

from agents.fundamental import analyze_fundamental
from agents.full_analysis import run_full_analysis, _to_json_safe
from agents.report_deep import run_one_ticker_deep_report
from agents.analysis_deep import (
    run_fundamental_deep,
    run_moat,
    run_peers,
    run_short,
    run_narrative,
    run_thesis,
    run_full_deep_combo,
)
from config.delisted import DELISTED_TICKERS
from config.tickers import (
    get_report_tickers,
    normalize_ticker,
    DEFAULT_REPORT_TOP_N,
    MARKET_US,
    MARKET_CN,
    MARKET_HK,
    POOL_NASDAQ100,
    POOL_SMALL_US,
    POOL_CSI300,
    POOL_SMALL_CN,
)
from data.analysis_memory import (
    get_outcome_summary,
    get_recent_analysis,
    record_analysis_run,
    record_report_run,
    update_analysis_outcomes,
)
from report.build_html import build_report_html
from llm import ask_llm


def _agent_clip(text: str, max_len: int = 1200) -> str:
    """Return a trimmed agent-friendly summary string."""
    s = (text or "").strip()
    if not s:
        return "—"
    return s if len(s) <= max_len else s[:max_len] + "…"


def _score_text(score: Any) -> str:
    try:
        if score is None or score == "":
            return "—"
        return f"{float(score):.1f}/10"
    except Exception:
        return str(score)


def _pick_non_empty(*values: Any, default: str = "—") -> str:
    for value in values:
        text = str(value or "").strip()
        if text and text != "—":
            return text
    return default


def _dedupe_texts(items: List[str], max_items: int = 3) -> List[str]:
    seen = set()
    result: List[str] = []
    for item in items:
        text = str(item or "").strip()
        if not text or text == "—":
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= max_items:
            break
    return result


def _format_numbered_lines(title: str, items: List[str]) -> List[str]:
    lines = [f"{title}："]
    for idx, item in enumerate(items, start=1):
        lines.append(f"{idx}. {item}")
    return lines


def _build_stock_brief(base: Dict[str, Any], deep_sections: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    ticker = base.get("ticker") or ""
    name = base.get("name") or ticker
    action = base.get("action") or "观察"
    score = base.get("score")

    reasons = _dedupe_texts([
        base.get("core_conclusion"),
        base.get("analysis_reason"),
        base.get("trend_structure"),
        base.get("score_reason"),
    ])
    risks = _dedupe_texts([
        base.get("tech_exit_note"),
        base.get("data_quality_note"),
        base.get("macd_status"),
        base.get("kdj_status"),
    ])
    suggestions = _dedupe_texts([
        base.get("tech_entry_note"),
        f"加仓参考：{base.get('add_price')}" if base.get("add_price") and base.get("add_price") != "—" else "",
        f"减仓参考：{base.get('reduce_price')}" if base.get("reduce_price") and base.get("reduce_price") != "—" else "",
    ])

    lines = [
        f"{name}（{ticker}）评分：{_score_text(score)}",
        f"结论：{action}",
        "",
    ]
    lines.extend(_format_numbered_lines("买入/关注理由", reasons or ["当前结论不足，建议先观察。"]))
    lines.append("")
    lines.extend(_format_numbered_lines("主要风险", risks or ["暂无明确额外风险提示。"]))
    lines.append("")
    lines.extend(_format_numbered_lines("操作建议", suggestions or ["暂时等待更明确的入场信号。"]))

    deep_summary = {}
    if deep_sections:
        for key, value in deep_sections.items():
            deep_summary[key] = _agent_clip(value, 500)

    if deep_summary:
        lines.append("")
        lines.append("深度补充：")
        for key, value in deep_summary.items():
            lines.append(f"- {key}: {value}")

    return {
        "ticker": ticker,
        "name": name,
        "score": score,
        "score_text": _score_text(score),
        "action": action,
        "reasons": reasons,
        "risks": risks,
        "suggestions": suggestions,
        "summary_text": "\n".join(lines).strip(),
        "deep_summary": deep_summary,
    }


def _store_latest_report_snapshot(snapshot: Dict[str, Any]) -> None:
    global _latest_report_snapshot
    with _report_progress_lock:
        _latest_report_snapshot = snapshot


def _build_report_snapshot(
    *,
    title: str,
    cards: List[Dict[str, Any]],
    report_path: str = "",
    job_id: str = "",
    market: str = "",
    pool: str = "",
    deep: int = 0,
    interval: str = "1d",
    prepost: int = 0,
    errors: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sorted_cards = sorted(cards, key=lambda c: float(c.get("score") or 0), reverse=True)
    top_picks = []
    for card in sorted_cards[:3]:
        top_picks.append({
            "ticker": card.get("ticker") or "",
            "name": card.get("name") or card.get("ticker") or "",
            "score": card.get("score"),
            "score_text": _score_text(card.get("score")),
            "action": card.get("action") or "观察",
            "core_conclusion": _agent_clip(card.get("core_conclusion") or "—", 180),
        })

    action_counts: Dict[str, int] = {}
    for card in cards:
        action = (card.get("action") or "观察").strip() or "观察"
        action_counts[action] = action_counts.get(action, 0) + 1

    lines = [
        f"{title} 已生成，共 {len(cards)} 只标的。",
        f"参数：market={market or 'us'} pool={pool or 'default'} interval={interval} deep={deep} prepost={prepost}",
    ]
    if top_picks:
        lines.append("")
        lines.append("重点关注：")
        for idx, pick in enumerate(top_picks, start=1):
            lines.append(
                f"{idx}. {pick['name']}（{pick['ticker']}）评分 {pick['score_text']}，{pick['action']}：{pick['core_conclusion']}"
            )
    if action_counts:
        lines.append("")
        lines.append(
            "动作分布：" + "，".join(f"{action} {count} 只" for action, count in sorted(action_counts.items()))
        )
    if report_path:
        lines.append("")
        lines.append(f"本地报告文件：{report_path}")
    if errors:
        err_preview = [f"{item.get('ticker')}: {item.get('error')}" for item in errors[:3]]
        if err_preview:
            lines.append("")
            lines.append("异常/跳过：")
            lines.extend(f"- {item}" for item in err_preview)

    return {
        "generated_at": generated_at,
        "title": title,
        "job_id": job_id,
        "market": market,
        "pool": pool,
        "deep": deep,
        "interval": interval,
        "prepost": prepost,
        "report_path": report_path,
        "card_count": len(cards),
        "top_picks": top_picks,
        "action_counts": action_counts,
        "errors": errors or [],
        "summary_text": "\n".join(lines).strip(),
    }


def _analysis_memory_meta(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    recorded = [r for r in results if r.get("recorded")]
    return {
        "recorded_count": len(recorded),
        "run_ids": [r.get("run_id") for r in recorded if r.get("run_id")][:20],
        "db_path": next((r.get("db_path") for r in recorded if r.get("db_path")), ""),
        "mem0_synced_count": sum(1 for r in recorded if r.get("mem0_status") == "synced"),
        "mem0_failed_count": sum(1 for r in recorded if r.get("mem0_status") == "failed"),
        "mem0_skipped_count": sum(1 for r in recorded if r.get("mem0_status") == "skipped"),
    }


def _record_analysis_memory_safely(
    card: Dict[str, Any],
    *,
    brief: Optional[Dict[str, Any]] = None,
    source: str,
    mode: str,
    request: Dict[str, Any],
) -> Dict[str, Any]:
    try:
        return record_analysis_run(
            card,
            brief=brief,
            source=source,
            mode=mode,
            request=request,
        )
    except Exception as e:
        print(f"[AnalysisMemory] 单股记录失败: {e}", flush=True)
        return {"recorded": False, "error": str(e)[:500]}


def _record_report_memory_safely(
    snapshot: Dict[str, Any],
    cards: List[Dict[str, Any]],
    *,
    request: Dict[str, Any],
) -> Dict[str, Any]:
    try:
        return _analysis_memory_meta(record_report_run(snapshot, cards, request=request))
    except Exception as e:
        print(f"[AnalysisMemory] 报告记录失败: {e}", flush=True)
        return {"recorded_count": 0, "error": str(e)[:500]}


def _normalize_interval(interval: str) -> str:
    """yfinance 无 10m，用 15m 代替；展示时仍可写 10 分钟 K。"""
    if (interval or "").strip().lower() == "10m":
        return "15m"
    return (interval or "1d").strip().lower()


def _run_report_impl(
    ticker_list: List[str],
    interval: str,
    deep: int,
    market: str,
    prepost: int,
    job_id: str,
    pool: str = "",
) -> tuple:
    """内部：跑报告循环，返回 (cards, title, html_content)。interval 可为 10m（内部用 15m）。"""
    interval_internal = _normalize_interval(interval)
    total = len(ticker_list)
    with _report_progress_lock:
        _report_progress[job_id] = {
            "job_id": job_id,
            "running": True,
            "current_index": 0,
            "total": total,
            "current_ticker": "",
            "done_count": 0,
            "errors": [],
        }
    print(f"[Report] 开始: 共 {total} 只", flush=True)
    cards: List[Dict[str, Any]] = []
    backtest_summary_prev: Dict[str, Any] = {}
    try:
        from data.recommendations import get_past_recommendations_with_returns
        _, backtest_summary_prev = get_past_recommendations_with_returns(since_days=90)
    except Exception:
        pass
    try:
        for i, t in enumerate(ticker_list):
            with _report_progress_lock:
                progress = _report_progress.get(job_id)
                if progress is not None:
                    progress["current_index"] = i + 1
                    progress["current_ticker"] = t
            print(f"[Report] [{i + 1}/{total}] 正在处理: {t}", flush=True)
            t0 = time.time()
            try:
                if deep == 1:
                    one = run_one_ticker_deep_report(
                        t,
                        include_narrative=True,
                        interval=interval_internal,
                        include_prepost=(prepost == 1),
                        backtest_summary=backtest_summary_prev,
                    )
                else:
                    one = run_full_analysis(t, interval=interval_internal, include_prepost=(prepost == 1), backtest_summary=backtest_summary_prev)
                elapsed = time.time() - t0
                if one:
                    cards.append(one)
                    with _report_progress_lock:
                        progress = _report_progress.get(job_id)
                        if progress is not None:
                            progress["done_count"] = len(cards)
                    print(f"[Report] [{i + 1}/{total}] 完成: {t} (已成功 {len(cards)} 只) 耗时 {elapsed:.1f}s", flush=True)
                else:
                    print(f"[Report] [{i + 1}/{total}] 跳过: {t} (无数据) 耗时 {elapsed:.1f}s", flush=True)
                    with _report_progress_lock:
                        progress = _report_progress.get(job_id)
                        if progress is not None:
                            progress["errors"].append({"ticker": t, "error": "无数据"})
            except Exception as e:
                elapsed = time.time() - t0
                err_msg = str(e).strip() or type(e).__name__
                with _report_progress_lock:
                    progress = _report_progress.get(job_id)
                    if progress is not None:
                        progress["errors"].append({"ticker": t, "error": err_msg})
                print(f"[Report] [{i + 1}/{total}] 失败: {t} - {err_msg[:80]} 耗时 {elapsed:.1f}s", flush=True)
    finally:
        with _report_progress_lock:
            progress = _report_progress.get(job_id)
            if progress is not None:
                progress["running"] = False
                progress["current_ticker"] = ""
        n_ok, n_err = len(cards), total - len(cards)
        print(f"[Report] 结束: 成功 {n_ok} 只, 跳过/失败 {n_err} 只", flush=True)

    market_label = {"us": "美股", "cn": "A股", "hk": "港股"}.get((market or "us").strip().lower(), "美股")
    pool = (pool or "").strip().lower()
    pool_label = ""
    if pool == POOL_NASDAQ100:
        pool_label = "纳斯达克100"
    elif pool == POOL_SMALL_US:
        pool_label = "小盘/潜力股（罗素2000）"
    elif pool == POOL_CSI300:
        pool_label = "沪深300"
    elif pool == POOL_SMALL_CN:
        pool_label = "小盘/潜力股（中证2000）"
    prefix = f"{market_label}{pool_label}" if pool_label else market_label
    if deep == 1:
        title = f"{prefix}优秀资产分析（含深度分析与对比）"
    elif (interval or "").strip().lower() != "1d":
        k_label = {"5m": "5分钟K", "15m": "15分钟K", "10m": "10分钟K", "1m": "1分钟K"}.get(
            (interval or "").strip().lower(), f"{interval}K"
        )
        title = f"{prefix}超短线评分（{k_label}" + ("，含盘前盘后）" if prepost == 1 else "）")
    else:
        title = f"{prefix}选股分析"
    gen_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report_summary = None
    if cards:
        lines = []
        for c in cards:
            ticker = c.get("ticker") or ""
            name = c.get("name") or ticker
            score = c.get("score", 5)
            action = (c.get("action") or "观察").strip()
            core = (c.get("core_conclusion") or "").strip() or "—"
            lines.append(f"{name}({ticker}) 评分{score} {action}：{core[:120]}")
        text = "\n".join(lines)
        try:
            report_summary = ask_llm(
                system="你是投资报告助手。根据各标的的核心结论、评分、交易动作，生成简洁的报告总览。",
                user=f"""以下是本期报告各标的的核心结论、评分、交易动作（每行一只）。请用 3-5 句话概括本期要点，并指出优先关注的 1-3 只标的及简要理由。直接输出总览正文，不要标题或列表编号。

{text}"""
            )
            if report_summary:
                report_summary = report_summary.strip()
        except Exception as e:
            print(f"[Report] 报告总览 LLM 调用失败: {e}", flush=True)
    # 既往推荐追踪：记录本期 9/10 分且「买入」的标的（每份报告最多 3 条），并拉取过去 N 天推荐的表现与胜率
    report_date = gen_time[:10]
    try:
        from data.recommendations import save_recommendation, get_past_recommendations_with_returns, is_sideways_market
        from config.analysis_config import RECOMMEND_SIDEWAYS_MIN_SCORE
        sideways = is_sideways_market(lookback_days=20)
        min_score_override = float(RECOMMEND_SIDEWAYS_MIN_SCORE) if sideways else None
        buy_9_10 = [c for c in cards if (c.get("score") or 0) >= 9 and (c.get("action") or "").strip() == "买入"]
        for c in buy_9_10[:3]:  # 每份报告最多记录 3 条
            save_recommendation(c, report_date, min_score_override=min_score_override)
        backtest_rows, backtest_summary = get_past_recommendations_with_returns(since_days=90)
    except Exception as e:
        print(f"[Report] 既往推荐追踪失败: {e}", flush=True)
        backtest_rows, backtest_summary = [], {}
    html_content = build_report_html(
        cards, title=title, gen_time=gen_time, report_summary=report_summary,
        backtest_rows=backtest_rows, backtest_summary=backtest_summary,
    )
    # 可选：将本期报告卡片同步写入 RAG 向量库
    try:
        from rag.config import RAG_SYNC_CARDS
        from rag.build_index import build_index_from_cards
        if RAG_SYNC_CARDS and cards:
            build_index_from_cards(cards)
    except Exception as e:
        print(f"[Report] RAG 同步卡片失败: {e}", flush=True)
    return cards, title, html_content


app = FastAPI(title="Stock Agent", description="美股基本面分析（默认本地 Ollama）")


def _seconds_until_8am() -> float:
    """计算距离下一个 8:00 的秒数（本地时间）。"""
    now = datetime.now()
    today_8am = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if now >= today_8am:
        today_8am += timedelta(days=1)
    return (today_8am - now).total_seconds()


def _run_daily_report_job() -> None:
    """执行每日报告脚本（子进程，复用 scripts/daily_report.py 逻辑）。"""
    root = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(root, "scripts", "daily_report.py")
    if not os.path.isfile(script):
        print("[DailyReport] 未找到 scripts/daily_report.py，跳过", flush=True)
        return
    try:
        proc = subprocess.run(
            [sys.executable, script],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=21600,  # 3 份报告最多 6 小时
        )
        if proc.stdout:
            print(f"[DailyReport] stdout: {proc.stdout[:500]}", flush=True)
        if proc.stderr:
            print(f"[DailyReport] stderr: {proc.stderr[:500]}", flush=True)
        if proc.returncode != 0:
            print(f"[DailyReport] 退出码 {proc.returncode}", flush=True)
        else:
            print("[DailyReport] 完成", flush=True)
    except subprocess.TimeoutExpired:
        print("[DailyReport] 超时", flush=True)
    except Exception as e:
        print(f"[DailyReport] 异常: {e}", flush=True)


def _daily_report_scheduler_loop() -> None:
    """后台线程：每天 8 点执行 daily_report.py，跨平台不依赖 crontab。"""
    while True:
        secs = _seconds_until_8am()
        print(f"[DailyReport] 下次执行: {datetime.now() + timedelta(seconds=secs)}", flush=True)
        time.sleep(secs)
        print("[DailyReport] 开始执行", flush=True)
        _run_daily_report_job()


@app.on_event("startup")
def _startup_daily_scheduler() -> None:
    """启动内置 8 点定时任务（纯 Python，不依赖 crontab）。设 DAILY_REPORT_SCHEDULE=0 可关闭。"""
    if os.environ.get("DAILY_REPORT_SCHEDULE", "1").strip() != "0":
        t = threading.Thread(target=_daily_report_scheduler_loop, daemon=True)
        t.start()
        print("[DailyReport] 已启用内置定时任务（每天 8:00）", flush=True)


@app.websocket("/socketcluster/")
async def websocket_socketcluster(websocket: WebSocket):
    """兼容浏览器扩展等对 /socketcluster/ 的 WebSocket 请求，接受后立即关闭，避免 403 刷屏。"""
    await websocket.accept()
    await websocket.close()


@app.get("/")
def root():
    return {
        "service": "stock-agent",
        "docs": "/docs",
        "health": "/health",
        "report_page": "GET /report/page 报告在线页：打开后点「生成报告」即可在此页看到进度与结果，无需复制到浏览器",
        "analyze": "/analyze?ticker=AAPL",
        "report": "/report?limit=5&market=us（美股）或 market=cn（A股）或 market=hk（港股）；pool=nasdaq100/csi300/csi2000/russell2000；?tickers=600519.SS,0700.HK 可混用",
        "report_progress": "GET /report/progress 轮询查看报告生成进度（当前第几只、成功数、失败列表）",
        "agent_memory": "GET /agent/memory/history?ticker=AAPL 查看分析记忆；POST /agent/memory/update-outcomes 补回测收益；GET /agent/memory/outcomes 查看胜率摘要",
        "深度分析（6 类）": {
            "1_基本面深度": "GET /analyze/deep?ticker=AAPL",
            "2_护城河": "GET /analyze/moat?ticker=AAPL",
            "3_同行对比": "GET /analyze/peers?ticker=AAPL&peers=MSFT,GOOGL",
            "4_空头视角": "GET /analyze/short?ticker=AAPL",
            "5_叙事变化": "GET /analyze/narrative?ticker=AAPL",
            "6_假设拆解": "POST /analyze/thesis  body: { ticker, hypothesis }",
            "组合(①②③④)": "GET /analyze/full?ticker=AAPL&narrative=1 可选",
        },
        "长期上下文(LangChain)": "GET /memory?ticker=AAPL&type=fundamental_deep  GET /memory/context?ticker=AAPL",
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/analyze", response_class=PlainTextResponse)
def analyze_ticker(ticker: str = Query(..., description="股票代码，如 AAPL、MSFT")):
    """原有接口：简短基本面分析（文本）。"""
    try:
        result = analyze_fundamental(ticker.upper())
        return result or "(无输出)"
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# —————— 6 类深度分析（融合你的 Prompt） ——————

@app.get("/analyze/deep", response_class=PlainTextResponse)
def analyze_deep(ticker: str = Query(..., description="股票代码")):
    """① 基本面深度分析（主力）：收入与增长质量、盈利能力、现金流、商业模式、中长期风险。"""
    try:
        return run_fundamental_deep(ticker.upper()) or "(无输出)"
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/analyze/moat", response_class=PlainTextResponse)
def analyze_moat(ticker: str = Query(..., description="股票代码")):
    """② 护城河 & 竞争优势：技术/切换成本/网络/规模/品牌壁垒，强/中/弱/无及削弱路径。"""
    try:
        return run_moat(ticker.upper()) or "(无输出)"
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/analyze/peers", response_class=PlainTextResponse)
def analyze_peers(
    ticker: str = Query(..., description="股票代码"),
    peers: Optional[str] = Query(None, description="逗号分隔同行代码，不传则按行业推断"),
):
    """③ 同行业横向对比：增速/盈利/商业模式/估值差异，高估/合理/低估原因及市场可能看错之处。"""
    try:
        return run_peers(ticker.upper(), peers=peers) or "(无输出)"
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/analyze/short", response_class=PlainTextResponse)
def analyze_short(ticker: str = Query(..., description="股票代码")):
    """④ 空头 / Devil's Advocate：增长可持续性、替代风险、依赖度、估值、下跌触发点。"""
    try:
        return run_short(ticker.upper()) or "(无输出)"
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/analyze/narrative", response_class=PlainTextResponse)
def analyze_narrative(ticker: str = Query(..., description="股票代码")):
    """⑤ 财报 & 管理层话术变化：叙事变化摘要、正面信号、需警惕信号。"""
    try:
        return run_narrative(ticker.upper()) or "(无输出)"
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/analyze/thesis", response_class=PlainTextResponse)
def analyze_thesis(
    ticker: str = Body(..., embed=True),
    hypothesis: str = Body(..., embed=True),
):
    """⑥ 投资假设拆解：关键前提、最易证伪的前提、假设失败的最可能原因。"""
    try:
        return run_thesis((ticker or "").upper().strip(), hypothesis or "") or "(无输出)"
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/external-data")
def get_external_data(
    ticker: str = Query(..., description="股票代码，如 AAPL、0700.HK"),
    period: str = Query("6mo", description="历史数据周期，默认 6mo"),
    interval: str = Query("1d", description="K线周期：1d=日K"),
    max_news: int = Query(10, ge=1, le=30, description="新闻条数"),
):
    """
    获取股票分析工作流所需的外部数据 JSON 模板格式。
    供下游项目直接消费，无需再调用外部 API。
    返回结构：stock_code, market_type, stock_data, historical_data, financial_data, news_data, options_data。
    """
    try:
        from agents.external_data_fetcher import fetch_external_data_json
        return fetch_external_data_json(
            ticker=ticker.upper().strip(),
            period=period,
            interval=interval,
            max_news=max_news,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/analyze/full")
def analyze_full_deep(
    ticker: str = Query(..., description="股票代码"),
    narrative: int = Query(0, description="1=包含⑤叙事分析，0=仅①②③④"),
):
    """实战组合：① 基本面 → ② 护城河 → ③ 同行对比 → ④ 空头；可选 ⑤ 叙事。返回 JSON 各段标题与正文。"""
    try:
        result = run_full_deep_combo(ticker.upper(), include_narrative=(narrative == 1))
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/agent/analyze")
def agent_analyze(
    ticker: str = Query(..., description="股票代码"),
    deep: int = Query(1, description="1=返回深度分析，0=仅基础综合分析"),
    narrative: int = Query(1, description="1=深度模式包含叙事分析，0=不包含"),
    interval: str = Query("1d", description="K线周期，默认日K"),
    prepost: int = Query(0, description="1=包含盘前盘后，0=不包含"),
):
    """
    给 AI Agent 使用的结构化单股分析接口。
    - deep=0: 返回 run_full_analysis 的核心结构化结果
    - deep=1: 在基础结果上追加 ①②③④(+⑤) 深度分析结果与摘要
    """
    t = (ticker or "").upper().strip()
    if not t:
        raise HTTPException(status_code=400, detail="ticker 不能为空")
    try:
        base = run_full_analysis(
            t,
            interval=_normalize_interval(interval),
            include_prepost=(prepost == 1),
        )
        if not base:
            raise HTTPException(status_code=404, detail=f"未获取到 {t} 的分析结果")

        response: Dict[str, Any] = {
            "ticker": t,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "mode": "deep" if deep == 1 else "basic",
            "card": base,
            "agent_summary": {
                "name": base.get("name") or t,
                "action": base.get("action") or "观察",
                "score": base.get("score"),
                "score_reason": base.get("score_reason") or "—",
                "core_conclusion": base.get("core_conclusion") or "—",
                "analysis_reason": base.get("analysis_reason") or "—",
                "trend_structure": base.get("trend_structure") or "—",
                "risk_controls": {
                    "add_price": base.get("add_price") or "—",
                    "reduce_price": base.get("reduce_price") or "—",
                    "tech_entry_note": base.get("tech_entry_note") or "—",
                    "tech_exit_note": base.get("tech_exit_note") or "—",
                },
            },
        }

        if deep == 1:
            deep_sections = run_full_deep_combo(t, include_narrative=(narrative == 1))
            response["deep_sections"] = deep_sections
            response["deep_section_summaries"] = {
                key: _agent_clip(value, 900)
                for key, value in deep_sections.items()
            }

        response["analysis_memory"] = _record_analysis_memory_safely(
            base,
            source="agent_analyze",
            mode=response["mode"],
            request={
                "ticker": t,
                "deep": deep,
                "narrative": narrative,
                "interval": interval,
                "prepost": prepost,
            },
        )
        return _to_json_safe(response)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/agent/analyze/brief")
def agent_analyze_brief(
    ticker: str = Query(..., description="股票代码"),
    deep: int = Query(1, description="1=返回深度摘要，0=仅基础综合分析"),
    narrative: int = Query(1, description="1=深度模式包含叙事分析，0=不包含"),
    interval: str = Query("1d", description="K线周期，默认日K"),
    prepost: int = Query(0, description="1=包含盘前盘后，0=不包含"),
):
    """给聊天机器人/飞书展示的人类可读单股摘要接口。"""
    payload = agent_analyze(
        ticker=ticker,
        deep=deep,
        narrative=narrative,
        interval=interval,
        prepost=prepost,
    )
    base = payload.get("card") or {}
    deep_sections = payload.get("deep_section_summaries") if deep == 1 else None
    brief = _build_stock_brief(base, deep_sections=deep_sections)
    return _to_json_safe({
        "ticker": payload.get("ticker"),
        "generated_at": payload.get("generated_at"),
        "mode": payload.get("mode"),
        "brief": brief,
        "agent_summary": payload.get("agent_summary") or {},
        "analysis_memory": payload.get("analysis_memory") or {},
    })


# —————— 长期上下文（LangChain memory_store） ——————
try:
    from chains.memory_store import retrieve, get_context_summary
except Exception:
    retrieve = lambda ticker=None, analysis_type=None, last_n=2: []
    get_context_summary = lambda ticker=None, analysis_type=None: ""


@app.get("/memory")
def memory_retrieve(
    ticker: str = Query(..., description="股票代码"),
    analysis_type: Optional[str] = Query(None, description="分析类型：fundamental_deep / moat / peers / short / narrative / thesis，不传则返回该标的全部"),
    last_n: int = Query(2, ge=1, le=10, description="每种类型最多返回条数"),
):
    """检索历史分析结果（长期上下文）。分析结果在跑 /analyze/deep 等接口时自动写入。"""
    try:
        records = retrieve(ticker.upper().strip(), analysis_type=analysis_type, last_n=last_n)
        return {"ticker": ticker, "records": records}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/memory/context", response_class=PlainTextResponse)
def memory_context(
    ticker: str = Query(..., description="股票代码"),
    analysis_type: Optional[str] = Query(None, description="分析类型，不传则返回最近一次任意类型"),
):
    """获取「上次分析」摘要，可用于拼接到新分析的 prompt 中做对比。"""
    try:
        return get_context_summary(ticker.upper().strip(), analysis_type=analysis_type) or "（无历史分析）"
    except Exception:
        return "（无历史分析）"


@app.get("/report/page", response_class=HTMLResponse)
def report_console_page():
    """
    报告在线页：打开此页后选择参数点击「生成报告」，页面会轮询进度并在此页直接展示报告 HTML，
    无需手动复制到浏览器。后续可部署到 Cloudflare 等。
    """
    console_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report", "console.html")
    if os.path.isfile(console_path):
        return FileResponse(console_path, media_type="text/html; charset=utf-8")
    raise HTTPException(status_code=404, detail="report/console.html not found")


@app.get("/report/progress")
def report_progress(job_id: Optional[str] = Query(None, description="报告任务 ID；不传则返回最近一次任务的进度")):
    """
    报告生成进度（轮询此接口可知当前执行到哪一只）。
    running: 是否正在跑；current_index/total: 第几只/共几只；current_ticker: 当前标的；done_count: 已成功数；errors: 失败列表 [{ticker, error}, ...]。
    """
    with _report_progress_lock:
        target_job_id = (job_id or "").strip() or _latest_report_job_id
        if not target_job_id:
            return {"job_id": "", "running": False, "current_index": 0, "total": 0, "current_ticker": "", "done_count": 0, "errors": []}
        progress = _report_progress.get(target_job_id)
        if progress is None:
            raise HTTPException(status_code=404, detail=f"job_id not found: {target_job_id}")
        return dict(progress)


@app.get("/report", response_class=HTMLResponse)
def report_page(
    tickers: str = Query(None, description="逗号分隔股票代码；A股可传 6 位（自动补 .SZ/.SS），港股可传 4/5 位（5 位只去第一位补 .HK，如 00100→0100.HK）；不传则按 market+pool 取池"),
    limit: int = Query(5, ge=1, le=200, description="当不传 tickers 时取的数量，默认 5（调试快；可传 100 跑全量）"),
    deep: int = Query(0, description="1=每只标的跑深度分析①②③④⑤+与上次对比，形成大方向/近期趋势；0=仅技术+消息+财报+期权"),
    interval: str = Query("1d", description="K线周期：1d=日K，5m/15m/10m/1m=分K（10m 用 15m 数据）"),
    prepost: int = Query(0, description="是否含盘前盘后：0=否，1=是（分K时常用）"),
    market: str = Query("us", description="市场选股：us=美股，cn=A股，hk=港股（不传 tickers 时生效）"),
    pool: str = Query("", description="选股池：不传或 sp500=大盘；nasdaq100=纳斯达克100；russell2000=美股小盘；csi300=A股沪深300；csi2000=A股中证2000；hsi=恒指；hstech=恒科，不传 tickers 时生效"),
    save_output: int = Query(1, description="1=将报告 HTML 保存到 report/output/；0=不保存（前端页面触发时传 0）"),
    job_id: Optional[str] = Query(None, description="报告任务 ID；前端轮询进度时建议传固定值"),
):
    """
    多市场选股报告：美股（S&P 500 / 罗素2000）/ A股（龙头 / 中证2000）/ 港股。
    deep=0：每只仅做技术面+消息面+财报+期权+一次 LLM 综合（快）。
    deep=1：每只额外跑 ①②③④⑤ 深度分析（仅日K），结合记忆做「与上次对比」。
    market=us/cn/hk：不传 tickers 时从对应市场池取前 limit 只。
    pool=sp500（默认）/ nasdaq100 / russell2000（美股小盘）/ csi300（A股沪深300）/ csi2000（A股中证2000）：不传 tickers 时生效。
    interval=1d：日K；interval=5m/15m/10m/1m：分K超短线（10m 以 15m 数据代替）。prepost=1：含盘前盘后。
    进度可轮询 GET /report/progress。
    """
    if tickers:
        ticker_list = [normalize_ticker(t) for t in tickers.split(",") if t.strip()][:200]
    else:
        ticker_list = get_report_tickers(limit=limit, market=market or MARKET_US, pool=pool or None)
    ticker_list = [t for t in ticker_list if t not in DELISTED_TICKERS]
    if not ticker_list:
        raise HTTPException(status_code=400, detail="请提供 tickers 或使用默认列表（limit>0）")
    report_job_id = ((job_id or "").strip() or uuid.uuid4().hex)
    global _latest_report_job_id
    with _report_progress_lock:
        _latest_report_job_id = report_job_id
    cards, title, html_content = _run_report_impl(
        ticker_list,
        interval,
        deep,
        market,
        prepost,
        job_id=report_job_id,
        pool=pool or "",
    )
    # 仅当 save_output=1 时保存到 report/output/（前端页面触发时传 save_output=0 不写盘）
    if save_output == 1:
        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report", "output")
        os.makedirs(out_dir, exist_ok=True)
        ts = datetime.now().strftime("%m%d-%H%M%S")
        out_path = os.path.join(out_dir, f"report-{ts}-{report_job_id[:8]}.html")
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(html_content)
            print(f"[Report] 已保存: {out_path}", flush=True)
        except Exception as e:
            print(f"[Report] 保存文件失败: {e}", flush=True)
    else:
        out_path = ""

    with _report_progress_lock:
        progress = dict(_report_progress.get(report_job_id) or {})
    snapshot = _build_report_snapshot(
        title=title,
        cards=cards,
        report_path=out_path,
        job_id=report_job_id,
        market=market,
        pool=pool or "",
        deep=deep,
        interval=interval,
        prepost=prepost,
        errors=progress.get("errors") or [],
    )
    snapshot["analysis_memory"] = _record_report_memory_safely(
        snapshot,
        cards,
        request={
            "tickers": tickers,
            "limit": limit,
            "deep": deep,
            "interval": interval,
            "prepost": prepost,
            "market": market,
            "pool": pool or "",
            "save_output": save_output,
            "job_id": report_job_id,
        },
    )
    _store_latest_report_snapshot(snapshot)
    return HTMLResponse(content=html_content)


@app.get("/agent/report")
def agent_report(
    tickers: str = Query(None, description="逗号分隔股票代码；不传则按 market+pool 取池"),
    limit: int = Query(5, ge=1, le=200, description="默认 5"),
    deep: int = Query(0, description="1=每只标的跑深度分析"),
    interval: str = Query("1d", description="K线周期"),
    prepost: int = Query(0, description="是否含盘前盘后"),
    market: str = Query("us", description="市场：us/cn/hk"),
    pool: str = Query("", description="选股池"),
    save_output: int = Query(1, description="1=保存 HTML 到 report/output/"),
    job_id: Optional[str] = Query(None, description="可传固定任务 ID"),
):
    """触发报告并返回适合 Agent/飞书消费的结构化摘要。"""
    if tickers:
        ticker_list = [normalize_ticker(t) for t in tickers.split(",") if t.strip()][:200]
    else:
        ticker_list = get_report_tickers(limit=limit, market=market or MARKET_US, pool=pool or None)
    ticker_list = [t for t in ticker_list if t not in DELISTED_TICKERS]
    if not ticker_list:
        raise HTTPException(status_code=400, detail="请提供 tickers 或使用默认列表（limit>0）")

    report_job_id = ((job_id or "").strip() or uuid.uuid4().hex)
    global _latest_report_job_id
    with _report_progress_lock:
        _latest_report_job_id = report_job_id

    cards, title, html_content = _run_report_impl(
        ticker_list,
        interval,
        deep,
        market,
        prepost,
        job_id=report_job_id,
        pool=pool or "",
    )

    out_path = ""
    if save_output == 1:
        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report", "output")
        os.makedirs(out_dir, exist_ok=True)
        ts = datetime.now().strftime("%m%d-%H%M%S")
        out_path = os.path.join(out_dir, f"report-{ts}-{report_job_id[:8]}.html")
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(html_content)
            print(f"[AgentReport] 已保存: {out_path}", flush=True)
        except Exception as e:
            print(f"[AgentReport] 保存文件失败: {e}", flush=True)
            out_path = ""

    with _report_progress_lock:
        progress = dict(_report_progress.get(report_job_id) or {})
    snapshot = _build_report_snapshot(
        title=title,
        cards=cards,
        report_path=out_path,
        job_id=report_job_id,
        market=market,
        pool=pool or "",
        deep=deep,
        interval=interval,
        prepost=prepost,
        errors=progress.get("errors") or [],
    )
    snapshot["analysis_memory"] = _record_report_memory_safely(
        snapshot,
        cards,
        request={
            "tickers": tickers,
            "limit": limit,
            "deep": deep,
            "interval": interval,
            "prepost": prepost,
            "market": market,
            "pool": pool or "",
            "save_output": save_output,
            "job_id": report_job_id,
        },
    )
    _store_latest_report_snapshot(snapshot)
    return _to_json_safe(snapshot)


@app.get("/agent/report/latest")
def agent_report_latest():
    """返回最近一次报告摘要，供聊天机器人直接读取。"""
    with _report_progress_lock:
        snapshot = dict(_latest_report_snapshot or {})
    if not snapshot:
        raise HTTPException(status_code=404, detail="暂无最近报告摘要")
    return _to_json_safe(snapshot)


@app.get("/agent/memory/history")
def agent_memory_history(
    ticker: str = Query("", description="股票代码；不传则返回最近所有标的"),
    limit: int = Query(20, ge=1, le=200, description="返回条数"),
    include_payload: int = Query(0, description="1=包含原始 card/request，0=只返回摘要字段"),
):
    """查看本地分析记忆，供 Hermes/飞书回看历史判断与 run_id。"""
    try:
        records = get_recent_analysis(
            ticker=ticker,
            limit=limit,
            include_payload=(include_payload == 1),
        )
        return _to_json_safe({
            "ticker": (ticker or "").upper().strip(),
            "count": len(records),
            "records": records,
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/agent/memory/outcomes")
def agent_memory_outcomes(
    ticker: str = Query("", description="股票代码；不传则统计全部"),
    since_days: int = Query(180, ge=1, le=2000, description="统计最近多少天"),
):
    """查看已补齐回测收益后的胜率/平均收益摘要。"""
    try:
        return _to_json_safe(get_outcome_summary(ticker=ticker, since_days=since_days))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/agent/memory/update-outcomes")
@app.get("/agent/memory/update-outcomes")
def agent_memory_update_outcomes(
    max_runs: int = Query(200, ge=1, le=2000, description="最多更新最近多少条分析记录"),
    horizons: str = Query("1,3,5,10,20", description="逗号分隔持有天数，如 1,3,5,10,20"),
):
    """按历史价格补齐已记录分析的未来收益，用于每天收盘后复盘。"""
    try:
        parsed_horizons = []
        for item in (horizons or "").split(","):
            item = item.strip()
            if not item:
                continue
            parsed_horizons.append(int(item))
        result = update_analysis_outcomes(
            max_runs=max_runs,
            horizons=parsed_horizons or (1, 3, 5, 10, 20),
        )
        return _to_json_safe(result)
    except ValueError:
        raise HTTPException(status_code=400, detail="horizons 需要是逗号分隔整数，例如 1,3,5,10,20")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
