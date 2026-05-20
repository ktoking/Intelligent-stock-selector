#!/bin/bash
# 启动 stock-agent HTTP 服务。
# 默认关闭内置定时任务，避免登录自启时额外跑日报。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
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

export DAILY_REPORT_SCHEDULE="${DAILY_REPORT_SCHEDULE:-0}"

if [ -x "$ROOT/venv/bin/python" ]; then
  exec "$ROOT/venv/bin/python" server.py "$@"
fi

if [ -x "$ROOT/.venv/bin/python" ]; then
  exec "$ROOT/.venv/bin/python" server.py "$@"
fi

exec python server.py "$@"
