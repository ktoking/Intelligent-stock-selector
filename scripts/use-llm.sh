#!/usr/bin/env bash
# LLM 后端切换脚本（API Key 从 .env 读取，由 Python 加载）
# 用法: ./scripts/use-llm.sh <ollama|minimax|deepseek|openai> [命令...]
# 示例: ./scripts/use-llm.sh minimax
#       ./scripts/use-llm.sh ollama python server.py

set -e
BACKEND="${1:-ollama}"
shift || true

case "$BACKEND" in
  ollama|minimax|deepseek|openai)
    export LLM_BACKEND="$BACKEND"
    ;;
  *)
    echo "用法: $0 <ollama|minimax|deepseek|openai> [命令...]"
    exit 1
    ;;
esac

echo "[use-llm] LLM_BACKEND=$LLM_BACKEND"
PYTHON=""
if [ -f "venv/bin/python" ]; then
  PYTHON="venv/bin/python"
elif command -v python3 &>/dev/null; then
  PYTHON="python3"
else
  PYTHON="python"
fi

if [ $# -gt 0 ]; then
  exec "$@"
else
  exec "$PYTHON" server.py
fi
