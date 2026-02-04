"""
RAG 模块：从 memory_store 与报告卡片写入向量库，分析时按语义检索拼进 Prompt。

使用前需先建索引：python -m rag.build_index
分析时设置环境变量 RAG_ENABLED=1 即可在综合 Prompt 中注入「参考历史分析」。
"""
from rag.store import get_collection, add_documents, query_documents
from rag.retrieve import retrieve_for_prompt
from rag.build_index import build_index_from_memory, build_index_from_cards

__all__ = [
    "get_collection",
    "add_documents",
    "query_documents",
    "retrieve_for_prompt",
    "build_index_from_memory",
    "build_index_from_cards",
]
