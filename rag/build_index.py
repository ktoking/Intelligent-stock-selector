"""
RAG 建索引：从 memory_store.jsonl 按「维度」切分后写入向量库。

维度设计（数据分析视角）：
1. 标的维度：ticker（每段都带，便于按标的过滤）
2. 分析类型维度：analysis_type + analysis_type_zh（基本面深度/护城河/同行对比/空头视角/叙事变化等）
3. 时间维度：ts + ts_date（便于按日过滤或排序）
4. 语义段落维度：按 ### 标题切段，每段为「一个小节」（如「收入与增长质量」「盈利能力」「技术或产品壁垒」等），
   单段超长时再按字符子块切，并保留 section_heading、section_ord、chunk_ord 便于检索与解释。
"""
import os
import re
import json
import uuid
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

from rag.store import get_collection, add_documents
from rag.config import RAG_PERSIST_DIR, RAG_CHUNK_BY_SECTION, RAG_SECTION_MAX

# memory_store 路径：与 chains/memory_store 一致
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_MEMORY_DIR = _PROJECT_ROOT / "data" / "memory"
_MEMORY_DIR = os.environ.get("STOCK_AGENT_MEMORY_DIR", "").strip()
_MEMORY_DIR = Path(_MEMORY_DIR) if _MEMORY_DIR else _DEFAULT_MEMORY_DIR
_MEMORY_FILE = _MEMORY_DIR / "memory_store.jsonl"

# 单段最大字符数（按段落切时，超长段落再子块切）
SECTION_MAX = RAG_SECTION_MAX
# 固定长度切块时的块长与重叠（RAG_CHUNK_BY_SECTION=0 时用）
CHUNK_SIZE = int(os.environ.get("RAG_CHUNK_SIZE", "500").strip() or "500")
CHUNK_OVERLAP = int(os.environ.get("RAG_CHUNK_OVERLAP", "50").strip() or "50")

# analysis_type -> 中文标签（写入 metadata 便于检索与展示）
ANALYSIS_TYPE_ZH: Dict[str, str] = {
    "fundamental_deep": "基本面深度",
    "moat": "护城河",
    "peers": "同行对比",
    "short": "空头视角",
    "narrative": "叙事变化",
    "full_deep_run": "深度报告",
    "report_card": "报告卡片",
}


def _split_by_sections(content: str) -> List[Tuple[str, str]]:
    """
    按 ### 标题拆成 (标题, 正文) 列表。
    若无 ###，则整段当作 (None, content)。
    """
    if not content or not content.strip():
        return []
    text = content.strip()
    # 匹配 ### 标题（兼容 ### 标题 或 ### 1. 标题）
    parts = re.split(r"\n(?=###\s+)", text)
    out: List[Tuple[str, str]] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if p.startswith("###"):
            first_line = p.split("\n", 1)[0]
            heading = first_line.replace("###", "").strip()
            body = p[len(first_line) :].strip() if len(p) > len(first_line) else ""
            out.append((heading, body))
        else:
            out.append(("", p))
    if not out:
        out.append(("", text))
    return out


def _chunk_text(text: str, chunk_size: int, overlap: int) -> List[str]:
    """按固定长度+重叠切块。"""
    if not text or not text.strip():
        return []
    s = text.strip()
    if len(s) <= chunk_size:
        return [s]
    chunks = []
    start = 0
    while start < len(s):
        end = start + chunk_size
        chunks.append(s[start:end])
        if end >= len(s):
            break
        start = end - overlap
    return chunks


def _section_to_documents(
    ticker: str,
    atype: str,
    ts: str,
    ts_date: str,
    section_heading: str,
    section_ord: int,
    body: str,
    atype_zh: str,
) -> Tuple[List[str], List[Dict[str, Any]], List[str]]:
    """
    将一个小节转为可写入向量库的 documents + metadatas + ids。
    若 body 超过 SECTION_MAX，则子块切分，chunk_ord 递增。
    """
    doc_texts: List[str] = []
    metas: List[Dict[str, Any]] = []
    ids_out: List[str] = []
    if not body.strip():
        # 仅有标题无正文时，仍写入一条（标题可检索）
        doc_texts.append(section_heading or "(无正文)")
        metas.append({
            "ticker": ticker,
            "analysis_type": atype,
            "analysis_type_zh": atype_zh,
            "ts": ts,
            "ts_date": ts_date,
            "section_heading": (section_heading or "")[:64],
            "section_ord": section_ord,
            "chunk_ord": 0,
        })
        ids_out.append(f"mem_{ticker}_{atype}_{ts_date}_{section_ord}_0_{uuid.uuid4().hex[:8]}")
        return doc_texts, metas, ids_out

    full = f"{section_heading}\n\n{body}" if section_heading else body
    if len(full) <= SECTION_MAX:
        doc_texts.append(full)
        metas.append({
            "ticker": ticker,
            "analysis_type": atype,
            "analysis_type_zh": atype_zh,
            "ts": ts,
            "ts_date": ts_date,
            "section_heading": (section_heading or "")[:64],
            "section_ord": section_ord,
            "chunk_ord": 0,
        })
        ids_out.append(f"mem_{ticker}_{atype}_{ts_date}_{section_ord}_0_{uuid.uuid4().hex[:8]}")
    else:
        sub_chunks = _chunk_text(body, SECTION_MAX - len(section_heading) - 4, CHUNK_OVERLAP)
        for cidx, sub in enumerate(sub_chunks):
            block = f"{section_heading}\n\n{sub}" if section_heading else sub
            doc_texts.append(block)
            metas.append({
                "ticker": ticker,
                "analysis_type": atype,
                "analysis_type_zh": atype_zh,
                "ts": ts,
                "ts_date": ts_date,
                "section_heading": (section_heading or "")[:64],
                "section_ord": section_ord,
                "chunk_ord": cidx,
            })
            ids_out.append(f"mem_{ticker}_{atype}_{ts_date}_{section_ord}_{cidx}_{uuid.uuid4().hex[:8]}")
    return doc_texts, metas, ids_out


def build_index_from_memory(
    memory_file: Optional[Path] = None,
    chunk_by_section: Optional[bool] = None,
    chunk_long: bool = True,
) -> int:
    """
    从 memory_store.jsonl 读取历史分析，按维度切分后写入向量库。

    维度与切分策略：
    - 标的：ticker（每条 metadata 必带）
    - 分析类型：analysis_type + analysis_type_zh（基本面深度/护城河/同行对比/空头视角/叙事变化）
    - 时间：ts + ts_date（精确到日，便于按日过滤）
    - 语义段落：按 ### 拆成小节，每节一条或子块多条；metadata 带 section_heading、section_ord、chunk_ord

    chunk_by_section：True 时按 ### 段落切；False 时仅按固定长度切（与旧逻辑一致）。默认跟随 RAG_CHUNK_BY_SECTION。
    chunk_long：在「按段落切」时，单段超过 SECTION_MAX 再子块切；在「按长度切」时，超长则按 CHUNK_SIZE 切。
    返回写入的文档条数。
    """
    fp = memory_file or _MEMORY_FILE
    if not fp.exists():
        print(f"[RAG] 未找到 memory 文件: {fp}", flush=True)
        return 0
    use_section = chunk_by_section if chunk_by_section is not None else RAG_CHUNK_BY_SECTION

    documents = []
    metadatas = []
    ids = []
    try:
        with open(fp, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    ticker = (r.get("ticker") or "").upper().strip()
                    atype = (r.get("analysis_type") or "").strip()
                    content = (r.get("content") or "").strip()
                    ts = (r.get("ts") or "")[:19]
                    ts_date = (ts[:10]) if len(ts) >= 10 else ts
                    atype_zh = ANALYSIS_TYPE_ZH.get(atype, atype or "其他")
                    if not content:
                        continue

                    if use_section:
                        sections = _split_by_sections(content)
                        for ord_i, (heading, body) in enumerate(sections):
                            doc_list, meta_list, id_list = _section_to_documents(
                                ticker=ticker,
                                atype=atype,
                                ts=ts,
                                ts_date=ts_date,
                                section_heading=heading,
                                section_ord=ord_i,
                                body=body,
                                atype_zh=atype_zh,
                            )
                            documents.extend(doc_list)
                            metadatas.extend(meta_list)
                            ids.extend(id_list)
                    else:
                        # 旧逻辑：仅按固定长度切
                        if chunk_long and len(content) > CHUNK_SIZE:
                            chunks = _chunk_text(content, CHUNK_SIZE, CHUNK_OVERLAP)
                            for i, ch in enumerate(chunks):
                                documents.append(ch)
                                metadatas.append({
                                    "ticker": ticker,
                                    "analysis_type": atype,
                                    "analysis_type_zh": atype_zh,
                                    "ts": ts,
                                    "ts_date": ts_date,
                                    "section_ord": i,
                                    "chunk_ord": 0,
                                })
                                ids.append(f"mem_{ticker}_{atype}_{ts}_{i}_{uuid.uuid4().hex[:8]}")
                        else:
                            doc = content[:10000]
                            documents.append(doc)
                            metadatas.append({
                                "ticker": ticker,
                                "analysis_type": atype,
                                "analysis_type_zh": atype_zh,
                                "ts": ts,
                                "ts_date": ts_date,
                            })
                            ids.append(f"mem_{ticker}_{atype}_{ts}_{uuid.uuid4().hex[:8]}")
                except (json.JSONDecodeError, TypeError):
                    continue
    except Exception as e:
        print(f"[RAG] 读取 memory 失败: {e}", flush=True)
        return 0

    if not documents:
        print("[RAG] memory 中无有效文档", flush=True)
        return 0
    add_documents(documents=documents, metadatas=metadatas, ids=ids)
    print(f"[RAG] 已从 memory 按维度写入 {len(documents)} 条（按段落={use_section}）", flush=True)
    return len(documents)


def build_index_from_cards(cards: List[Dict[str, Any]]) -> int:
    """
    将报告卡片列表写入向量库。每张卡片拼成一段：ticker 名称 评分 交易动作 核心结论 评分理由。
    metadata 带 ticker、analysis_type=report_card、sector、market。
    返回写入的文档条数。
    """
    if not cards:
        return 0
    documents = []
    metadatas = []
    ids = []
    atype_zh = ANALYSIS_TYPE_ZH.get("report_card", "报告卡片")
    for c in cards:
        ticker = (c.get("ticker") or "").upper().strip()
        name = (c.get("name") or ticker).strip()
        score = c.get("score", "")
        action = (c.get("action") or "观察").strip()
        core = (c.get("core_conclusion") or "").strip()
        reason = (c.get("score_reason") or "").strip()
        sector = (c.get("sector") or "").strip()
        market = (c.get("market") or "").strip()
        text = f"{ticker} {name} 评分{score} {action} 核心结论：{core} 评分理由：{reason}"
        if not text.strip():
            continue
        documents.append(text[:2000])
        metadatas.append({
            "ticker": ticker,
            "analysis_type": "report_card",
            "analysis_type_zh": atype_zh,
            "sector": sector[:64] if sector else "",
            "market": market[:32] if market else "",
        })
        ids.append(f"card_{ticker}_{uuid.uuid4().hex[:12]}")
    if not documents:
        return 0
    add_documents(documents=documents, metadatas=metadatas, ids=ids)
    print(f"[RAG] 已从报告卡片写入 {len(documents)} 条", flush=True)
    return len(documents)


def main():
    """命令行：python -m rag.build_index [--memory-only] [--memory-file path] [--no-section] [--no-chunk]"""
    import argparse
    parser = argparse.ArgumentParser(
        description="RAG 建索引：从 memory 按「标的/分析类型/时间/语义段落」维度切分后写入向量库"
    )
    parser.add_argument("--memory-only", action="store_true", help="仅从 memory_store.jsonl 建索引")
    parser.add_argument("--memory-file", type=str, default="", help="memory JSONL 路径")
    parser.add_argument(
        "--no-section",
        action="store_true",
        help="不按 ### 段落切，改为按固定长度切（RAG_CHUNK_BY_SECTION=0）",
    )
    parser.add_argument("--no-chunk", action="store_true", help="长文不子块切，整条写入（会截断）")
    args = parser.parse_args()
    n = build_index_from_memory(
        memory_file=Path(args.memory_file) if args.memory_file else None,
        chunk_by_section=not args.no_section,
        chunk_long=not args.no_chunk,
    )
    if n == 0:
        print("[RAG] 未写入任何文档。", flush=True)
    print("[RAG] 建索引完成。", flush=True)


if __name__ == "__main__":
    main()
