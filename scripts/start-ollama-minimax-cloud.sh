#!/usr/bin/env bash
# 本机 Ollama + Ollama 云端模型 minimax-m2.7:cloud
# 用法（项目根）: ./scripts/start-ollama-minimax-cloud.sh
# 需在 .env / .env.local 中配置 OLLAMA_API_KEY；可选 OLLAMA_MODEL（默认 minimax-m2.7:cloud）

set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -f .env ]; then
  set -a
  # shellcheck source=/dev/null
  source .env
  set +a
fi
if [ -f .env.local ]; then
  set -a
  # shellcheck source=/dev/null
  source .env.local
  set +a
fi

MODEL="${OLLAMA_MODEL:-minimax-m2.7:cloud}"

if [ -z "${OLLAMA_API_KEY:-}" ]; then
  echo "[start-ollama-cloud] 错误: 未设置 OLLAMA_API_KEY。请在 .env 中配置，或: export OLLAMA_API_KEY=..."
  echo "[start-ollama-cloud] 密钥: https://ollama.com/settings/keys"
  exit 1
fi

echo "[start-ollama-cloud] 停止已有 Ollama..."
pkill -f "ollama serve" 2>/dev/null || true
sleep 2

echo "[start-ollama-cloud] 启动 Ollama（OLLAMA_KEEP_ALIVE=300，已注入 OLLAMA_API_KEY）..."
export OLLAMA_API_KEY
OLLAMA_KEEP_ALIVE=300 ollama serve &
OLLAMA_PID=$!
sleep 5

echo "[start-ollama-cloud] 预热模型: $MODEL"
curl -s -X POST http://127.0.0.1:11434/api/generate \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"${MODEL}\",\"prompt\":\"hi\",\"stream\":false}" | head -c 400 || true
echo ""
echo "[start-ollama-cloud] 完成。PID=$OLLAMA_PID 模型=$MODEL"
echo "[start-ollama-cloud] stock-agent 建议 .env: LLM_BACKEND=ollama OLLAMA_MODEL=$MODEL"
echo "[start-ollama-cloud] 验证: curl -s http://127.0.0.1:11434/api/version"
