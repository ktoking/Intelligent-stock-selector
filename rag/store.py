"""
RAG 向量库：Chroma 持久化，按 ticker/analysis_type 等 metadata 过滤 + 语义检索。
"""
import os
from typing import List, Dict, Any, Optional

from rag.config import RAG_PERSIST_DIR, RAG_COLLECTION_NAME, RAG_TOP_K
from rag.embedding import get_embedding_function


def _ensure_dir():
    os.makedirs(RAG_PERSIST_DIR, exist_ok=True)


def get_collection():
    """
    获取或创建 Chroma 集合（持久化）。
    首次调用会创建目录与集合；Embedding 由 config 中的 RAG_EMBEDDING 决定。
    """
    import chromadb

    _ensure_dir()
    ef = get_embedding_function()
    client = chromadb.PersistentClient(path=RAG_PERSIST_DIR)
    collection = client.get_or_create_collection(
        name=RAG_COLLECTION_NAME,
        embedding_function=ef,
        metadata={"description": "stock analysis history and report cards"},
    )
    return collection


def add_documents(
    documents: List[str],
    metadatas: Optional[List[Dict[str, Any]]] = None,
    ids: Optional[List[str]] = None,
) -> None:
    """
    向向量库添加文档。documents 与 metadatas/ids 一一对应。
    metadatas 建议含：ticker, analysis_type, ts（便于过滤）。
    """
    if not documents:
        return
    coll = get_collection()
    if ids is None:
        import uuid
        ids = [str(uuid.uuid4()) for _ in documents]
    if metadatas is None:
        metadatas = [{}] * len(documents)
    # Chroma 要求 metadata 值为 str、int、float 或 bool
    clean_meta = []
    for m in metadatas:
        clean_meta.append({k: (v if isinstance(v, (str, int, float, bool)) else str(v)) for k, v in (m or {}).items()})
    coll.add(documents=documents, metadatas=clean_meta, ids=ids)


def query_documents(
    query_texts: Optional[List[str]] = None,
    query_embeddings: Optional[List[List[float]]] = None,
    n_results: int = RAG_TOP_K,
    where: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    语义检索。优先用 query_texts（会经 embedding 转向量）；或直接传 query_embeddings。
    where：metadata 过滤，如 {"ticker": "AAPL"} 或 {"analysis_type": "fundamental_deep"}。
    返回 chromadb 的 result：{"ids": [[...]], "documents": [[...]], "metadatas": [[...]], "distances": [[...]]}。
    """
    coll = get_collection()
    kwargs = {"n_results": n_results}
    if where:
        kwargs["where"] = where
    if query_embeddings is not None:
        res = coll.query(query_embeddings=query_embeddings, **kwargs)
    elif query_texts:
        res = coll.query(query_texts=query_texts, **kwargs)
    else:
        return {"ids": [], "documents": [], "metadatas": [], "distances": []}
    return res
