"""
长期上下文管理：按 ticker + analysis_type 存储历史分析结果，供链内检索「上次分析」。
仅使用 JSONL 持久化，不占用内存；存储目录可配置，未配置时默认项目下 data/memory。
"""
import os
import json
from datetime import datetime
from typing import List, Optional
from pathlib import Path

# 存储目录：环境变量优先，否则默认项目根下 data/memory
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_STORE_DIR = _PROJECT_ROOT / "data" / "memory"
_STORE_DIR = os.environ.get("STOCK_AGENT_MEMORY_DIR", "").strip()
_STORE_DIR = Path(_STORE_DIR) if _STORE_DIR else _DEFAULT_STORE_DIR

_FILENAME = "memory_store.jsonl"
_MAX_PER_KEY = 5  # 每个 ticker+type 最多保留条数（retrieve 时截断；文件本身只追加不自动裁剪）


def _file_path() -> Path:
    """返回 JSONL 文件路径；确保目录存在。"""
    _STORE_DIR.mkdir(parents=True, exist_ok=True)
    return _STORE_DIR / _FILENAME


def save(ticker: str, analysis_type: str, content: str) -> None:
    """保存一次分析结果到 JSONL 文件，不写入内存。"""
    ticker = (ticker or "").upper().strip()
    if not ticker or not analysis_type:
        return
    record = {
        "ticker": ticker,
        "analysis_type": analysis_type,
        "content": content[:50000],  # 单条上限
        "ts": datetime.now().isoformat(),
    }
    fp = _file_path()
    try:
        with open(fp, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def retrieve(ticker: str, analysis_type: Optional[str] = None, last_n: int = 2) -> List[dict]:
    """
    从 JSONL 文件检索历史分析。analysis_type 为空时检索该 ticker 所有类型；last_n 为每种类型最多条数。
    返回按时间倒序的列表，用于拼接到 prompt 上下文。
    """
    ticker = (ticker or "").upper().strip()
    if not ticker:
        return []
    fp = _file_path()
    if not fp.exists():
        return []
    records: List[dict] = []
    try:
        with open(fp, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    if (r.get("ticker") or "").upper() != ticker:
                        continue
                    if analysis_type and (r.get("analysis_type") or "") != analysis_type:
                        continue
                    records.append(r)
                except (json.JSONDecodeError, TypeError):
                    continue
    except Exception:
        return []
    records.sort(key=lambda x: x.get("ts") or "", reverse=True)
    # 每种 (ticker, analysis_type) 最多 last_n 条
    seen_keys: dict = {}
    out: List[dict] = []
    for r in records:
        key = (r.get("ticker"), r.get("analysis_type"))
        count = seen_keys.get(key, 0)
        if count >= last_n:
            continue
        seen_keys[key] = count + 1
        out.append(r)
    out.sort(key=lambda x: x.get("ts") or "", reverse=True)
    return out[: last_n * (len(seen_keys) or 1)]  # 最多 last_n 条（单 type）或按 type 数略放宽


def get_context_summary(ticker: str, analysis_type: Optional[str] = None) -> str:
    """返回可用于拼接到 system/user 的「上次分析」摘要；无则返回空字符串。"""
    records = retrieve(ticker, analysis_type=analysis_type, last_n=1)
    if not records:
        return ""
    r = records[0]
    return f"【上次分析摘要 - {r.get('analysis_type', '')} @ {(r.get('ts') or '')[:10]}】\n{(r.get('content') or '')[:2000]}...\n"


def get_memory_store():
    """返回 memory store 接口（save/retrieve/get_context_summary），便于链内注入。"""
    class Store:
        @staticmethod
        def save(ticker: str, analysis_type: str, content: str):
            save(ticker, analysis_type, content)

        @staticmethod
        def retrieve(ticker: str, analysis_type: Optional[str] = None, last_n: int = 2):
            return retrieve(ticker, analysis_type, last_n)

        @staticmethod
        def get_context_summary(ticker: str, analysis_type: Optional[str] = None):
            return get_context_summary(ticker, analysis_type)

    return Store()
