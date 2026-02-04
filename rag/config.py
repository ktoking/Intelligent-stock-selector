"""RAG 相关配置：向量库路径、集合名、Embedding 方式等。"""
import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 向量库持久化目录（环境变量优先）
RAG_PERSIST_DIR = os.environ.get("RAG_PERSIST_DIR", "").strip()
if not RAG_PERSIST_DIR:
    RAG_PERSIST_DIR = str(_PROJECT_ROOT / "data" / "rag_chroma")
else:
    RAG_PERSIST_DIR = str(Path(RAG_PERSIST_DIR).expanduser().resolve())

# Chroma 集合名
RAG_COLLECTION_NAME = os.environ.get("RAG_COLLECTION_NAME", "stock_analysis").strip() or "stock_analysis"

# Embedding：ollama（本地 Ollama nomic-embed-text）| default（Chroma 默认，需联网或已缓存）
RAG_EMBEDDING = (os.environ.get("RAG_EMBEDDING", "ollama")).strip().lower()
if RAG_EMBEDDING not in ("ollama", "default"):
    RAG_EMBEDDING = "ollama"

# Ollama Embedding 模型名
OLLAMA_EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text").strip() or "nomic-embed-text"
OLLAMA_EMBED_URL = os.environ.get("OLLAMA_EMBED_URL", "http://localhost:11434").strip() or "http://localhost:11434"

# 检索默认条数
RAG_TOP_K = int(os.environ.get("RAG_TOP_K", "5").strip() or "5")
RAG_TOP_K = max(1, min(20, RAG_TOP_K))

# 是否在综合分析时启用 RAG 上下文（0=关闭，1=开启）
RAG_ENABLED = os.environ.get("RAG_ENABLED", "0").strip() in ("1", "true", "yes")

# 报告生成后是否将卡片同步写入向量库（0=关闭，1=开启）
RAG_SYNC_CARDS = os.environ.get("RAG_SYNC_CARDS", "0").strip() in ("1", "true", "yes")

# memory 建索引是否按 ### 语义段落切分（1=按段落，0=仅按固定长度）
RAG_CHUNK_BY_SECTION = os.environ.get("RAG_CHUNK_BY_SECTION", "1").strip() in ("1", "true", "yes")
# 单段最大字符数（按段落切时，超长段落再子块切）
RAG_SECTION_MAX = int(os.environ.get("RAG_SECTION_MAX", "800").strip() or "800")
