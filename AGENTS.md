# Agent 记忆（本仓库）

## 当前默认 LLM 配置（stock-agent）

| 项 | 值 |
|----|-----|
| 后端 | **`LLM_BACKEND=ollama`**（请求发往本机 `http://127.0.0.1:11434`） |
| 模型 | **`OLLAMA_MODEL=gemma4:31b-cloud`**（推理在 **Ollama 云端**，本机只跑 `ollama serve`） |
| 密钥 | **`OLLAMA_API_KEY`** 写在 **`.env.local`**（[ollama.com/settings/keys](https://ollama.com/settings/keys)），**不是** `MINIMAX_API_KEY` |

实现位置：**`.env.local`** 已按上表写好；请在本机把 **`OLLAMA_API_KEY=`** 后面填上密钥（空则云端模型会 401/503）。

启动 Ollama（带 Key 注入）：

```bash
./scripts/start-ollama-minimax-cloud.sh
```

未拉取过模型时执行一次：`ollama pull gemma4:31b-cloud`。

验证：`curl -s http://127.0.0.1:11434/api/version`；`ollama run gemma4:31b-cloud "hi"`。

---

## 与 MiniMax 开放平台的区别

- **Ollama 云端**（如 `gemma4:31b-cloud`、`minimax-m2.7:cloud`）：`LLM_BACKEND=ollama` + `OLLAMA_MODEL` + **`OLLAMA_API_KEY`**（Ollama 网站密钥）。
- **MiniMax 开放平台**：`LLM_BACKEND=minimax` + **`MINIMAX_API_KEY`**（api.minimaxi.com）。**不要**与上者混用同一套变量。

---

## 其他

- 纯本地模型（无云端）：`OLLAMA_MODEL=qwen3.5:9b` 等 + `./scripts/start-ollama-for-openclaw.sh`。
- OpenClaw 的 `openclaw.json` 若接 Ollama，模型 id 与当前默认一致（如 **`gemma4:31b-cloud`**），`baseUrl` 仍为 **`http://127.0.0.1:11434`**。详见 `docs/OpenClaw-本地模型接入.md`。

## AI 量化实验室（quant_agent）

### 私人项目：禁止 Skynet 关联与上报

- 本仓库 `/Users/kaiyi.wang/PycharmProjects/stock-agent` 及 `github.com/ktoking/Intelligent-stock-selector` 是用户私人项目。用户已要求关闭所有 Skynet 代码、提示词、技能、Git diff、度量与统计上报，以及由这些上报触发的通知。
- 本项目不要调用 Skynet 生码、Harness、提交归因或度量流程；普通本地开发、Codex 分析与 Futunn 行情/模拟框架继续使用。
- 本机通过 `/Users/kaiyi.wang/.config/codex-private-projects/skynet_hook_guard.py` 和同目录 `excluded-projects.json` 在 Skynet 钩子处理输入之前排除本项目；按路径及 GitHub 仓库身份覆盖 Codex、Cursor、Claude。其他独立公司项目维持原配置。
- 本仓库 `.codex/config.toml` 禁用 `features.codex_hooks` 与 `features.hooks`；`quant_agent` 的 Codex CLI 子进程也显式禁用这两个功能。不要重新启用或绕过项目排除配置。
- 不通过忽略研究数据或缩小 Git diff 来恢复上报，不删除源码、实验数据或 GitHub remote 来处理这类通知。

- 用户已指定量化研究及每日复盘使用 **Codex CLI**，`quant_agent` 默认 `ai_provider=codex`，不自动回退到 Ollama。
- 授权的模拟预算为股票 **USD 10,000**、期权 **USD 1,000**；实盘须另行授权。本模块没有实盘交易适配器。
- Futunn AppKey REST 优先，无 OpenD；只允许经过核实的 `/api/v1.0/sim-trade/` 股票订单接口。期权账户/历史链未验证时关闭期权执行。
- 自选股和更广的市场异动只加入新版本研究池；已开始 Shadow/模拟盘的策略冻结成员、规则和数据基准。
- `.quant` 附件只作为研究证据解析；不执行其中的订单代码，不把日线回测称为原生5分钟策略回测。
- 运行状态、账户ID、实验材料在 `~/.local/share/stock-agent/quant-agent/`；私钥保持在外部既有路径，不加入仓库或日志。
- 运行与权限门槛见 `docs/AI量化实验室-架构与运行.md`；验证使用项目 `.venv`。

- 当前指数成分从 Futunn `valuation/index-stocks` 全分页获取（US..NDX / US..SPX），与自选去重；不把今日名单当历史时点成分。
- 正常开市：订单风控60秒，广池300秒，资讯1800秒，WebSocket重点最多40只；休市关闭正常网络轮询。
- Codex CLI 显式 `gpt-6.1-sol / high`，240秒超时；看板区分请求设置与实际运行头。
- 新表达式因子每数据版本最多1个、研究试验每版本最多30个。受限DSL生成独立Python代码并因果检查、回测、核账、真实未来验证；所有失败记录保留。不要热覆盖冻结策略、交易适配器或预算。
- 模拟实绩只来自券商模拟成交与账本估值。累计成交幂等增量，平均成本核账；费用为估算、成交时间为核对时间，禁止混入历史回测或Shadow假设成交。

- 用户2026-10-14授权自动择优模拟晋升：仅休市替换空仓、无未完成订单且资金已交收的股票槽位；有持仓的策略保留，候选排队。股票总额度10000美元、期权1000美元独立，每个完成交易日最多替换一次。
- 晋升必须通过历史门槛与独立核账，以同一行情、同一区间比较验证/回顾收益与回撤；同规则版本优先竞争自身槽位，不能为了排名重复占用其他槽位。模拟等待期被用户豁免，不标为未来验证通过。
- 资金交接使用strategy_funding双边流水；旧策略保留原规则、冻结哈希、交易与盈亏记录。退役资金移出不当成亏损，不重置券商余额，不把总预算扩大。
- 交易暂停不应阻断有效数据上的研究；每日复盘完成不代表因子流程完成。research_cycle_completed按交易日和数据目录独立记录，缺失环节重试并展示实际失败、代码、检验和晋升证据。
