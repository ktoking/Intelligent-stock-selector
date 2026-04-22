# Agent 记忆（本仓库）

## 当前默认 LLM 配置（stock-agent）

| 项 | 值 |
|----|-----|
| 后端 | **`LLM_BACKEND=ollama`**（请求发往本机 `http://127.0.0.1:11434`） |
| 模型 | **`OLLAMA_MODEL=glm-5.1:cloud`**（推理在 **Ollama 云端**，本机只跑 `ollama serve`） |
| 密钥 | **`OLLAMA_API_KEY`** 写在 **`.env.local`**（[ollama.com/settings/keys](https://ollama.com/settings/keys)），**不是** `MINIMAX_API_KEY` |

实现位置：**`.env.local`** 已按上表写好；请在本机把 **`OLLAMA_API_KEY=`** 后面填上密钥（空则云端模型会 401/503）。

启动 Ollama（带 Key 注入）：

```bash
./scripts/start-ollama-minimax-cloud.sh
```

未拉取过模型时执行一次：`ollama pull glm-5.1:cloud`。

验证：`curl -s http://127.0.0.1:11434/api/version`；`ollama run glm-5.1:cloud "hi"`。

---

## 与 MiniMax 开放平台的区别

- **Ollama 云端**（如 `glm-5.1:cloud`、`minimax-m2.7:cloud`）：`LLM_BACKEND=ollama` + `OLLAMA_MODEL` + **`OLLAMA_API_KEY`**（Ollama 网站密钥）。
- **MiniMax 开放平台**：`LLM_BACKEND=minimax` + **`MINIMAX_API_KEY`**（api.minimaxi.com）。**不要**与上者混用同一套变量。

---

## 其他

- 纯本地模型（无云端）：`OLLAMA_MODEL=qwen3.5:9b` 等 + `./scripts/start-ollama-for-openclaw.sh`。
- OpenClaw 的 `openclaw.json` 若接 Ollama，模型 id 与当前默认一致（如 **`glm-5.1:cloud`**），`baseUrl` 仍为 **`http://127.0.0.1:11434`**。详见 `docs/OpenClaw-本地模型接入.md`。
