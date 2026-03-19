#!/usr/bin/env bash
# 为 OpenClaw 启动 Ollama，避免 LLM request timed out
# 用法: ./scripts/start-ollama-for-openclaw.sh

set -e
echo "[start-ollama] 停止已有 Ollama..."
pkill -f "ollama serve" 2>/dev/null || true
sleep 2

echo "[start-ollama] 启动 Ollama（OLLAMA_KEEP_ALIVE=300 保持模型常驻）..."
OLLAMA_KEEP_ALIVE=300 ollama serve &
OLLAMA_PID=$!
sleep 5

echo "[start-ollama] 预热 qwen2.5:3b（首次加载约 15-30 秒）..."
curl -s -X POST http://127.0.0.1:11434/api/generate \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen2.5:3b","prompt":"1","stream":false}' >/dev/null 2>&1 || true

echo "[start-ollama] 完成。Ollama PID=$OLLAMA_PID"
echo "[start-ollama] 验证: curl -s http://127.0.0.1:11434/api/version"
