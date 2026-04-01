# OpenClaw 接入本地模型说明

OpenClaw **可以**接入本地模型，常见方式有两种：**Ollama**、**LM Studio**（以及任意兼容 OpenAI API 的本地服务）。本项目的报告服务默认用的就是本地 Ollama，和 OpenClaw 共用同一套本地模型完全可行。

---

## 一、配置文件位置

- **路径**：` ~/.openclaw/openclaw.json `
- 若目录不存在：先执行一次 ` openclaw onboard ` 或 ` openclaw gateway start `，会生成配置目录；再按下面示例编辑或合并配置。

---

## 二、方式一：Ollama（推荐，与本项目一致）

你本机若已安装 **Ollama**（本项目默认也用 Ollama），可直接让 OpenClaw 走同一服务。

### 1. 确认 Ollama 已运行（避免 LLM request timed out）

**推荐**：用脚本启动并预热，避免冷启动超时：

```bash
# 一键启动（默认预热 qwen3.5:9b，与 OpenClaw primary 一致）
./scripts/start-ollama-for-openclaw.sh

# 换小模型预热：MODEL=qwen2.5:3b ./scripts/start-ollama-for-openclaw.sh
```

或手动：

```bash
# 若未安装：https://ollama.com 下载安装后执行
ollama pull qwen3.5:9b               # 未拉取时先执行
OLLAMA_KEEP_ALIVE=300 ollama serve   # 保持模型常驻 5 分钟，减少冷启动
ollama run qwen3.5:9b "hi"           # 预热（首次约 20-40 秒）
ollama list                          # 查看已拉取模型
```

**超时原因**：大模型（如 glm-4.7-flash 19GB）冷启动需 30-60 秒，OpenClaw 默认超时更短。建议用 **qwen2.5:3b**（1.9GB）或 qwen3.5:9b（6.6GB），并预热后再用 OpenClaw。

Ollama 默认地址：` http://127.0.0.1:11434 `（**不要**在 OpenClaw 里加 `/v1`，否则工具调用可能异常）。

### 2. 在 openclaw.json 里配置 Ollama

在 ` ~/.openclaw/openclaw.json ` 的 ` models.providers ` 中加入（或合并）：

```json
{
  "models": {
    "mode": "merge",
    "providers": {
      "ollama": {
        "baseUrl": "http://127.0.0.1:11434",
        "apiKey": "ollama-local",
        "api": "ollama",
        "models": [
          {
            "id": "qwen2.5:7b",
            "name": "Qwen2.5 7B",
            "contextWindow": 32768,
            "maxTokens": 8192
          }
        ]
      }
    }
  },
  "agents": {
    "defaults": {
      "model": {
        "primary": "ollama/qwen2.5:7b"
      }
    }
  }
}
```

- **baseUrl**：固定为 ` http://127.0.0.1:11434 `，不要加 `/v1`。
- **api**：必须为 ` "ollama" `，不要用 ` openai-completions `。
- **id**：必须和 ` ollama list ` 里的模型名一致（含 tag），例如 ` qwen2.5:7b `、` qwen2.5:3b `。
- 若希望同时保留云端模型作备用，保留 ` "mode": "merge" `，并在 ` providers ` 里继续配置其他提供商即可。

### 3. Ollama Cloud：`minimax-m2.7:cloud`（本机 OpenClaw 调云端推理）

OpenClaw 仍使用 **`baseUrl`: `http://127.0.0.1:11434`** + **`api`: `"ollama"`**，模型 id 写 **`minimax-m2.7:cloud`**；实际推理在 **Ollama 云端**，本机只跑 Ollama 客户端。

**必须同时满足：**

1. **Ollama 账号**：终端执行一次 `ollama signin`（浏览器登录 [ollama.com](https://ollama.com)）。
2. **API Key**：在 [ollama.com/settings/keys](https://ollama.com/settings/keys) 创建密钥，本机环境变量：
   ```bash
   export OLLAMA_API_KEY="你的密钥"
   ```
   启动 **`ollama serve`** 的终端 / LaunchAgent 也要带上该变量（否则本地会 `unauthorized`）。
3. **拉取 manifest**：`ollama pull minimax-m2.7:cloud`
4. **`openclaw.json`** 示例：
   ```json
   "agents": {
     "defaults": {
       "model": { "primary": "ollama/minimax-m2.7:cloud" },
       "models": { "ollama/minimax-m2.7:cloud": {} }
     }
   }
   ```
   并在 `models.providers.ollama.models` 里增加 id 为 `minimax-m2.7:cloud` 的条目（`contextWindow` 可写 `200000`）。

5. 改配置后：`openclaw gateway restart`

> 与 **MiniMax 开放平台**（`api.minimaxi.com` + `MINIMAX_API_KEY`）不是同一路；本项目 `stock-agent` 若用 minimax provider，与 Ollama Cloud 互不冲突。

---

## 三、方式二：LM Studio

LM Studio 默认在 ` http://127.0.0.1:1234/v1 ` 暴露 OpenAI 兼容接口。

### 1. 在 LM Studio 中

- 下载并加载你需要的模型（如 MiniMax M2.1、Qwen 等）。
- 开启「Local Server」，确认地址为 ` http://127.0.0.1:1234 `。

### 2. 在 openclaw.json 里配置 LM Studio

```json
{
  "models": {
    "mode": "merge",
    "providers": {
      "lmstudio": {
        "baseUrl": "http://127.0.0.1:1234/v1",
        "apiKey": "lmstudio",
        "api": "openai-responses",
        "models": [
          {
            "id": "minimax-m2.1-gs32",
            "name": "Minimax M2.1",
            "reasoning": false,
            "input": ["text"],
            "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 },
            "contextWindow": 196608,
            "maxTokens": 8192
          }
        ]
      }
    }
  },
  "agents": {
    "defaults": {
      "model": { "primary": "lmstudio/minimax-m2.1-gs32" }
    }
  }
}
```

- **id**：需与 LM Studio 里「Local Server」页显示的模型 ID 一致。
- 若用其他模型，把 ` id `、` name `、` contextWindow `、` maxTokens ` 改成你当前模型即可。

---

## 四、其他本地服务（vLLM、LiteLLM、自建网关等）

只要服务提供 **OpenAI 风格** 的 `/v1` 接口，都可以按下面方式接成一家 provider：

```json
{
  "models": {
    "mode": "merge",
    "providers": {
      "local": {
        "baseUrl": "http://127.0.0.1:8000/v1",
        "apiKey": "sk-local",
        "api": "openai-responses",
        "models": [
          {
            "id": "your-model-id",
            "name": "本地模型",
            "contextWindow": 128000,
            "maxTokens": 8192
          }
        ]
      }
    }
  }
}
```

把 ` baseUrl ` 和 ` id ` 换成你本地服务的地址和模型名即可。

---

## 五、修改配置后

1. 若网关在跑：重启网关  
   ` openclaw gateway start `（或先停再起）。
2. 检查模型是否可见：  
   - Ollama：` curl http://127.0.0.1:11434/api/tags `  
   - LM Studio：` curl http://127.0.0.1:1234/v1/models `  
3. 运行自检：` openclaw doctor `。

---

## 六、与本项目（stock-agent）的关系

| 项目 | 用途 |
|------|------|
| **stock-agent** | 报告、分析用的 LLM 由 `llm.py` 控制，默认读 `OLLAMA_MODEL`，连本机 Ollama。 |
| **OpenClaw** | 桌面智能体，通过 `~/.openclaw/openclaw.json` 配置模型。 |

两边可以**共用同一台 Ollama**：Ollama 同时服务 stock-agent 与 OpenClaw，只需在 OpenClaw 里按上面配置好 `ollama` 的 provider 和默认 `primary` 模型即可。

---

**小结**：可以接入本地模型；Ollama 与 LM Studio 最常见；配置写在 ` ~/.openclaw/openclaw.json `，Ollama 的 ` baseUrl ` 不要带 `/v1`，` api ` 填 ` "ollama" `。
