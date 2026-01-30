"""
股票分析 HTTP 服务。默认使用本地 Ollama（免费），无需 API Key。

启动：python server.py  或  uvicorn server:app --reload --host 0.0.0.0 --port 8000
"""
import warnings
warnings.filterwarnings("ignore", module="urllib3")

import threading
from datetime import datetime
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Query, Body
from fastapi.responses import PlainTextResponse, HTMLResponse

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
from config.tickers import get_report_tickers, DEFAULT_REPORT_TOP_N, MARKET_US, MARKET_CN, MARKET_HK
from report.build_html import build_report_html

app = FastAPI(title="Stock Agent", description="美股基本面分析（默认本地 Ollama）")


@app.get("/")
def root():
    return {
        "service": "stock-agent",
        "docs": "/docs",
        "health": "/health",
        "analyze": "/analyze?ticker=AAPL",
        "report": "/report?limit=5&market=us（美股）或 market=cn（A股）或 market=hk（港股）；?tickers=600519.SS,0700.HK 可混用",
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
    tickers: str = Query(None, description="逗号分隔股票代码，不传则按市值+近期增长从 S&P 500 池取前 N 只"),
    limit: int = Query(5, ge=1, le=200, description="当不传 tickers 时取的数量，默认 5（调试快；可传 100 跑全量）"),
    deep: int = Query(0, description="1=每只标的跑深度分析①②③④⑤+与上次对比，形成大方向/近期趋势；0=仅技术+消息+财报+期权"),
    interval: str = Query("1d", description="K线周期：1d=日K（波段），5m/15m/1m=分K（超短线）"),
    prepost: int = Query(0, description="是否含盘前盘后：0=否，1=是（分K时常用）"),
    market: str = Query("us", description="市场选股：us=美股，cn=A股，hk=港股（不传 tickers 时生效）"),
):
    """
    多市场选股报告：美股（S&P 500 池）/ A股 / 港股。
    deep=0：每只仅做技术面+消息面+财报+期权+一次 LLM 综合（快）。
    deep=1：每只额外跑 ①②③④⑤ 深度分析（仅日K），结合记忆做「与上次对比」。
    market=us/cn/hk：不传 tickers 时从对应市场池取前 limit 只。
    interval=1d：日K；interval=5m/15m：分K超短线。prepost=1：含盘前盘后。
    进度可轮询 GET /report/progress。
    """
    if tickers:
        ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()][:200]
    else:
        ticker_list = get_report_tickers(limit=limit, market=market or MARKET_US)
    if not ticker_list:
        raise HTTPException(status_code=400, detail="请提供 tickers 或使用默认列表（limit>0）")

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
            try:
                if deep == 1:
                    one = run_one_ticker_deep_report(t, include_narrative=True)
                else:
                    one = run_full_analysis(t, interval=interval, include_prepost=(prepost == 1))
                if one:
                    cards.append(one)
                    with _report_progress_lock:
                        _report_progress["done_count"] = len(cards)
                    print(f"[Report] [{i + 1}/{total}] 完成: {t} (已成功 {len(cards)} 只)", flush=True)
                else:
                    print(f"[Report] [{i + 1}/{total}] 跳过: {t} (无数据)", flush=True)
                    with _report_progress_lock:
                        _report_progress["errors"].append({"ticker": t, "error": "无数据"})
            except Exception as e:
                err_msg = str(e).strip() or type(e).__name__
                with _report_progress_lock:
                    _report_progress["errors"].append({"ticker": t, "error": err_msg})
                print(f"[Report] [{i + 1}/{total}] 失败: {t} - {err_msg[:80]}", flush=True)
    finally:
        with _report_progress_lock:
            _report_progress["running"] = False
            _report_progress["current_ticker"] = ""
        n_ok, n_err = len(cards), total - len(cards)
        print(f"[Report] 结束: 成功 {n_ok} 只, 跳过/失败 {n_err} 只", flush=True)

    market_label = {"us": "美股", "cn": "A股", "hk": "港股"}.get((market or "us").strip().lower(), "美股")
    if deep == 1:
        title = f"{market_label}优秀资产分析（含深度分析与对比）"
    elif interval != "1d":
        k_label = {"5m": "5分钟K", "15m": "15分钟K", "1m": "1分钟K"}.get(interval, f"{interval}K")
        title = f"{market_label}超短线评分（{k_label}" + ("，含盘前盘后）" if prepost == 1 else "）")
    else:
        title = f"{market_label}选股分析"
    gen_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html_content = build_report_html(cards, title=title, gen_time=gen_time)
    return HTMLResponse(content=html_content)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
