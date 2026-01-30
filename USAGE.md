# Stock Agent 使用说明

## 一、本地是否已启动

- 服务在 **8000 端口**。在项目目录下执行：
  ```bash
  curl -s http://127.0.0.1:8000/health
  ```
  若返回 `{"status":"ok"}` 说明已启动；否则需要先启动（见下文「如何启动」）。

## 二、访问什么可以拿到结果

| 用途 | 地址 | 说明 |
|------|------|------|
| 健康检查 | http://127.0.0.1:8000/health | 确认服务是否存活 |
| 接口文档 | http://127.0.0.1:8000/docs | Swagger 文档，可在线试调 |
| 单只基本面分析（文本） | http://127.0.0.1:8000/analyze?ticker=AAPL | 返回该股票基本面分析文案 |
| **多只优秀资产报告（HTML）** | http://127.0.0.1:8000/report | 默认按市值+近期增长取 **100 只**，日 K 技术+消息+财报+期权 |
| **深度报告（含①②③④⑤+与上次对比）** | http://127.0.0.1:8000/report?deep=1&limit=5 | 每只跑深度分析+记忆对比，输出大方向是否一致、近期趋势，可筛「仅显示大方向不变」 |
| 指定只数 | http://127.0.0.1:8000/report?limit=20 | 取前 20 只 |
| 指定股票 | http://127.0.0.1:8000/report?tickers=AAPL,MSFT,NVDA | 只分析这几只 |
| 10 分钟 K 线报告 | http://127.0.0.1:8000/report?interval=10m&limit=5 | 超短线（10m 内部用 15m 数据） |

**拿到结果的方式**：浏览器直接打开上述 URL；报告页可「另存为」保存为 HTML 文件。  
**K 线周期**：`interval=1d` 日 K；`5m`/`15m`/`10m`/`1m` 分 K（`10m` 以 15m 数据代替，yfinance 无 10m）。

### 6 类深度分析（融合标准 Prompt，可横向对比）

| 分析类型 | 地址 / 方法 | 说明 |
|----------|-------------|------|
| ① 基本面深度 | GET /analyze/deep?ticker=AAPL | 收入与增长质量、盈利能力、现金流、商业模式、中长期风险（标准模板） |
| ② 护城河 | GET /analyze/moat?ticker=AAPL | 技术/切换成本/网络/规模/品牌壁垒，强/中/弱/无及削弱路径 |
| ③ 同行对比 | GET /analyze/peers?ticker=AAPL&peers=MSFT,GOOGL | 增速/盈利/商业模式/估值差异，高估/合理/低估及市场可能看错之处 |
| ④ 空头视角 | GET /analyze/short?ticker=AAPL | 增长可持续性、替代风险、依赖度、估值、下跌触发点 |
| ⑤ 叙事变化 | GET /analyze/narrative?ticker=AAPL | 财报与管理层话术变化、正面/警惕信号 |
| ⑥ 假设拆解 | POST /analyze/thesis，body: `{"ticker":"AAPL","hypothesis":"你的假设"}` | 关键前提、最易证伪前提、失败最可能原因 |
| **组合（①②③④）** | GET /analyze/full?ticker=AAPL | 一次返回 4 段分析（JSON） |
| **组合+⑤** | GET /analyze/full?ticker=AAPL&narrative=1 | 含 ⑤ 财报与叙事变化 |

## 三、如何切换模型（Ollama）

当前默认使用 **qwen2.5:3b**。要换模型有两种方式。

### 方式 1：环境变量（推荐）

启动服务**之前**设置：

```bash
export OLLAMA_MODEL=qwen2.5:7b
./venv/bin/python server.py
```

或一行：

```bash
OLLAMA_MODEL=qwen2.5:7b ./venv/bin/python server.py
```

### 方式 2：改代码默认值

编辑 `llm.py`，找到使用 Ollama 时的 `DEFAULT_MODEL` 那一行，把 `"qwen2.5:3b"` 改成你要的模型名，例如 `"qwen2.5:7b"`。

### 使用新模型前需先拉取

```bash
ollama pull qwen2.5:7b    # 或 qwen2.5:14b、llama3.2 等
```

切换模型后需**重启**本服务（或重新运行 `python server.py`）才会生效。

## 四、如何启动 / 停止服务

**启动：**

```bash
cd /Users/kaiyi.wang/PycharmProjects/stock-agent
./venv/bin/python server.py
```

或指定模型后启动：

```bash
OLLAMA_MODEL=qwen2.5:7b ./venv/bin/python server.py
```

**停止：** 在运行服务的终端里按 `Ctrl+C`。

**后台运行示例：**

```bash
nohup ./venv/bin/python server.py > server.log 2>&1 &
```

## 五、AI Agent 调优（模型参数、关键字、Prompt）

可从**模型参数**、**关键字/输出约束**、**Prompt 与角色**三方面调优，使报告更稳定或更贴合风格。

### 5.1 模型参数（环境变量）

| 变量 | 含义 | 推荐/默认 |
|------|------|-----------|
| `LLM_TEMPERATURE` | 采样温度，0~2；越低输出越稳定，适合分析 | `0.2`（结构化分析建议保持较低） |
| `LLM_MAX_TOKENS` | 单次回复最大 token 数；不设则用模型默认 | 不设（若回复过长可设如 `2048`） |
| `OLLAMA_MODEL` | Ollama 模型名 | `qwen2.5:3b`，可改为 `qwen2.5:7b` 等 |
| `LLM_BACKEND` | 后端：`ollama` / `deepseek` / `openai` | 有 API Key 时自动或显式指定 |
| `LLM_TIMEOUT` | 单次请求超时（秒） | `120` |

示例：

```bash
LLM_TEMPERATURE=0.1 LLM_MAX_TOKENS=2048 OLLAMA_MODEL=qwen2.5:7b ./venv/bin/python server.py
```

以上参数在 `llm.ask_llm` 与 `chains/llm_factory` 中统一生效（配置来自 `config/llm_config.py`）。

### 5.2 关键字与输出约束

- **Report 交易动作校验（可选）**：环境变量 `REPORT_ACTION_KEYWORDS` 可设为逗号分隔的关键词列表，用于自检或过滤「交易动作」是否落在预期集合内；不设则不校验。配置在 `config/llm_config.py`。
- **新闻**：当前直接使用 yfinance 返回的新闻列表，未做关键词过滤；若需只保留「财报/业绩/收购/诉讼」等，可在 `agents/news.py` 中按标题或摘要做关键词过滤或打标。
- **加仓/减仓价格**：Prompt 中已要求结合技术面入场/离场参考给出数字或「—」；若希望更严格，可在 `agents/full_analysis.py` 的 `_parse_llm_output` 后增加校验（如必须为数字或「—」）。

### 5.3 Prompt 与角色

- **Report 综合（9 项输出）**：`agents/full_analysis.py` 中 `_build_prompt` 构造用户 Prompt，`ask_llm` 的 `system` 为「美股多维度分析师，严格按 9 项格式输出」。若希望语气更保守/激进，可改 system 或在该文件中增加「保守/中性/激进」分支，后续可与 `config/llm_config.py` 的 `PROMPT_TONE` 联动。
- **6 类深度分析**：`agents/prompts.py` 中为 ① 基本面深度、② 护城河、③ 同行、④ 空头、⑤ 叙事、⑥ 假设拆解的模板；`agents/analysis_deep.py` 中每条调用配有独立 system 描述。调优时可直接改对应模板或 system 字符串（如强调「不给出买卖建议」「只列有逻辑链条的风险」等）。
- **基本面简短分析**：`agents/fundamental.py` 中构造 prompt 并传入 system「长期价值投资取向、避免情绪化和炒作」。可按需要微调措辞。

### 5.4 可编辑文件速查

| 目的 | 文件 |
|------|------|
| 温度、max_tokens、动作关键词、PROMPT_TONE | `config/llm_config.py` |
| 模型/后端/超时 | `llm.py`（及环境变量） |
| Report 9 项格式与角色 | `agents/full_analysis.py`（`_build_prompt` + `ask_llm` 的 system） |
| 6 类深度分析模板 | `agents/prompts.py` |
| 6 类深度 system 描述 | `agents/analysis_deep.py` |
| 新闻条数/来源 | `agents/news.py` |
