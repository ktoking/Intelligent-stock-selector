"""
股票分析 HTTP 服务。默认使用本地 Ollama（免费），无需 API Key。

启动：python server.py  或  uvicorn server:app --reload --host 0.0.0.0 --port 8000
"""
import warnings
warnings.filterwarnings("ignore", module="urllib3")

import os
import threading
import time
from datetime import datetime
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Query, Body, WebSocket
from fastapi.responses import PlainTextResponse, HTMLResponse, FileResponse

# Report 进度：单次运行期间可被 GET /report/progress 轮询
_report_progress: Dict[str, Any] = {"running": False, "current_index": 0, "total": 0, "current_ticker": "", "done_count": 0, "errors": []}
_report_progress_lock = threading.Lock()

from agents.fundamental import analyze_fundamental
from agents.full_analysis import run_full_analysis
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
from config.tickers import (
    get_report_tickers,
    normalize_ticker,
    DEFAULT_REPORT_TOP_N,
    MARKET_US,
    MARKET_CN,
    MARKET_HK,
    POOL_NASDAQ100,
    POOL_SMALL_US,
    POOL_SMALL_CN,
)
from report.build_html import build_report_html
from llm import ask_llm


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
    pool: str = "",
) -> tuple:
    """内部：跑报告循环，返回 (cards, title, html_content)。interval 可为 10m（内部用 15m）。"""
    interval_internal = _normalize_interval(interval)
    total = len(ticker_list)
    with _report_progress_lock:
        _report_progress["running"] = True
        _report_progress["total"] = total
        _report_progress["current_index"] = 0
        _report_progress["current_ticker"] = ""
        _report_progress["done_count"] = 0
        _report_progress["errors"] = []
    print(f"[Report] 开始: 共 {total} 只", flush=True)
    cards: List[Dict[str, Any]] = []
    try:
        for i, t in enumerate(ticker_list):
            with _report_progress_lock:
                _report_progress["current_index"] = i + 1
                _report_progress["current_ticker"] = t
            print(f"[Report] [{i + 1}/{total}] 正在处理: {t}", flush=True)
            t0 = time.time()
            try:
                if deep == 1:
                    one = run_one_ticker_deep_report(t, include_narrative=True)
                else:
                    one = run_full_analysis(t, interval=interval_internal, include_prepost=(prepost == 1))
                elapsed = time.time() - t0
                if one:
                    cards.append(one)
                    with _report_progress_lock:
                        _report_progress["done_count"] = len(cards)
                    print(f"[Report] [{i + 1}/{total}] 完成: {t} (已成功 {len(cards)} 只) 耗时 {elapsed:.1f}s", flush=True)
                else:
                    print(f"[Report] [{i + 1}/{total}] 跳过: {t} (无数据) 耗时 {elapsed:.1f}s", flush=True)
                    with _report_progress_lock:
                        _report_progress["errors"].append({"ticker": t, "error": "无数据"})
            except Exception as e:
                elapsed = time.time() - t0
                err_msg = str(e).strip() or type(e).__name__
                with _report_progress_lock:
                    _report_progress["errors"].append({"ticker": t, "error": err_msg})
                print(f"[Report] [{i + 1}/{total}] 失败: {t} - {err_msg[:80]} 耗时 {elapsed:.1f}s", flush=True)
    finally:
        with _report_progress_lock:
            _report_progress["running"] = False
            _report_progress["current_ticker"] = ""
        n_ok, n_err = len(cards), total - len(cards)
        print(f"[Report] 结束: 成功 {n_ok} 只, 跳过/失败 {n_err} 只", flush=True)

    market_label = {"us": "美股", "cn": "A股", "hk": "港股"}.get((market or "us").strip().lower(), "美股")
    pool = (pool or "").strip().lower()
    pool_label = ""
    if pool == POOL_NASDAQ100:
        pool_label = "纳斯达克100"
    elif pool == POOL_SMALL_US:
        pool_label = "小盘/潜力股（罗素2000）"
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
                user=f"""以下是本期报告各标的的核心结论、评分、交易动作（每行一只）。请用 3-5 句话概括本期要点，并指出优先关注的 1-3 只标的及简要理由。直接输出总览正文，不要标题或列表编号。

{text}"""
            )
            if report_summary:
                report_summary = report_summary.strip()
        except Exception as e:
            print(f"[Report] 报告总览 LLM 调用失败: {e}", flush=True)
    # 既往推荐追踪：记录本期 9/10 分且「买入」的标的，并拉取过去 N 天推荐的表现与胜率
    report_date = gen_time[:10]
    try:
        from data.recommendations import save_recommendation, get_past_recommendations_with_returns
        for c in cards:
            save_recommendation(c, report_date)
        backtest_rows, backtest_summary = get_past_recommendations_with_returns(since_days=30)
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
        "report": "/report?limit=5&market=us（美股）或 market=cn（A股）或 market=hk（港股）；pool=nasdaq100（纳斯达克100）/ russell2000（美股小盘）/ csi2000（A股小盘）；?tickers=600519.SS,0700.HK 可混用",
        "report_progress": "GET /report/progress 轮询查看报告生成进度（当前第几只、成功数、失败列表）",
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
def report_progress():
    """
    报告生成进度（轮询此接口可知当前执行到哪一只）。
    running: 是否正在跑；current_index/total: 第几只/共几只；current_ticker: 当前标的；done_count: 已成功数；errors: 失败列表 [{ticker, error}, ...]。
    """
    with _report_progress_lock:
        return dict(_report_progress)


@app.get("/report", response_class=HTMLResponse)
def report_page(
    tickers: str = Query(None, description="逗号分隔股票代码；A股可传 6 位如 001317,603767（自动补 .SZ/.SS），港股可传 4 位如 0700（自动补 .HK）；不传则按 market+pool 取池"),
    limit: int = Query(5, ge=1, le=200, description="当不传 tickers 时取的数量，默认 5（调试快；可传 100 跑全量）"),
    deep: int = Query(0, description="1=每只标的跑深度分析①②③④⑤+与上次对比，形成大方向/近期趋势；0=仅技术+消息+财报+期权"),
    interval: str = Query("1d", description="K线周期：1d=日K，5m/15m/10m/1m=分K（10m 用 15m 数据）"),
    prepost: int = Query(0, description="是否含盘前盘后：0=否，1=是（分K时常用）"),
    market: str = Query("us", description="市场选股：us=美股，cn=A股，hk=港股（不传 tickers 时生效）"),
    pool: str = Query("", description="选股池：不传或 sp500=大盘；nasdaq100=纳斯达克100；russell2000=美股小盘（罗素2000）；csi2000=A股小盘/潜力（中证2000），不传 tickers 时生效"),
    save_output: int = Query(1, description="1=将报告 HTML 保存到 report/output/；0=不保存（前端页面触发时传 0）"),
):
    """
    多市场选股报告：美股（S&P 500 / 罗素2000）/ A股（龙头 / 中证2000）/ 港股。
    deep=0：每只仅做技术面+消息面+财报+期权+一次 LLM 综合（快）。
    deep=1：每只额外跑 ①②③④⑤ 深度分析（仅日K），结合记忆做「与上次对比」。
    market=us/cn/hk：不传 tickers 时从对应市场池取前 limit 只。
    pool=sp500（默认）/ nasdaq100（纳斯达克100）/ russell2000（美股小盘）/ csi2000（A股小盘）：不传 tickers 时生效。
    interval=1d：日K；interval=5m/15m/10m/1m：分K超短线（10m 以 15m 数据代替）。prepost=1：含盘前盘后。
    进度可轮询 GET /report/progress。
    """
    if tickers:
        ticker_list = [normalize_ticker(t) for t in tickers.split(",") if t.strip()][:200]
    else:
        ticker_list = get_report_tickers(limit=limit, market=market or MARKET_US, pool=pool or None)
    if not ticker_list:
        raise HTTPException(status_code=400, detail="请提供 tickers 或使用默认列表（limit>0）")
    cards, title, html_content = _run_report_impl(ticker_list, interval, deep, market, prepost, pool=pool or "")
    # 仅当 save_output=1 时保存到 report/output/（前端页面触发时传 save_output=0 不写盘）
    if save_output == 1:
        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report", "output")
        os.makedirs(out_dir, exist_ok=True)
        ts = datetime.now().strftime("%m%d-%H%M")
        out_path = os.path.join(out_dir, f"report-{ts}.html")
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(html_content)
            print(f"[Report] 已保存: {out_path}", flush=True)
        except Exception as e:
            print(f"[Report] 保存文件失败: {e}", flush=True)
    return HTMLResponse(content=html_content)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
