"""
多步骤推理编排：LCEL 风格链（外部数据 → Prompt 填充 → LLM → 输出），可选长期上下文存储与检索。
深度分析 ①②③④⑤ 可并行执行以缩短总耗时。环境变量 DEEP_PARALLEL=0 可关闭并行。
"""
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, Optional

# 深度分析是否并行：默认 True；设为 0 则顺序执行（单实例 Ollama 排队时可用）
_DEEP_PARALLEL = os.environ.get("DEEP_PARALLEL", "1").strip().lower() not in ("0", "false", "no")

from langchain_core.messages import SystemMessage, HumanMessage
from chains.llm_factory import get_llm
from chains.data_fetchers import fetch_stock_data
from chains.memory_store import save, get_context_summary, retrieve
from agents.prompts import (
    build_fundamental_deep,
    build_moat,
    build_peers,
    build_short,
    build_narrative,
    build_thesis,
)


def _chain_one(system: str, builder_fn, save_type: str):
    """单步链：inputs → fetch_stock_data → builder_fn(data) → LLM → 存 memory → 返回文本。"""
    def run(inputs: dict) -> str:
        data = fetch_stock_data.invoke(inputs)
        user_text = builder_fn(data)
        out = get_llm().invoke([
            SystemMessage(content=system),
            HumanMessage(content=user_text),
        ]).content
        t = (inputs.get("ticker") or "").upper().strip()
        if t:
            save(t, save_type, out)
        return out
    return run


def _build_fundamental(data): return build_fundamental_deep(data["ticker"], data.get("financials", ""), data.get("company_info", ""))
def _build_moat(data): return build_moat(data["ticker"], data.get("company_info", ""), data.get("financials", ""))
def _build_peers(data): return build_peers(data["ticker"], data.get("peers", ""), data.get("company_info", ""), data.get("financials", ""))
def _build_short(data): return build_short(data["ticker"], data.get("company_info", ""), data.get("financials", ""))
def _build_narrative(data): return build_narrative(data["ticker"], data.get("quarterly_summary", ""), data.get("news_summary", ""))
def _build_thesis(data): return build_thesis(data["ticker"], data.get("hypothesis", ""), data.get("company_info", ""))


# ① 基本面深度
chain_fundamental_deep = _chain_one(
    "你是偏保守的长期美股基本面分析师，不给出买卖建议，只帮助理解公司真实状况。严格按用户给出的结构输出。",
    _build_fundamental,
    "fundamental_deep",
)

# ② 护城河
chain_moat = _chain_one(
    "你是研究企业护城河的投资分析师。每一项明确判断强/中/弱/无，说明被削弱路径，避免空泛。",
    _build_moat,
    "moat",
)

# ③ 同行对比
chain_peers = _chain_one(
    "你是卖方分析师，做同行对比。重点关注增速、盈利、商业模式、估值差异，并回答高估/合理/低估原因及市场可能看错之处。",
    _build_peers,
    "peers",
)

# ④ 空头
chain_short = _chain_one(
    "你是空头研究员，只找潜在问题和风险。不重复多头观点，只列有逻辑链条的风险。",
    _build_short,
    "short",
)

# ⑤ 叙事
chain_narrative = _chain_one(
    "你擅长从财报与披露中识别管理层叙事变化。输出叙事变化摘要、正面信号、需警惕信号。",
    _build_narrative,
    "narrative",
)


def chain_thesis(inputs: dict) -> str:
    """⑥ 假设拆解：inputs 需含 hypothesis。"""
    data = fetch_stock_data.invoke(inputs)
    user_text = _build_thesis(data)
    out = get_llm().invoke([
        SystemMessage(content="你协助拆解投资假设：列出关键前提、最易证伪的前提、假设失败的最可能原因。"),
        HumanMessage(content=user_text),
    ]).content
    t = (inputs.get("ticker") or "").upper().strip()
    if t:
        save(t, "thesis", out)
    return out


def _run_one_deep_step(label: str, chain_fn, inputs: dict, ticker: str, step_names: dict) -> tuple:
    """单步深度分析，返回 (label, result)，内部打印耗时。供并行调用。"""
    t0 = time.time()
    try:
        result = chain_fn(inputs)
    except Exception as e:
        result = f"[分析异常] {e}"
    elapsed = time.time() - t0
    print(f"[Report] {ticker} {step_names[label]} 耗时 {elapsed:.1f}s", flush=True)
    return (label, result)


def chain_full_deep(ticker: str, peers: str = None, include_narrative: bool = False, use_memory: bool = True, parallel: bool = None) -> Dict[str, str]:
    """
    实战组合：①②③④（可选⑤）。parallel=True 时五步并行执行，总耗时约等于最慢一步；否则顺序执行。
    parallel 默认跟随环境变量 DEEP_PARALLEL（1=并行，0=顺序）。单实例 Ollama 若内部排队，可设 DEEP_PARALLEL=0。
    use_memory=True 时结果会保存，后续可通过 memory_store.retrieve 做「上次分析」对比。
    """
    if parallel is None:
        parallel = _DEEP_PARALLEL
    ticker = (ticker or "").upper().strip()
    inputs = {"ticker": ticker, "peers": peers}
    results = {}
    step_names = {
        "1_基本面深度": "①基本面深度",
        "2_护城河与竞争优势": "②护城河",
        "3_同行业横向对比": "③同行对比",
        "4_空头视角": "④空头视角",
        "5_财报与叙事变化": "⑤叙事变化",
    }
    steps = [
        ("1_基本面深度", chain_fundamental_deep),
        ("2_护城河与竞争优势", chain_moat),
        ("3_同行业横向对比", chain_peers),
        ("4_空头视角", chain_short),
    ]
    if include_narrative:
        steps.append(("5_财报与叙事变化", chain_narrative))

    if parallel and len(steps) > 1:
        t_parallel = time.time()
        with ThreadPoolExecutor(max_workers=min(5, len(steps))) as executor:
            futures = {
                executor.submit(_run_one_deep_step, label, fn, inputs, ticker, step_names): label
                for label, fn in steps
            }
            for fut in as_completed(futures):
                label, result = fut.result()
                results[label] = result
        print(f"[Report] {ticker} 深度分析（并行）总耗时 {time.time() - t_parallel:.1f}s", flush=True)
    else:
        for label, chain_fn in steps:
            t0 = time.time()
            try:
                results[label] = chain_fn(inputs)
            except Exception as e:
                results[label] = f"[分析异常] {e}"
            elapsed = time.time() - t0
            print(f"[Report] {ticker} {step_names[label]} 耗时 {elapsed:.1f}s", flush=True)

    # 整次深度分析结果存为 full_deep_run，供 report 时「与上次对比」
    try:
        blob = json.dumps({"ts": datetime.now().isoformat(), **results}, ensure_ascii=False)
        save(ticker, "full_deep_run", blob)
    except Exception:
        pass
    return results


def run_comparison(ticker: str, current_deep: Dict[str, str], past_deep: Optional[Dict[str, str]]) -> Dict[str, Any]:
    """
    对比「本次深度分析」与「上次深度分析」：大方向是否一致、近期趋势。
    无上次时返回 direction_unchanged=True, reason="无历史对比", recent_trend="—"。
    """
    ticker = (ticker or "").upper().strip()
    out = {"direction_unchanged": True, "reason": "无历史对比", "recent_trend": "—"}
    if not past_deep or not isinstance(past_deep, dict):
        return out
    try:
        prompt = f"""请对比以下「{ticker}」的【本次分析】与【上次分析】，严格按 3 行输出（不要多写）：

大方向是否一致：<是 或 否>
依据或变化要点：<一两句话，说明大方向不变的理由或发生的主要变化>
近期对比趋势：<一两句话，近期相对上次的趋势变化>

【本次分析】
{json.dumps(current_deep, ensure_ascii=False)[:6000]}

【上次分析】
{json.dumps(past_deep, ensure_ascii=False)[:6000]}
"""
        resp = get_llm().invoke([
            SystemMessage(content="你是投资复盘助手。只输出上述 3 行，不要其他内容。"),
            HumanMessage(content=prompt),
        ]).content or ""
        for line in resp.strip().split("\n"):
            line = line.strip()
            if line.startswith("大方向是否一致：") or line.startswith("大方向是否一致:"):
                out["direction_unchanged"] = "是" in (line.replace("大方向是否一致：", "").replace("大方向是否一致:", "").strip())
            elif line.startswith("依据或变化要点：") or line.startswith("依据或变化要点:"):
                out["reason"] = line.replace("依据或变化要点：", "").replace("依据或变化要点:", "").strip()
            elif line.startswith("近期对比趋势：") or line.startswith("近期对比趋势:"):
                out["recent_trend"] = line.replace("近期对比趋势：", "").replace("近期对比趋势:", "").strip()
    except Exception:
        pass
    return out
