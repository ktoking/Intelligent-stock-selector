# Report 流程说明：做了什么事、怎么做的、用了哪些工具、达到什么目的

本文档说明「拉报告」从请求到最终 HTML 的完整流程，便于理解每一环在做什么、依赖哪些数据与工具。

---

## 一、入口与参数

**接口**：`GET /report`

**主要参数**：

| 参数 | 说明 | 默认 |
|------|------|------|
| **tickers** | 逗号分隔的股票代码；不传则按 market+pool 自动取池 | — |
| **limit** | 不传 tickers 时取多少只 | 5 |
| **market** | 市场：us / cn / hk（不传 tickers 时生效） | us |
| **pool** | 选股池：sp500 / nasdaq100 / russell2000 / csi2000（不传 tickers 时生效） | 空（大盘） |
| **interval** | K 线周期：1d=日K，5m/15m/1m=分K | 1d |
| **prepost** | 是否含盘前盘后（分K时常用） | 0 |
| **deep** | 0=仅技术+消息+财报+期权+一次 LLM；1=额外跑深度分析①②③④⑤+与上次对比 | 0 |
| **save_output** | 是否将 HTML 保存到 report/output/ | 1 |

**结果**：返回一整页 HTML 报告；进度可轮询 `GET /report/progress`。

---

## 二、整体流程（顶层）

```
1. 解析参数 → 得到标的列表 ticker_list
2. 对 ticker_list 逐只执行「单只分析」→ 得到多张「卡片」cards
3. 用 LLM 根据所有卡片生成「报告总览」report_summary
4. 既往推荐追踪：把本期 9/10 分且「买入」的写入记录，并拉取过去 N 天推荐的表现（胜率、收益、基准对比）
5. 用 build_report_html(cards, report_summary, backtest_...) 生成最终 HTML
6. 可选：将本期卡片同步写入 RAG 向量库（供后续检索）
7. 返回 HTML；若 save_output=1 则另存到 report/output/report-{时间}.html
```

---

## 三、标的列表从哪里来（ticker_list）

- **传了 tickers**：按逗号拆分，经 `normalize_ticker` 转成 yfinance 格式（如 A股 6 位→.SS/.SZ，港股 4 位→.HK），最多 200 只。
- **没传 tickers**：调用 `get_report_tickers(limit, market, pool)`：
  - **美股**：sp500/默认 → 市值+增长取前 N，或 Wikipedia S&P500；nasdaq100 → Wikipedia；russell2000 → Wikipedia 或静态。
  - **A股**：默认/大盘 → 优先 AKShare（沪深300 / 全A按市值），再 Wikipedia，再静态；csi2000 → 优先 AKShare 中证2000，再静态。
  - **港股**：Wikipedia 恒生或静态。

---

## 四、单只分析：两种模式

对列表里**每一只**，按 `deep` 二选一：

### 模式 A：deep=0（常规，快）

**调用**：`run_full_analysis(ticker, interval, include_prepost)`

**流程概览**：

1. **拉 4 类数据 + 1 类可选增强**
2. **拼成一大段 Prompt**，发给 **LLM 一次**
3. **解析 LLM 输出**（10 项：核心结论、趋势结构、MACD/KDJ 状态、分析原因、评分、评分理由、交易动作、加仓价、减仓价）
4. **组装成一张「卡片」** dict（含技术/消息/财报/期权摘要与 LLM 结论）

### 模式 B：deep=1（深度，慢）

**调用**：`run_one_ticker_deep_report(ticker, include_narrative=True)`

**流程概览**：

1. 先执行 **run_full_analysis**，得到「基础卡片」。
2. 再跑 **5 类深度分析**（基本面深度、护城河、同行对比、空头视角、叙事变化），每类由 **LangChain 链 + LLM** 生成一段摘要。
3. 从 **memory_store** 取出该标的上次同类分析，做 **「与上次对比」**（大方向是否一致、近期趋势）。
4. 用 **5 段深度摘要** 再调一次 **LLM**，对基础评分做 **微调**（可选 LangChain 结构化输出）。
5. 把深度摘要、对比结论、微调后评分等写回同一张卡片，得到「富卡片」。

---

## 五、常规模式（deep=0）详细：用了哪些工具、怎么做的

`run_full_analysis` 内部顺序如下。

### 1. 技术面（agents/technical.py）

**工具**：yfinance 拉历史 K 线（OHLCV）。

**做什么**：

- 计算 **均线** MA5/10/20/60，**MACD(12,26,9)**，**KDJ(9,3,3)**，**RSI(14)**，**ATR(14)** 与 **ATR%**。
- 算 **量能**：近期成交量 / 20 日均量（量比）。
- 生成 **一句技术面状态摘要**（如：日线多头排列；MACD金叉；KDJ中性；RSI中性(45)；量比1.2；ATR%2.5%）。
- 按可配置规则生成 **入场/离场参考**（MA20/MA60、ATR 止损倍数、放量突破阈值等）。

**输出**：`technical` dict（trend_ma, macd_summary, kdj_summary, rsi_summary, volume_context, tech_levels, tech_status_one_line 等），供后面拼进 Prompt。

### 2. 消息面（agents/news.py）

**工具**：yfinance 的 `ticker.news`（标题、链接、时间等）。

**做什么**：

- 取近期新闻列表。
- 可选：用 **LLM** 对新闻做一次 **摘要**（get_news_summary_llm），得到短文本。

**输出**：`news`（含 news 列表）、`news_llm_summary`（若有），写入 Prompt。

### 3. 财报/估值（agents/fundamental.py）

**工具**：yfinance（info、financials、earnings、calendar 等）。

**做什么**：

- 取市值、当前价、涨跌幅、PE、行业、52 周高低、量比（近期量/平均量）、股息率、机构建议、下次财报日等。
- 取财报原始文本（financials_str）。
- 可选：用 **LLM** 对财报做 **解读**（get_financials_interpretation），得到一句话或短段解读。

**输出**：`fundamental`（含 financials_str、current_price、pe、market_cap 等）、`financials_interpretation`，写入 Prompt。

### 4. 期权多空（agents/options.py）

**工具**：yfinance 期权链（put/call 成交量或未平仓等）。

**做什么**：

- 汇总近期 put/call 比例或多空倾向，生成一句描述（如「偏多/偏空/中性」）。

**输出**：`options_summary`（description、ratio 等），写入 Prompt。

### 5. RAG 历史分析（可选，rag/retrieve.py）

**工具**：ChromaDB 向量库 + 本地/Ollama embedding。

**做什么**：

- 若已建索引（`python -m rag.build_index`），则按 ticker（和可选 query）做 **语义检索**，取若干条历史分析片段。
- 格式化为「参考历史分析」段落。

**输出**：`rag_context` 字符串，拼入 Prompt，供 LLM 参考「上次说了什么」。

### 6. 拼 Prompt 与调用 LLM（agents/full_analysis.py）

**怎么做**：

- 把上述 **技术面一句摘要 + 均线/MACD/KDJ/RSI/量能/ATR% + 入场离场参考**、**消息面（含 LLM 新闻摘要）**、**财报/估值/期权**、**RAG 历史** 拼成一大段 **【技术面】【消息面】【财报/估值/期权】** 的 Prompt。
- **System**：要求 LLM 按固定 10 项格式输出（核心结论、趋势结构、MACD状态、KDJ状态、分析原因、评分、评分理由、交易动作、加仓价格、减仓价格）。
- **调用**：优先 **LangChain with_structured_output(FullAnalysisOutput)**（Pydantic），失败则 **ask_llm**（Ollama/OpenAI 等）+ 按行解析。

**输出**：解析得到的 10 项 + 与 fundamental/technical 等合并，形成一张 **卡片**（含 ticker、name、score、action、core_conclusion、trend_structure、macd_status、kdj_status、tech_entry_note、tech_exit_note、tech_status_one_line、add_price、reduce_price 等）。

---

## 六、深度模式（deep=1）额外做了什么

**入口**：`run_one_ticker_deep_report`（agents/report_deep.py）。

1. **五类深度分析**（chains/chains.py + agents/analysis_deep.py）  
   每类由 **LangChain 链** 编排：拉数据（或复用 full_analysis 已有数据）→ 拼专用 Prompt → 调 LLM → 得到一段摘要。  
   五类：基本面深度、护城河、同行对比、空头视角、叙事变化。  
   结果写入 **memory_store**（JSONL），并按「标的 + 分析类型」可检索。

2. **与上次对比**（chains/memory_store.py + run_comparison）  
   从 memory_store 取该标的**上次**同类深度结果，再调 LLM 做「大方向是否一致、近期趋势」的对比摘要。

3. **评分微调**  
   把五段深度摘要（可截断）拼给 LLM，要求基于这些对 **full_analysis 的评分** 做 ± 调整或给出最终评分。  
   优先 **LangChain with_structured_output(ScoreAdjustment)**，失败则正则解析「最终评分：N」或「调整：±1」。

4. **富卡片**  
   在基础卡片上追加：五段深度摘要、与上次对比结论、近期趋势、微调后评分等，供报告页展示。

---

## 七、报告总览与既往推荐

- **报告总览**：  
  把所有卡片的「名称、评分、交易动作、核心结论」整理成一段文本，再调 **ask_llm** 一次，生成 **3～5 句话的总览**，并指出优先关注的 1～3 只及理由。  
  结果放在报告顶部「报告总览」区块。

- **既往推荐追踪**（data/recommendations.py）：  
  - 本期 9/10 分且动作为「买入」的，会 **save_recommendation** 写入本地记录。  
  - **get_past_recommendations_with_returns**：取过去 N 天（如 30 天）内所有「9/10 分买入」记录，用 yfinance 算持有至今的收益、胜率、与基准（标普/A股/港股指数）对比、最差收益、收益分布等。  
  这些结果在 HTML 里以「既往推荐表现」表格+摘要形式展示。

---

## 八、生成 HTML（report/build_html.py）

**输入**：cards、title、gen_time、report_summary、backtest_rows、backtest_summary。

**做什么**：

- 顶部：标题、生成时间、**报告总览**（report_summary）、**既往推荐表现**（表格 + 摘要）。
- 每个 card 渲染为一张「卡片」：  
  核心结论、评分、交易动作、加仓/减仓价、**技术面摘要**（tech_status_one_line）、趋势结构、MACD/KDJ、分析原因、技术入场/离场参考、量比、ATR% 等；  
  若 deep=1，再展示五段深度摘要、与上次对比、近期趋势等。
- 内联 CSS + 简单 JS（筛选、排序、统计等），输出一整份可单独打开的 HTML。

**输出**：`html_content` 字符串，作为 `/report` 的响应体；可选写入 `report/output/report-{时间}.html`。

---

## 九、用到的工具与数据源汇总

| 类别 | 工具/数据源 | 用途 |
|------|-------------|------|
| 行情/K 线 | yfinance | 历史 OHLCV、技术指标计算、量比 |
| 新闻 | yfinance news | 消息面列表；可选 LLM 摘要 |
| 财报/估值 | yfinance info、financials、earnings 等 | 市值、PE、涨跌、量比、财报摘要；可选 LLM 解读 |
| 期权 | yfinance options | Put/Call 多空描述 |
| 选股池 | AKShare / Wikipedia / 静态列表（config/tickers + data/universe） | 得到 ticker_list |
| LLM | Ollama / OpenAI / DeepSeek（llm.py、chains/llm_factory） | 综合评分、总览、深度摘要、对比、评分微调、新闻/财报解读 |
| 深度编排 | LangChain（chains/chains.py、agents/analysis_deep.py） | 五类深度链、结构化输出 |
| 记忆 | memory_store（JSONL）、RAG（ChromaDB + embedding） | 与上次对比、历史分析检索 |
| 既往推荐 | data/recommendations.py + yfinance | 记录 9/10 分买入、计算持有收益与胜率 |

---

## 十、达到什么目的

1. **一站式多维度报告**：对一批标的（自选或按市场/池自动选），自动拉取技术、消息、财报、期权，并给出 **统一格式的结论**（结论、趋势、MACD/KDJ、评分、买卖动作、加仓/减仓价）。
2. **可解释**：每只都有「趋势结构」「MACD状态」「KDJ状态」「分析原因」「评分理由」和**技术面一句摘要**，便于人工快速扫一眼。
3. **可选深度**：deep=1 时增加基本面和叙事等深度分析，以及「与上次对比」，适合跟踪持仓或重点标的。
4. **可回溯**：既往推荐追踪 + 报告存档，便于事后看「当时 9/10 分买入的现在表现如何」。
5. **多市场**：支持美股/A股/港股，选股池可配置（大盘/纳斯达克100/罗素2000/中证2000 等），技术面与报告逻辑统一，便于扩展。

整体上，Report 流程 = **数据拉取（多源）→ 多维度摘要（技术/消息/财报/期权）+ 可选 RAG/深度 → 一次或多次 LLM 综合/微调 → 结构化卡片 → 总览 + 回溯 → 单页 HTML 报告**。
