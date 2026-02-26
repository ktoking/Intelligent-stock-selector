# RAG 模块使用说明

RAG 模块把「历史分析结果」和「报告卡片摘要」写入向量库（Chroma），在综合分析时按语义检索若干条拼进 Prompt，提升一致性与可解释性。

---

## 一、目录与依赖

- **目录**：`rag/`
  - `config.py`：向量库路径、集合名、Embedding 方式、RAG_ENABLED / RAG_SYNC_CARDS
  - `embedding.py`：Ollama 或 Chroma 默认 Embedding
  - `store.py`：Chroma 持久化、add_documents、query_documents
  - `build_index.py`：从 memory_store.jsonl 建索引、从报告卡片建索引
  - `retrieve.py`：retrieve_for_prompt、format_rag_context
- **依赖**：`chromadb>=0.4.0`、`requests>=2.28.0`（Ollama 回退用）

---

## 二、建索引（写入向量库）

### 1. 从 memory_store 建索引（按维度切分）

历史分析结果在 `data/memory/memory_store.jsonl`（或 `STOCK_AGENT_MEMORY_DIR` 指定目录）。**默认按「维度」切分后写入**：

- **标的维度**：每条 metadata 带 `ticker`，便于按标的过滤。
- **分析类型维度**：`analysis_type` + `analysis_type_zh`（基本面深度 / 护城河 / 同行对比 / 空头视角 / 叙事变化等）。
- **时间维度**：`ts`（精确时间）+ `ts_date`（YYYY-MM-DD），便于按日过滤。
- **语义段落维度**：按 content 中的 **`###` 标题**拆成小节（如「收入与增长质量」「盈利能力」「技术或产品壁垒」等），每节一条；单节超长（默认 800 字）再子块切，metadata 带 `section_heading`、`section_ord`、`chunk_ord`。

运行：

```bash
# 默认：按 ### 段落切分后写入（推荐）
python -m rag.build_index --memory-only

# 指定 memory 文件路径
python -m rag.build_index --memory-file /path/to/memory_store.jsonl

# 不按段落切，改为按固定长度切（500 字/块、50 字重叠）
python -m rag.build_index --memory-only --no-section

# 长文不子块切，整段或整条写入（超长会截断）
python -m rag.build_index --memory-only --no-chunk
```

环境变量：`RAG_CHUNK_BY_SECTION=1`（默认）按段落切；`RAG_SECTION_MAX=800` 单段最大字符数；`RAG_CHUNK_SIZE`/`RAG_CHUNK_OVERLAP` 在 `--no-section` 时生效。

### 2. 报告完成后自动写入卡片（可选）

设置环境变量后，每次报告生成结束会把本期卡片同步到向量库：

```bash
export RAG_SYNC_CARDS=1
python server.py
```

每张卡片会拼成一段：「ticker 名称 评分 交易动作 核心结论 评分理由」，metadata 含 `ticker`、`analysis_type=report_card`、`sector`、`market`。

---

## 三、综合分析时使用 RAG（检索拼进 Prompt）

设置环境变量后，单只综合分析会先做一次语义检索，把「参考历史分析」拼进 Prompt（在【技术面】之前）：

```bash
export RAG_ENABLED=1
python server.py
```

- **检索逻辑**：用「ticker + 分析 结论 评分 交易动作」作 query，默认按 **同一 ticker** 过滤，取 TopK（默认 5）条，总长度约 1500 字截断后格式化为「【参考历史分析】…」。
- **未建索引时**：检索结果为空，Prompt 不包含 RAG 块，不影响正常分析。

---

## 四、环境变量一览

| 变量 | 含义 | 默认 |
|------|------|------|
| `RAG_PERSIST_DIR` | 向量库持久化目录 | 项目下 `data/rag_chroma` |
| `RAG_COLLECTION_NAME` | Chroma 集合名 | `stock_analysis` |
| `RAG_EMBEDDING` | Embedding 方式：`ollama` / `default` | `ollama` |
| `OLLAMA_EMBED_MODEL` | Ollama 嵌入模型名 | `nomic-embed-text` |
| `OLLAMA_EMBED_URL` | Ollama 服务地址 | `http://localhost:11434` |
| `RAG_TOP_K` | 检索条数 | `5` |
| `RAG_ENABLED` | 综合分析是否启用 RAG 上下文：`0`/`1` | `0` |
| `RAG_SYNC_CARDS` | 报告完成后是否同步卡片到向量库：`0`/`1` | `0` |
| `RAG_CHUNK_BY_SECTION` | memory 建索引是否按 ### 段落切（1=按段落，0=按固定长度） | `1` |
| `RAG_SECTION_MAX` | 单段最大字符数（按段落切时，超长再子块切） | `800` |
| `RAG_CHUNK_SIZE` | 固定长度切块时的块长（字，仅当 RAG_CHUNK_BY_SECTION=0） | `500` |
| `RAG_CHUNK_OVERLAP` | 固定长度切块重叠（字） | `50` |
| `STOCK_AGENT_MEMORY_DIR` | memory_store.jsonl 所在目录（与 chains 一致） | 项目下 `data/memory` |

---

## 五、Ollama Embedding 准备

使用 `RAG_EMBEDDING=ollama`（默认）时，需本地已启动 Ollama 并拉取嵌入模型：

```bash
ollama pull nomic-embed-text
```

若未安装 Chroma 自带的 `OllamaEmbeddingFunction`，模块会用 `requests` 调 `http://localhost:11434/api/embeddings` 作为回退。

使用 Chroma 默认模型（英文为主）时：

```bash
export RAG_EMBEDDING=default
```

首次运行会下载默认模型（如 all-MiniLM-L6-v2）。

---

## 六、推荐流程

1. **首次使用**：先建索引，再开 RAG。
   ```bash
   ollama pull nomic-embed-text
   python -m rag.build_index --memory-only
   export RAG_ENABLED=1
   python server.py
   ```
2. **报告后同步卡片**：需要把每期报告结论写入向量库时：
   ```bash
   export RAG_SYNC_CARDS=1
   export RAG_ENABLED=1
   python server.py
   ```
3. **只建索引、暂不检索**：不设 `RAG_ENABLED`，仅运行 `python -m rag.build_index --memory-only`，之后需要时再设 `RAG_ENABLED=1`。

---

## 七、与 memory_store 的关系

- **memory_store**：按 **ticker + analysis_type** 精确取「该标的上次/历史分析」，用于深度「与上次对比」。
- **RAG**：按 **语义相似** 检索「历史分析/报告卡片」片段，拼进综合 Prompt。
- 两者可同时使用：memory 管「同一标的、同一类型」的精确历史；RAG 管「可读的参考片段」增强综合上下文。
