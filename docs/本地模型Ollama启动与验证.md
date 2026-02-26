# 本地模型（Ollama）启动与验证

本项目默认用 **Ollama** 跑本地大模型，无需 API Key。按下面步骤安装、启动并验证即可。

---

## 一、安装 Ollama

1. 打开 **https://ollama.com**，按系统下载安装包（macOS / Windows / Linux）。
2. 安装完成后：
   - **macOS / Windows**：一般会自动在后台启动 Ollama 服务，无需再敲命令。
   - **Linux**：若未自动启动，可执行：
     ```bash
     ollama serve
     ```
     服务默认监听 **http://localhost:11434**。

---

## 二、拉取模型

项目默认使用 **qwen2.5:3b**（体积小、够用）。在终端执行：

```bash
ollama pull qwen2.5:3b
```

或只写：

```bash
ollama pull qwen2.5
```

如需更好效果，可拉取更大模型（耗显存/内存更多）：

```bash
ollama pull qwen2.5:7b
# 使用 7b 时需设置环境变量：export OLLAMA_MODEL=qwen2.5:7b
```

**RAG 用 Ollama 做向量时**，需再拉取嵌入模型：

```bash
ollama pull nomic-embed-text
```

---

## 三、确认 Ollama 已启动

### 方法 1：看已拉取的模型列表

```bash
curl http://localhost:11434/api/tags
```

若返回 JSON（内含 `models` 列表），说明服务正常。

### 方法 2：简单对话测试

```bash
curl -X POST http://localhost:11434/api/generate \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen2.5:3b","prompt":"说一句你好","stream":false}'
```

有正常文本回复即表示模型可用。

### 方法 3：用本项目 LLM 模块验证（推荐）

在项目根目录执行：

```bash
python -c "
from llm import ask_llm
r = ask_llm(system='你是助手。', user='只说一句话：你好')
print('回复:', r)
print('验证通过' if r else '无回复，请检查 Ollama 与模型')
"
```

若无报错且打印出「回复: xxx」和「验证通过」，说明本地模型已正确接好。

---

## 四、用服务接口验证

启动本项目的 HTTP 服务后，用接口间接验证「本地模型是否被调用」：

```bash
# 终端 1：启动服务
python server.py

# 终端 2：健康检查
curl http://127.0.0.1:8000/health

# 单只分析（会调 Ollama）
curl "http://127.0.0.1:8000/analyze?ticker=AAPL"
```

若 `/health` 返回 `{"status":"ok"}`，且 `/analyze?ticker=AAPL` 能返回一段分析文案，说明本地模型已被项目正常使用。

---

## 五、常见问题

| 现象 | 可能原因 | 处理 |
|------|----------|------|
| `Connection refused` / 无法连接 localhost:11434 | Ollama 未启动 | 打开 Ollama 应用，或执行 `ollama serve` |
| `model not found` | 未拉取对应模型 | 执行 `ollama pull qwen2.5:3b` |
| 项目报「无法连接 Ollama」 | 同上，或端口被占 | 确认 11434 端口只有 Ollama 在使用 |
| 想换模型 | 默认是 qwen2.5:3b | 设置环境变量 `OLLAMA_MODEL=qwen2.5:7b` 后重启服务 |

---

## 六、小结

1. **安装**：从 https://ollama.com 安装 Ollama。  
2. **启动**：安装后一般已自动启动；Linux 未启动时用 `ollama serve`。  
3. **拉模型**：`ollama pull qwen2.5:3b`（RAG 用再拉 `nomic-embed-text`）。  
4. **验证**：`curl http://localhost:11434/api/tags` 或项目内 `python -c "from llm import ask_llm; print(ask_llm(system='', user='说你好'))"` 或访问 `GET /health` 与 `GET /analyze?ticker=AAPL`。
