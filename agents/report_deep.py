"""
Report 深度模式：对单只标的跑「技术+消息+财报+期权」卡片 + 「①②③④⑤ 深度分析」+ 「与上次对比」。
返回一张「富卡片」dict，含原有卡片字段 + 深度摘要 + 大方向是否一致、近期趋势。
"""
import json
from typing import Optional, Dict, Any

from agents.full_analysis import run_full_analysis

# 可选 LangChain 链与记忆
try:
    from chains.chains import chain_full_deep, run_comparison
    from chains.memory_store import retrieve
    _USE_CHAINS = True
except Exception:
    _USE_CHAINS = False
    chain_full_deep = None
    run_comparison = None
    retrieve = None


def _short_summary(text: str, max_len: int = 120) -> str:
    """取前几句作为摘要，避免卡片过长。"""
    if not text or not text.strip():
        return "—"
    s = text.strip().replace("\n", " ")[:max_len]
    if len(text.strip()) > max_len:
        s += "…"
    return s


def run_one_ticker_deep_report(
    ticker: str,
    peers: Optional[str] = None,
    include_narrative: bool = True,
) -> Optional[Dict[str, Any]]:
    """
    对单只标的：1) 跑 full_analysis 得卡片基础数据；2) 跑深度分析 ①②③④⑤；3) 取上次 full_deep_run；4) 跑对比得大方向/近期趋势；5) 合并为富卡片。
    """
    ticker = (ticker or "").upper().strip()
    if not ticker:
        return None
    # 1) 卡片基础（技术+消息+财报+期权 + 一次 LLM 评分/趋势/动作）
    try:
        card = run_full_analysis(ticker)
    except Exception:
        card = None
    if not card:
        return None

    deep_results = {}
    past_deep = None
    comparison = {"direction_unchanged": True, "reason": "无历史对比", "recent_trend": "—"}

    if _USE_CHAINS and chain_full_deep and run_comparison and retrieve:
        try:
            # 2) 深度分析 ①②③④⑤，并会写入 full_deep_run
            deep_results = chain_full_deep(ticker, peers=peers, include_narrative=include_narrative, use_memory=True)
            # 3) 取「上次」full_deep_run（在本次 save 之前的一次）
            records = retrieve(ticker, analysis_type="full_deep_run", last_n=2)
            # 第一条是刚存的本次，第二条才是上次
            if len(records) >= 2:
                try:
                    past_deep = json.loads(records[1].get("content", "{}"))
                    if "ts" in past_deep:
                        past_deep = {k: v for k, v in past_deep.items() if k != "ts"}
                except Exception:
                    past_deep = None
            # 4) 对比
            comparison = run_comparison(ticker, deep_results, past_deep)
        except Exception:
            pass

    # 5) 合并：卡片基础 + 深度摘要（每段取前 120 字）+ 对比
    card["fundamental_deep_summary"] = _short_summary(deep_results.get("1_基本面深度", ""))
    card["moat_summary"] = _short_summary(deep_results.get("2_护城河与竞争优势", ""))
    card["peers_summary"] = _short_summary(deep_results.get("3_同行业横向对比", ""))
    card["short_summary"] = _short_summary(deep_results.get("4_空头视角", ""))
    card["narrative_summary"] = _short_summary(deep_results.get("5_财报与叙事变化", ""))
    card["direction_unchanged"] = comparison.get("direction_unchanged", True)
    card["comparison_reason"] = comparison.get("reason", "—") or "—"
    card["recent_trend"] = comparison.get("recent_trend", "—") or "—"
    return card
