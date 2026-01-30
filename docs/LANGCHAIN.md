# LangChain 在本项目中的用法

本项目用 LangChain 做三件事：**多步骤推理编排**、**外部数据接入**、**长期上下文管理**。

## 一、多步骤推理如何组织

- **单步链**：`数据拉取(Runnable) → Prompt 填充 → LLM → 输出`，每一步由 `chains/chains.py` 里的 `_chain_one` 统一封装。
- **组合链**：`chain_full_deep(ticker)` 按顺序执行 ① 基本面 → ② 护城河 → ③ 同行对比 → ④ 空头（可选 ⑤ 叙事），每步结果自动写入长期上下文。
- **入口**：`agents/analysis_deep.py` 在 `LANGCHAIN=1` 且已安装 `langchain-core` / `langchain-openai` 时，会走 `chains.chains` 的链；否则回退到直接 `ask_llm`。

## 二、外部数据如何接入

- **数据层**：`chains/data_fetchers.py` 里将 yfinance / 新闻 / 同行列表 等封装成 **Runnable**（`fetch_stock_data`）。
- **输入**：链的输入为 `dict`，例如 `{"ticker": "AAPL", "peers": "MSFT,GOOGL"}`。
- **输出**：`fetch_stock_data.invoke(inputs)` 返回扩充后的 dict（`financials`, `company_info`, `quarterly_summary`, `news_summary`, `peers`），供各分析 Prompt 使用。
- **扩展**：新增数据源时，在 `data_fetchers.py` 里加新的 Runnable 或在 `_fetch_stock_data` 中增加字段即可。

## 三、上下文如何被长期管理

- **存储**：`chains/memory_store.py` 提供 `save(ticker, analysis_type, content)`，每次分析完成后由链自动调用，将结果按「标的 + 分析类型」**仅写入 JSONL 文件**，不占用内存。
- **检索**：`retrieve(ticker, analysis_type=None, last_n=2)` 从 JSONL 文件按标的（及可选类型）取最近 N 条记录；`get_context_summary(ticker, analysis_type)` 返回一段可拼进 Prompt 的「上次分析」摘要。
- **持久化**：分析结果**仅持久化在 JSONL**，默认目录为项目根下 `data/memory/memory_store.jsonl`；可通过环境变量 `STOCK_AGENT_MEMORY_DIR=/path/to/dir` 指定目录。
- **HTTP**：`GET /memory?ticker=AAPL&analysis_type=fundamental_deep&last_n=2` 检索历史；`GET /memory/context?ticker=AAPL` 获取「上次分析」摘要文本。

## 四、如何关闭 LangChain

- 设置环境变量 `LANGCHAIN=0`，则深度分析仍使用原来的 `llm.ask_llm` 路径，不依赖 LangChain。
- 若不安装 `langchain-core` / `langchain-openai`，也会自动回退到 `ask_llm`；此时 `/memory` 接口返回空或占位。

## 五、依赖

```
langchain-core>=0.3.0
langchain-openai>=0.2.0
```

Ollama 通过 `ChatOpenAI(base_url="http://localhost:11434/v1", api_key="ollama")` 接入，与现有 `llm.py` 配置一致。

---

## 六、引入 LangChain 还能做哪些优化

| 方向 | 说明 | 与本项目的结合 |
|------|------|----------------|
| **结构化输出** | 用 `llm.with_structured_output(Pydantic)` 让模型直接返回 JSON/对象，避免正则解析。 | `agents/full_analysis.py` 里「核心结论、评分、加仓价、减仓价」等可定义成 Pydantic 模型，一次调用得到结构化结果，不再用 `_parse_llm_output` 逐行解析。 |
| **统一 LLM 抽象** | 全项目通过 `chains/llm_factory.get_llm()` 调用，一处切换模型/温度/API。 | 把 `llm.ask_llm` 的调用逐步迁到 LangChain ChatModel，便于以后换模型、加流式、加重试。 |
| **单标的分析链化** | 将「技术面 + 消息面 + 财报 → Prompt → LLM → 解析」做成一条 LCEL 链。 | `run_full_analysis` 可改为 `fetch_for_report \| prompt \| get_llm() \| parser`，步骤可观测、可复用、可加 memory 注入。 |
| **RAG / 检索增强** | 用向量库存历史报告，按「相似问题/标的」检索后拼进上下文。 | 将历史 `memory_store` 的摘要或报告片段做 embedding 存 Chroma/FAISS，分析时检索「同行业或同类型」的过往结论，提升一致性、减少重复分析。 |
| **对话记忆（多轮）** | 若提供聊天式问答，需要「记住本轮对话」。 | 使用 LangChain 的 `ConversationBufferWindowMemory` 或 `ConversationSummaryMemory`，按 session 存 Human/AI 消息，生成报告或回答时自动带上最近几轮/摘要。 |
| **工具 + Agent** | 把拉取行情、财报、新闻等封装成 Tool，由 LLM 决定何时调哪个。 | 适合「用户自由提问」场景：用户问「AAPL 和 MSFT 谁更贵」，Agent 自动选 get_fundamental、get_technical 等工具，再综合回答。 |
| **流式输出** | `llm.stream()` 逐 token 返回，适合长报告。 | 报告生成或深度分析时改为 `stream`，前端可边生成边展示，体验更好。 |
| **可选缓存** | 对相同/相似 prompt 做缓存，减少重复调用。 | 对「同一 ticker、同一分析类型、数据未变」的请求可做语义或 key 缓存，节省 API 与时间。 |

---

## 七、LangChain 的「上下文 / 记忆」是怎么存的

LangChain 里和「记忆」相关的有两类：**对话记忆**（多轮聊天）和**结果/知识记忆**（本项目已在用的那种）。

### 7.1 对话记忆（多轮聊天）

用于「用户多轮提问、模型要记得前面说过什么」的场景，存的是 **消息列表**（`HumanMessage` / `AIMessage` / `SystemMessage`），在每次调用前被注入到 prompt 里。

| 类型 | 存储方式 | 特点 |
|------|----------|------|
| **ConversationBufferMemory** | 内存里一个 list，保存全部 Human/AI 消息。 | 简单，但轮次多了会超长，适合短对话。 |
| **ConversationBufferWindowMemory** | 只保留最近 K 轮消息（如 K=10）。 | 控制长度，不爆上下文；更早的轮次会丢失。 |
| **ConversationSummaryMemory** | 用 LLM 把历史对话压缩成一段「摘要」保存，新消息继续追加。 | 长对话也能压成短摘要，但多一次 LLM 调用、有成本。 |
| **VectorStoreRetrieverMemory** | 把历史消息（或摘要）做 embedding 存进向量库，按「当前问题」相似度检索若干条再拼进 prompt。 | 适合超长对话或知识库，按相关性取回，不按「最近 K 轮」。 |

**典型用法**：先 `memory.save_context({"input": user_msg}, {"output": ai_msg})`，再在链里用 `memory.load_memory_variables({})` 得到 `{"history": "Human: ...\nAI: ..."}`，把 `history` 拼进 system 或 user prompt。持久化可自己写：把 `memory.chat_memory.messages` 序列化到 Redis/DB/文件，下次启动再灌回去。

### 7.2 本项目的「长期上下文」：结果记忆（按 ticker + 类型）

当前 **不是** 上面那种「多轮对话消息」，而是 **按分析结果** 的记忆：

- **存什么**：每次深度分析完成后的 **文本结果**（如基本面深度、护城河、同行对比、空头、full_deep_run）。
- **按什么键**：`(ticker, analysis_type)`，例如 `("AAPL", "fundamental_deep")`。
- **存在哪**：  
  - 默认：内存列表 `_RECORDS`，每 key 最多保留 N 条（如 5 条）。  
  - 可选：设置 `STOCK_AGENT_MEMORY_DIR` 后，每条追加写入目录下的 `memory_store.jsonl`，实现跨进程/重启持久化。
- **怎么用**：`retrieve(ticker, analysis_type, last_n)` 取最近几次结果；`get_context_summary(ticker, analysis_type)` 得到「上次分析」的摘要字符串，在 `run_comparison` 或深度分析 prompt 里拼成「上次分析」上下文。

因此：**本项目目前的「记忆」是「按标的 + 分析类型的结果存储，仅 JSONL 持久化（不占内存）」**；若要再做「多轮对话记忆」，可以在此基础上增加 LangChain 的 `ConversationBufferWindowMemory`（或 Summary/Vector）按 session 存对话，两套记忆可以并存——一套管「上次分析结果」（JSONL），一套管「当前对话轮次」（对话记忆）。

### 7.3 本项目是否用了 RAG / 检索增强？

**没有。** 当前没有使用向量库、embedding 或按相似度检索。分析结果记忆是按 **ticker + analysis_type** 的键值检索（从 JSONL 读最近 N 条），不是「相似问题/相似报告」的语义检索。若以后要做 RAG，可把历史报告摘要做 embedding 存向量库，分析时按「同行业/同类型」检索再拼进上下文。

### 7.4 对话记忆对这个项目有帮助吗？两套记忆能并存吗？

- **对话记忆**：若产品形态是「用户多轮提问、机器人连续回答」（例如：「帮我看看 AAPL」「和 MSFT 比呢？」「总结一下」），则**对话记忆有帮助**，模型能记住上一轮说的标的和结论。若主要是「选一批 ticker → 跑报告 → 看 HTML」，没有多轮对话，则对话记忆不是刚需。
- **两套记忆并存**：可以。**分析结果记忆**（JSONL）负责「上次/历史分析结果」，供对比与复盘；**对话记忆**（如 BufferWindow）负责「当前会话的几轮问答」，供多轮对话上下文。两套互不替代，可同时启用。
