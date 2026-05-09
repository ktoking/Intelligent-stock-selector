#!/usr/bin/env bash
# Run Ollama as a foreground service with the repo's cloud-model environment.

set -euo pipefail

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

if [ -z "${OLLAMA_API_KEY:-}" ]; then
  echo "[serve-ollama-cloud] missing OLLAMA_API_KEY in .env.local or environment" >&2
  exit 1
fi

export OLLAMA_API_KEY
export OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-300}"

exec /opt/homebrew/bin/ollama serve
