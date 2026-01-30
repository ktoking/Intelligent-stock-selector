"""
6 类深度分析执行：拉取数据 → 组装 Prompt → 调用 LLM。
优先使用 LangChain 链（多步骤编排 + 长期上下文）；不可用时回退到 ask_llm。
实战组合：① 基本面 → ② 护城河 → ③ 同行对比 → ④ 空头 → （可选）⑤ 叙事、⑥ 假设拆解。
"""
import os
from typing import Optional, Dict, Any, List

import yfinance as yf
from llm import ask_llm

from agents.prompts import (
    build_fundamental_deep,
    build_moat,
    build_peers,
    build_short,
    build_narrative,
    build_thesis,
)
from agents.news import get_news_summary

# 是否使用 LangChain 链（编排 + 长期上下文）；未装 langchain 时自动回退
_USE_LANGCHAIN = os.environ.get("LANGCHAIN", "1").strip() == "1"
try:
    from chains.chains import (
        chain_fundamental_deep as _lc_fundamental,
        chain_moat as _lc_moat,
        chain_peers as _lc_peers,
        chain_short as _lc_short,
        chain_narrative as _lc_narrative,
        chain_thesis as _lc_thesis,
        chain_full_deep as _lc_full_deep,
    )
    _LANGCHAIN_AVAILABLE = True
except Exception:
    _LANGCHAIN_AVAILABLE = False


def _get_stock_data(ticker: str) -> tuple:
    """拉取财务、info、季度摘要、新闻，供各分析使用。"""
    stock = yf.Ticker(ticker)
    info = stock.info or {}
    try:
        financials = stock.financials
        financials_str = financials.to_string() if financials is not None and not financials.empty else "无"
    except Exception:
        financials_str = "无"
    try:
        quarterly = stock.quarterly_financials
        quarterly_str = quarterly.to_string() if quarterly is not None and not quarterly.empty else "无"
    except Exception:
        quarterly_str = "无"
    # company_info 摘要
    company_info = (
        f"名称: {info.get('longName') or info.get('shortName') or ticker}\n"
        f"行业: {info.get('industry')}  板块: {info.get('sector')}\n"
        f"市值: {info.get('marketCap')}  当前PE: {info.get('trailingPE')}  预期PE: {info.get('forwardPE')}\n"
        f"毛利率: {info.get('grossMargins')}  营收增长: {info.get('revenueGrowth')}\n"
        f"简介: {(info.get('longBusinessSummary') or '')[:500]}"
    )
    return financials_str, company_info, quarterly_str, info


def _get_peers_list(ticker: str, info: dict) -> str:
    """同行列表：优先用传入/配置，否则按行业从内置池取若干。"""
    # 可扩展：从 API 或配置读取 peers
    sector = (info.get("sector") or "").strip()
    industry = (info.get("industry") or "").strip()
    # 简单按行业给常见同行（示例，可改为从 universe 按 sector 筛）
    sector_peers = {
        "Technology": "MSFT, GOOGL, META, NVDA, AMD, INTC, QCOM, AVGO, CRM, ORCL, ADBE",
        "Consumer Cyclical": "AMZN, TSLA, HD, NKE, MCD, SBUX, TGT, LOW",
        "Healthcare": "JNJ, UNH, PFE, ABBV, TMO, ABT, LLY, MRK, BMY, AMGN",
        "Financial Services": "JPM, BAC, WFC, GS, MS, C, AXP, BLK, SCHW",
        "Communication Services": "GOOGL, META, DIS, NFLX, CMCSA",
    }
    sk, ik = (sector or "").lower(), (industry or "").lower()
    for k, v in sector_peers.items():
        if k.lower() in sk or k.lower() in ik:
            return v
    return "（未配置同行，请基于行业常识分析或传入 peers 参数）"


def run_fundamental_deep(ticker: str) -> str:
    """① 基本面深度分析（主力）。"""
    ticker = ticker.upper().strip()
    if _USE_LANGCHAIN and _LANGCHAIN_AVAILABLE:
        return _lc_fundamental({"ticker": ticker})
    financials_str, company_info, _, _ = _get_stock_data(ticker)
    prompt = build_fundamental_deep(ticker, financials_str, company_info)
    return ask_llm(
        system="你是偏保守的长期美股基本面分析师，不给出买卖建议，只帮助理解公司真实状况。严格按用户给出的结构输出。",
        user=prompt,
    )


def run_moat(ticker: str) -> str:
    """② 护城河 & 竞争优势分析。"""
    ticker = ticker.upper().strip()
    if _USE_LANGCHAIN and _LANGCHAIN_AVAILABLE:
        return _lc_moat({"ticker": ticker})
    financials_str, company_info, _, _ = _get_stock_data(ticker)
    prompt = build_moat(ticker, company_info, financials_str)
    return ask_llm(
        system="你是研究企业护城河的投资分析师。每一项明确判断强/中/弱/无，说明被削弱路径，避免空泛。",
        user=prompt,
    )


def run_peers(ticker: str, peers: Optional[str] = None) -> str:
    """③ 同行业横向对比。peers 不传则按行业从内置池推断。"""
    ticker = ticker.upper().strip()
    if _USE_LANGCHAIN and _LANGCHAIN_AVAILABLE:
        return _lc_peers({"ticker": ticker, "peers": peers})
    financials_str, company_info, _, info = _get_stock_data(ticker)
    peer_list = peers or _get_peers_list(ticker, info)
    prompt = build_peers(ticker, peer_list, company_info, financials_str)
    return ask_llm(
        system="你是卖方分析师，做同行对比。重点关注增速、盈利、商业模式、估值差异，并回答高估/合理/低估原因及市场可能看错之处。",
        user=prompt,
    )


def run_short(ticker: str) -> str:
    """④ 空头 / Devil's Advocate。"""
    ticker = ticker.upper().strip()
    if _USE_LANGCHAIN and _LANGCHAIN_AVAILABLE:
        return _lc_short({"ticker": ticker})
    financials_str, company_info, _, _ = _get_stock_data(ticker)
    prompt = build_short(ticker, company_info, financials_str)
    return ask_llm(
        system="你是空头研究员，只找潜在问题和风险。不重复多头观点，只列有逻辑链条的风险。",
        user=prompt,
    )


def run_narrative(ticker: str) -> str:
    """⑤ 财报 & 管理层话术变化分析。"""
    ticker = ticker.upper().strip()
    if _USE_LANGCHAIN and _LANGCHAIN_AVAILABLE:
        return _lc_narrative({"ticker": ticker})
    _, company_info, quarterly_str, _ = _get_stock_data(ticker)
    news = get_news_summary(ticker)
    news_summary = "\n".join(
        f"- {n.get('title', '')} ({n.get('published', '')})"
        for n in (news.get("news") or [])[:8]
    )
    prompt = build_narrative(ticker, quarterly_str, news_summary)
    return ask_llm(
        system="你擅长从财报与披露中识别管理层叙事变化。输出叙事变化摘要、正面信号、需警惕信号。",
        user=prompt,
    )


def run_thesis(ticker: str, hypothesis: str) -> str:
    """⑥ 投资假设拆解。"""
    ticker = ticker.upper().strip()
    if _USE_LANGCHAIN and _LANGCHAIN_AVAILABLE:
        return _lc_thesis({"ticker": ticker, "hypothesis": hypothesis})
    _, company_info, _, _ = _get_stock_data(ticker)
    prompt = build_thesis(ticker, hypothesis, company_info)
    return ask_llm(
        system="你协助拆解投资假设：列出关键前提、最易证伪的前提、假设失败的最可能原因。",
        user=prompt,
    )


def run_full_deep_combo(ticker: str, include_narrative: bool = False) -> Dict[str, str]:
    """
    实战组合：① 基本面 → ② 护城河 → ③ 同行对比 → ④ 空头；可选 ⑤ 叙事。
    使用 LangChain 时结果会写入长期上下文（memory_store），便于后续检索对比。
    """
    ticker = ticker.upper().strip()
    if _USE_LANGCHAIN and _LANGCHAIN_AVAILABLE:
        return _lc_full_deep(ticker, include_narrative=include_narrative, use_memory=True)
    out = {}
    try:
        out["1_基本面深度"] = run_fundamental_deep(ticker)
    except Exception as e:
        out["1_基本面深度"] = f"[分析异常] {e}"
    try:
        out["2_护城河与竞争优势"] = run_moat(ticker)
    except Exception as e:
        out["2_护城河与竞争优势"] = f"[分析异常] {e}"
    try:
        out["3_同行业横向对比"] = run_peers(ticker)
    except Exception as e:
        out["3_同行业横向对比"] = f"[分析异常] {e}"
    try:
        out["4_空头视角"] = run_short(ticker)
    except Exception as e:
        out["4_空头视角"] = f"[分析异常] {e}"
    if include_narrative:
        try:
            out["5_财报与叙事变化"] = run_narrative(ticker)
        except Exception as e:
            out["5_财报与叙事变化"] = f"[分析异常] {e}"
    return out
