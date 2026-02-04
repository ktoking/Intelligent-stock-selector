"""
RAG 检索：按 query 或 ticker 做语义检索，返回若干条文本供拼进 Prompt。
支持 metadata 过滤（如 ticker、analysis_type）。
"""
from typing import List, Dict, Any, Optional

from rag.store import query_documents
from rag.config import RAG_TOP_K, RAG_ENABLED


def retrieve_for_prompt(
    ticker: Optional[str] = None,
    query: Optional[str] = None,
    top_k: int = RAG_TOP_K,
    where: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    语义检索，返回若干条 {text, ticker, analysis_type, ts, ...} 供拼进 Prompt。
    ticker：可选，用于构造默认 query 或 metadata 过滤。
    query：检索语句；若未传则用 ticker 相关短句（如「{ticker} 分析 结论」）。
    top_k：返回条数。
    where：Chroma metadata 过滤，如 {"ticker": "AAPL"} 或 {"analysis_type": "fundamental_deep"}。
    若 RAG_ENABLED=0 或检索异常，返回空列表。
    """
    if not RAG_ENABLED:
        return []
    try:
        q = (query or "").strip()
        if not q and ticker:
            q = f"{ticker} 分析 结论 评分 交易动作"
        if not q:
            return []
        w = where or {}
        if ticker and "ticker" not in w:
            w = {**w, "ticker": (ticker or "").upper().strip()}
        res = query_documents(query_texts=[q], n_results=top_k, where=w if w else None)
        ids = (res.get("ids") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        out = []
        for i, (doc_id, doc_text, meta) in enumerate(zip(ids, docs, metas or [])):
            if not doc_text:
                continue
            out.append({
                "id": doc_id,
                "text": doc_text,
                "ticker": (meta or {}).get("ticker", ""),
                "analysis_type": (meta or {}).get("analysis_type", ""),
                "ts": (meta or {}).get("ts", ""),
            })
        return out
    except Exception as e:
        print(f"[RAG] 检索异常: {e}", flush=True)
        return []


def format_rag_context(records: List[Dict[str, Any]], max_total_chars: int = 1500) -> str:
    """
    将检索结果格式化为「参考历史分析」段落，供拼进 Prompt。
    超过 max_total_chars 会截断后面的条。
    """
    if not records:
        return ""
    lines = []
    total = 0
    for r in records:
        t = (r.get("text") or "").strip()
        if not t:
            continue
        ticker = r.get("ticker", "")
        atype = r.get("analysis_type", "")
        ts = (r.get("ts", ""))[:10]
        head = f"[{ticker} {atype} {ts}]" if (ticker or atype) else ""
        line = f"{head}\n{t}" if head else t
        if total + len(line) + 2 > max_total_chars:
            line = line[: max_total_chars - total - 20] + "…"
            lines.append(line)
            break
        lines.append(line)
        total += len(line) + 2
    if not lines:
        return ""
    return "【参考历史分析】\n" + "\n\n".join(lines)
