"""
RAG Embedding：支持 Ollama（nomic-embed-text）与 Chroma 默认。
需先拉取模型：ollama pull nomic-embed-text
"""
from rag.config import RAG_EMBEDDING, OLLAMA_EMBED_MODEL, OLLAMA_EMBED_URL


class _OllamaEmbeddingFunction:
    """兼容：若 chromadb 无 OllamaEmbeddingFunction，用 requests 调 Ollama /api/embeddings。"""

    def __init__(self, url: str, model_name: str):
        self.url = url.rstrip("/")
        if "/api/embeddings" not in self.url:
            self.url = f"{self.url}/api/embeddings"
        self.model_name = model_name

    def __call__(self, input_texts):
        import requests
        out = []
        for t in input_texts:
            try:
                r = requests.post(
                    self.url,
                    json={"model": self.model_name, "prompt": (t or "").strip() or " "},
                    timeout=60,
                )
                r.raise_for_status()
                out.append(r.json().get("embedding", []))
            except Exception:
                out.append([])
        return out


def get_embedding_function():
    """
    返回 Chroma 可用的 embedding function。
    RAG_EMBEDDING=ollama 时使用 Ollama（需先 ollama pull nomic-embed-text）；
    RAG_EMBEDDING=default 时使用 Chroma 默认（all-MiniLM-L6-v2，首次会下载）。
    """
    if RAG_EMBEDDING == "ollama":
        try:
            from chromadb.utils import embedding_functions
            if hasattr(embedding_functions, "OllamaEmbeddingFunction"):
                base = (OLLAMA_EMBED_URL or "http://localhost:11434").rstrip("/")
                url = f"{base}/api/embeddings" if "/api/" not in base else base
                return embedding_functions.OllamaEmbeddingFunction(
                    model_name=OLLAMA_EMBED_MODEL,
                    url=url,
                )
        except Exception:
            pass
        return _OllamaEmbeddingFunction(
            url=OLLAMA_EMBED_URL or "http://localhost:11434",
            model_name=OLLAMA_EMBED_MODEL,
        )
    # default：Chroma 内置默认（all-MiniLM-L6-v2）
    try:
        from chromadb.utils import embedding_functions
        return embedding_functions.DefaultEmbeddingFunction()
    except Exception as e:
        raise RuntimeError(
            f"Chroma 默认 Embedding 初始化失败。可设置 RAG_EMBEDDING=ollama 使用 Ollama。错误: {e}"
        ) from e
