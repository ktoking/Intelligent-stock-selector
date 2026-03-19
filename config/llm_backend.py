"""
LLM 后端配置：统一管理 ollama | minimax | deepseek | openai 切换。
所有相关环境变量集中在此，便于维护与切换。
"""
import os

# ---------- 后端切换 ----------
# 可选：ollama | minimax | deepseek | openai；空则按 key 推断
LLM_BACKEND = (os.environ.get("LLM_BACKEND", "") or "").strip().lower()

# ---------- API Keys（按需填写） ----------
MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY", "").strip()
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()

# ---------- 各后端专属配置 ----------
# Ollama：本地模型名
OLLAMA_MODEL = (os.environ.get("OLLAMA_MODEL", "qwen3.5:9b") or "qwen3.5:9b").strip()

# MiniMax：国内 api.minimaxi.com，海外 api.minimax.io
MINIMAX_API_BASE = (os.environ.get("MINIMAX_API_BASE", "") or "https://api.minimaxi.com/v1").strip()
MINIMAX_MODEL = (os.environ.get("MINIMAX_MODEL", "MiniMax-M2.5") or "MiniMax-M2.5").strip()

# DeepSeek：固定
DEEPSEEK_BASE = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"

# OpenAI：默认
OPENAI_MODEL = (os.environ.get("OPENAI_MODEL", "gpt-4o-mini") or "gpt-4o-mini").strip()

# ---------- 通用参数 ----------
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "120") or "120")
