**筛选器**
<img width="3160" height="1828" alt="image" src="https://github.com/user-attachments/assets/021529d0-9271-425b-b8fc-1ec35d008c09" />
**整体分析**
<img width="1584" height="1918" alt="image" src="https://github.com/user-attachments/assets/b3cc99a8-43a3-4db6-92ef-927d22478cae" />

---

# Stock Agent

多市场股票分析服务：技术面 + 消息面 + 财报 + 期权 + LLM 综合评分，支持美股 / A股 / 港股，可选深度分析（基本面深度、护城河、同行对比、空头视角、叙事变化）与「与上次对比」。默认本地 Ollama，无需 API Key。

---

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 本地模型（需先安装 Ollama 并拉取模型）
ollama pull qwen2.5:3b
python server.py
```

服务默认在 **http://127.0.0.1:8000**。健康检查：`curl http://127.0.0.1:8000/health`；接口文档：http://127.0.0.1:8000/docs 。

---

## Report 接口（核心）

**GET /report** 生成多只标的的 HTML 报告，支持大盘/小盘、多市场、日 K/分 K、深度分析。

### 报告在线页（推荐）

打开 **GET /report/page**（如 http://127.0.0.1:8000/report/page）即可在浏览器里选市场、数量、深度等，点击「生成报告」后：

- 页面会**轮询进度**，显示「当前第 x/y 只：TICKER」及已完成数、失败列表；
- 生成完成后**报告直接展示在同一页**（iframe），无需复制到浏览器或另开窗口；
- 可点击「在新标签页打开当前报告」用当前 HTML 新开一页。

适合本地或内网使用；后续可将前端与 API 部署到 **Cloudflare Pages / Workers**，通过同一域名访问。

### 参数一览

| 参数 | 说明 | 默认 |
|------|------|------|
| **tickers** | 逗号分隔股票代码；不传则按 market+pool 取池 | — |
| **limit** | 不传 tickers 时取的数量 | 5 |
| **market** | 市场：`us` 美股 / `cn` A股 / `hk` 港股 | us |
| **pool** | 选股池：不传或 `sp500` 大盘；`nasdaq100` 纳斯达克100；`russell2000` 美股小盘；`csi2000` A股小盘；`hsi` 恒指；`hstech` 恒科 | — |

选股池**优先从线上拉取**（Wikipedia 等）：纳斯达克100、恒生指数、恒生科技指数、沪深300、罗素2000（若有表）会先尝试线上成分表，失败再回退到项目内静态列表；美股大盘 `limit>10` 仍从 S&P 500 线上拉取后按市值+近期增长排序。`limit` 上限已放宽到 500，便于做全量或大批量分析。
| **deep** | 1=每只跑深度分析①②③④⑤+与上次对比；0=仅技术+消息+财报+期权+综合评分 | 0 |
| **interval** | K 线：`1d` 日 K；`5m`/`15m`/`10m`/`1m` 分 K（10m 内部用 15m） | 1d |
| **prepost** | 1=含盘前盘后（日 K 时涨跌幅为盘前/盘后价） | 0 |

### 股票代码格式

- **美股**：直接写代码，如 `AAPL,MSFT,NVDA`。
- **A 股**：可传 6 位数字，自动补交易所后缀：  
  `001317,603767` → `001317.SZ`（深圳）、`603767.SS`（上海）。  
  也可直接写 `600519.SS,000858.SZ`。
- **港股**：可传 4 位数字，自动补 `.HK`：`0700` → `0700.HK`。

### 常用示例

| 用途 | URL |
|------|-----|
| 美股大盘前 20 只（日 K） | `/report?market=us&limit=20` |
| 美股纳斯达克100 前 20 只 | `/report?market=us&pool=nasdaq100&limit=20` |
| 美股小盘（罗素2000 风格）前 10 只 | `/report?market=us&pool=russell2000&limit=10` |
| A股小盘（中证2000 风格）前 10 只 | `/report?market=cn&pool=csi2000&limit=10` |
| 港股恒指前 20 只 | `/report?market=hk&pool=hsi&limit=20` |
| 港股恒科前 20 只 | `/report?market=hk&pool=hstech&limit=20` |
| 指定 A 股（6 位自动补 .SS/.SZ） | `/report?tickers=001317,603767,600882` |
| 深度报告（含①②③④⑤+与上次对比） | `/report?deep=1&limit=5` |
| 分 K 超短线（15 分钟 K） | `/report?interval=15m&limit=10` |
| 日 K + 盘前盘后涨跌幅 | `/report?interval=1d&prepost=1&limit=5` |

### 报告行为说明

- **自动保存**：每次生成后会将 HTML 写入 `report/output/report-MMDD-HHMM.html`（如 `report-0130-1432.html`），无需手动下载。
- **进度**：生成时可轮询 **GET /report/progress** 查看当前第几只、成功数、失败列表。
- **评分**：10～1 分制（10 最强）；交易动作为「买入 / 观察 / 离场」；所属板块展示为中文（美股常见行业已映射）。
- **深度模式**（`deep=1`）：依赖 LangChain（`langchain-core`、`langchain-openai`）；会跑 ①②③④⑤ 并做「与上次对比」，且对综合评分做一次轻量 LLM 微调。深度分析默认并行执行以缩短耗时；可设 `DEEP_PARALLEL=0` 改为顺序。
- **既往推荐追踪与胜率**：每次报告中 9/10 分且「买入」的标会自动写入 `data/memory/recommendations.jsonl`；下次报告会拉取过去 30 天内这些推荐标的的「今日价」并计算涨跌幅与持有天数，在报告顶部展示「既往推荐表现」表格及胜率/平均涨跌幅，相当于轻量回测。

### 每日定时报告（9 点自动跑三份）

脚本 `scripts/daily_report.py` 可每日自动跑三份报告：**美股 SP500 100 只**、**A 股中证 2000 100 只**、**港股恒指 100 只**，`deep=0` 不跑深度分析。

**前置**：服务需已启动（`python server.py`），可配合 systemd / screen 常驻。**周末及中国法定节假日自动跳过**（可 `--force` 强制执行）；节假日判断需 `pip install chinese_calendar`。

```bash
# 手动执行
python scripts/daily_report.py

# 定时（crontab -e 添加）
0 9 * * * cd /path/to/stock-agent && python scripts/daily_report.py

# 周末/节假日强制运行
python scripts/daily_report.py --force
```

报告保存到 `report/output/report-MMDD-HHMM.html`，三份会按完成时间生成不同文件名。

### 评分说明（详细）

报告里的 **评分** 为 **10～1 分制**（10 最强、1 最弱），由两阶段组成。

**1）综合评分（必做）**

- 每只标的先拉取：**技术面**（均线、MACD、KDJ、入场/离场参考）、**消息面**（近期新闻约 5 条）、**财报/估值/期权**（公司、行业、市值、当前价、涨跌幅、PE、期权多空、财报摘要前 800 字）。
- 上述内容拼成一段 **User Prompt**，再配上 **System**：「你是多维度分析师，按 9 项格式输出」。
- 模型输出 9 项，其中一项为 **「评分：&lt;10-1 的数字&gt;」**。
- 代码从该行解析出数字，并 **截断到 [1, 10]**；若解析失败则用默认 **5**。  
→ 也就是说：**分数完全由 LLM 根据「技术 + 消息 + 财报/估值/期权」综合给出**，没有公式或规则分，只有 Prompt 里的「10=最强、1=最弱」的说明。

**2）深度微调（仅当 deep=1 时）**

- 在综合评分之后，会跑 **①②③④⑤** 深度分析（基本面深度、护城河、同行对比、空头视角、叙事变化）。
- 取每段摘要的 **前 200 字**，拼成一段短文，再发起 **一次轻量 LLM 调用**，要求模型在「当前评分」基础上做微调。
- 模型只能回复两种格式之一：**「最终评分：N」**（N 为 1～10 整数）或 **「调整：+1 / 0 / -1」**。
- 若为「最终评分：N」，则最终分数 = N（并 clamp 到 1～10）；若为「调整：±1」，则最终分数 = 原分 + 调整量（再 clamp）。  
→ 也就是说：**深度模式下的分数 = 综合评分 + 基于五段深度摘要的一次微调**；微调失败或未返回有效结果时，保留综合评分。

**报告页的定性解读**

- 分数在报告里会映射成一句定性：**9～10 → 强烈看好**，**7～8 → 偏多**，**5～6 → 可配置**，**3～4 → 关注**，**1～2 → 观望**。  
（映射逻辑在 `report/build_html.py` 的 `_score_interpretation`。）

**小结**

- **谁在评**：综合评分 = 一次 LLM（技术+消息+财报+期权）；深度微调 = 再一次 LLM（五段深度摘要）。
- **依据什么**：综合阶段看的是均线/MACD/K DJ、新闻、市值/PE/涨跌幅/期权/财报摘要；微调阶段看的是五段深度摘要的短文。
- **分数范围**：始终 1～10，解析后会做截断；无公式，全靠模型在 Prompt 约束下输出。

---

## 其他接口

| 用途 | 方法 | 说明 |
|------|------|------|
| 单只基本面（文本） | GET /analyze?ticker=AAPL | 简短基本面分析文案 |
| ① 基本面深度 | GET /analyze/deep?ticker=AAPL | 收入与增长、盈利、现金流、商业模式、风险 |
| ② 护城河 | GET /analyze/moat?ticker=AAPL | 技术/切换成本/网络/规模/品牌壁垒 |
| ③ 同行对比 | GET /analyze/peers?ticker=AAPL&peers=MSFT,GOOGL | 增速/盈利/估值差异 |
| ④ 空头视角 | GET /analyze/short?ticker=AAPL | 增长可持续性、替代风险、估值、下跌触发点 |
| ⑤ 叙事变化 | GET /analyze/narrative?ticker=AAPL | 财报与话术变化 |
| ⑥ 假设拆解 | POST /analyze/thesis，body: `{"ticker":"AAPL","hypothesis":"假设"}` | 关键前提、证伪点、失败原因 |
| 组合 ①②③④⑤ | GET /analyze/full?ticker=AAPL&narrative=1 | 一次返回多段分析（JSON） |
| 长期记忆检索 | GET /memory?ticker=AAPL&analysis_type=fundamental_deep | 历史分析记录 |
| 上次分析摘要 | GET /memory/context?ticker=AAPL&analysis_type=fundamental_deep | 用于对比的摘要文本 |

---

## 模型与后端

- **默认**：本地 Ollama，模型名由环境变量 `OLLAMA_MODEL` 控制（默认 `qwen2.5:3b`）。
- **切换模型**：`export OLLAMA_MODEL=qwen2.5:7b` 后启动；或 `ollama pull qwen2.5:7b` 再设环境变量。
- **云端**：设置 `DEEPSEEK_API_KEY` 或 `OPENAI_API_KEY`，可选 `LLM_BACKEND=deepseek` / `openai`，详见 `llm.py` 注释。

---

## 启动与停止

```bash
# 前台
python server.py

# 指定模型
OLLAMA_MODEL=qwen2.5:7b python server.py

# 后台
nohup python server.py > server.log 2>&1 &
```

停止：前台运行时 `Ctrl+C`。

---

## AI 与报告调优

### 环境变量（模型与行为）

| 变量 | 含义 | 默认 |
|------|------|------|
| `LLM_TEMPERATURE` | 采样温度，越低越稳定 | 0.3 |
| `LLM_MAX_TOKENS` | 单次回复最大 token | 不设 |
| `OLLAMA_MODEL` | Ollama 模型名 | qwen2.5:3b |
| `LLM_BACKEND` | ollama / deepseek / openai | 按 Key 推断 |
| `LLM_TIMEOUT` | 请求超时（秒） | 120 |
| `DEEP_PARALLEL` | 深度分析是否并行，0=顺序 | 1 |

### 可编辑文件速查

| 目的 | 文件 |
|------|------|
| 温度、max_tokens、PROMPT_TONE | `config/llm_config.py` |
| 模型/后端/超时 | `llm.py` |
| Report 9 项格式与综合 Prompt | `agents/full_analysis.py` |
| 深度分析 ①②③④⑤ 模板 | `agents/prompts.py`、`chains/chains.py` |
| 选股池、A股/港股代码规范化 | `config/tickers.py` |
| 报告 HTML 与筛选逻辑 | `report/build_html.py` |

---

## 免责声明

本报告仅供参考，不构成任何投资建议。数据来源于公开信息，可能存在延迟或误差。投资有风险，决策需谨慎。
