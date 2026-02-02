"""
Report 深度模式：对单只标的跑「技术+消息+财报+期权」卡片 + 「①②③④⑤ 深度分析」+ 「与上次对比」。
返回一张「富卡片」dict，含原有卡片字段 + 深度摘要 + 大方向是否一致、近期趋势。
深度开启时，会基于五段深度摘要对 full_analysis 的评分做一次轻量 LLM 微调。
"""
import json
import re
import time
from typing import Optional, Dict, Any

from agents.full_analysis import run_full_analysis
from llm import ask_llm

# 可选 LangChain 链与记忆
try:
    from chains.chains import chain_full_deep, run_comparison
    from chains.memory_store import retrieve
    _USE_CHAINS = True
except Exception as _e:
    _USE_CHAINS = False
    chain_full_deep = None
    run_comparison = None
    retrieve = None
    _CHAINS_IMPORT_ERROR = str(_e)[:150]


# 深度摘要展示长度：卡片内展示用，便于在报告里看全结构（###、** 等）
DEEP_SUMMARY_MAX_LEN = 800
# 喂给「评分微调」LLM 的每段摘要长度，控制 token
DEEP_SCORE_MICRO_SUMMARY_LEN = 200


def _short_summary(text: str, max_len: int = 120) -> str:
    """取前几句作为摘要；max_len 大时保留换行供 HTML 格式化。"""
    if not text or not text.strip():
        return "—"
    s = text.strip()
    if len(s) > max_len:
        s = s[:max_len] + "…"
    return s


def _adjust_score_by_deep(ticker: str, original_score: float, deep_results: Dict[str, Any]) -> Optional[float]:
    """
    基于五段深度摘要，用一次轻量 LLM 对评分做微调（10=最强，1=最弱）。
    返回微调后的分数 [1, 10]，失败或无深度内容时返回 None（调用方保留原分）。
    """
    if not deep_results:
        return None
    labels = [
        ("1_基本面深度", "①基本面深度"),
        ("2_护城河与竞争优势", "②护城河"),
        ("3_同行业横向对比", "③同行对比"),
        ("4_空头视角", "④空头视角"),
        ("5_财报与叙事变化", "⑤叙事变化"),
    ]
    parts = []
    for key, label in labels:
        raw = deep_results.get(key) or ""
        s = (raw.strip()[:DEEP_SCORE_MICRO_SUMMARY_LEN] + "…") if len((raw or "").strip()) > DEEP_SCORE_MICRO_SUMMARY_LEN else (raw or "").strip()
        if s:
            parts.append(f"【{label}】\n{s}")
    if not parts:
        return None
    text = "\n\n".join(parts)
    try:
        orig = max(1, min(10, float(original_score)))
    except (TypeError, ValueError):
        orig = 5
    system = "你是股票分析师。根据下面五段深度摘要，对「当前评分」做微调。只输出一行，且仅以下两种格式之一：\n最终评分：N（N为1-10的整数）\n或\n调整：+1 或 调整：0 或 调整：-1"
    user = f"标的：{ticker}\n当前评分：{orig}（10=最强，1=最弱）\n\n请根据以下深度摘要微调评分（风险明显则下调，机会/护城河突出可上调）：\n\n{text}"
    try:
        raw = ask_llm(system=system, user=user, max_tokens=80)
    except Exception:
        return None
    raw = (raw or "").strip()
    # 解析「最终评分：N」
    m = re.search(r"最终评分[：:]\s*(\d+)", raw)
    if m:
        try:
            n = int(m.group(1))
            return max(1, min(10, n))
        except (ValueError, IndexError):
            pass
    # 解析「调整：+1 / 0 / -1」
    m = re.search(r"调整[：:]\s*([+-]?\d+)", raw)
    if m:
        try:
            delta = int(m.group(1))
            return max(1, min(10, orig + delta))
        except (ValueError, IndexError):
            pass
    return None


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
    print(f"[Report] {ticker} 综合分析（技术+消息+财报+LLM）开始…", flush=True)
    t0 = time.time()
    try:
        card = run_full_analysis(ticker)
    except Exception as e:
        print(f"[Report] {ticker} 综合分析异常: {e}", flush=True)
        card = None
    if not card:
        return None
    print(f"[Report] {ticker} 综合分析完成 耗时 {time.time() - t0:.1f}s", flush=True)

    deep_results = {}
    past_deep = None
    comparison = {"direction_unchanged": True, "reason": "无历史对比", "recent_trend": "—"}
    card["deep_disabled_reason"] = None
    card["deep_error"] = None

    if _USE_CHAINS and chain_full_deep and run_comparison and retrieve:
        try:
            # 2) 深度分析 ①②③④⑤，并会写入 full_deep_run（5 个子分析各调一次 LLM，耗时会较长）
            print(f"[Report] {ticker} 深度分析（①②③④⑤）开始…", flush=True)
            t_deep = time.time()
            deep_results = chain_full_deep(ticker, peers=peers, include_narrative=include_narrative, use_memory=True)
            print(f"[Report] {ticker} 深度分析完成 耗时 {time.time() - t_deep:.1f}s", flush=True)
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
            # 4.5) 轻量 LLM 微调评分：基于五段深度摘要对 full_analysis 的评分做微调
            if deep_results:
                try:
                    orig = card.get("score")
                    if orig is not None:
                        print(f"[Report] {ticker} 评分微调中（原分 {orig}）…", flush=True)
                        t_adj = time.time()
                        new_score = _adjust_score_by_deep(ticker, orig, deep_results)
                        if new_score is not None:
                            card["score"] = new_score
                            card["score_adjusted_by_deep"] = True
                            print(f"[Report] {ticker} 评分微调完成 -> {new_score} 耗时 {time.time() - t_adj:.1f}s", flush=True)
                        else:
                            print(f"[Report] {ticker} 评分微调未返回新分，保留原分", flush=True)
                except Exception as e:
                    print(f"[Report] {ticker} 评分微调异常: {e}", flush=True)
                    pass  # 微调失败则保留原分
        except Exception as e:
            card["deep_error"] = str(e).strip()[:200]
            print(f"[Report] 深度分析失败 {ticker}: {card['deep_error']}", flush=True)
    else:
        card["deep_disabled_reason"] = (
            _CHAINS_IMPORT_ERROR if not _USE_CHAINS else "LangChain 链未就绪"
        )

    # 5) 合并：卡片基础 + 深度摘要（每段取前 DEEP_SUMMARY_MAX_LEN 字，保留换行供 HTML 格式化）+ 对比
    card["fundamental_deep_summary"] = _short_summary(deep_results.get("1_基本面深度", ""), DEEP_SUMMARY_MAX_LEN)
    card["moat_summary"] = _short_summary(deep_results.get("2_护城河与竞争优势", ""), DEEP_SUMMARY_MAX_LEN)
    card["peers_summary"] = _short_summary(deep_results.get("3_同行业横向对比", ""), DEEP_SUMMARY_MAX_LEN)
    card["short_summary"] = _short_summary(deep_results.get("4_空头视角", ""), DEEP_SUMMARY_MAX_LEN)
    card["narrative_summary"] = _short_summary(deep_results.get("5_财报与叙事变化", ""), DEEP_SUMMARY_MAX_LEN)
    card["direction_unchanged"] = comparison.get("direction_unchanged", True)
    card["comparison_reason"] = comparison.get("reason", "—") or "—"
    card["recent_trend"] = comparison.get("recent_trend", "—") or "—"
    return card
