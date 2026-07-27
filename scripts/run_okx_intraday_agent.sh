#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
exec "${PYTHON_BIN:-.venv/bin/python}" scripts/okx_intraday_agent.py --loop
