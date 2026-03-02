"""
LLM 调用：默认使用本地免费的 Ollama，可选 DeepSeek 等云端 API。

免费用法（推荐）：
  1. 安装 Ollama：https://ollama.com
  2. 拉取模型：ollama pull qwen2.5:7b  或  ollama pull qwen2.5
  3. 不设置任何 API Key，直接运行 python main.py

云端用法（需付费/配额）：
  export DEEPSEEK_API_KEY=your-key   # 使用 DeepSeek
  或
  export OPENAI_API_KEY=your-key    # 使用 OpenAI（base_url 需自行配置）

调优（环境变量）：
  LLM_TEMPERATURE=0.2   # 采样温度，越低输出越稳定，适合分析
  LLM_MAX_TOKENS=2048   # 单次回复最大 token，不设则用模型默认
  OLLAMA_MODEL=qwen2.5:7b   # Ollama 模型名
"""
import os
from openai import OpenAI

from config.llm_config import LLM_TEMPERATURE, LLM_MAX_TOKENS

# 后端选择：ollama（本地免费）| deepseek | openai
_backend = os.environ.get("LLM_BACKEND", "").strip().lower()
_deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
_openai_key = os.environ.get("OPENAI_API_KEY", "").strip()

# LLM 请求超时（秒），避免 report 一直转圈
_LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "120"))

# 默认优先 Ollama（无 key 即用本地），有 key 时可按 LLM_BACKEND 或 key 推断
if _backend == "deepseek" and _deepseek_key:
    client = OpenAI(api_key=_deepseek_key, base_url="https://api.deepseek.com", timeout=_LLM_TIMEOUT)
    DEFAULT_MODEL = "deepseek-chat"
elif _backend == "openai" and _openai_key:
    client = OpenAI(api_key=_openai_key, timeout=_LLM_TIMEOUT)
    DEFAULT_MODEL = "gpt-4o-mini"
elif _deepseek_key and _backend != "ollama":
    client = OpenAI(api_key=_deepseek_key, base_url="https://api.deepseek.com", timeout=_LLM_TIMEOUT)
    DEFAULT_MODEL = "deepseek-chat"
elif _openai_key and _backend not in ("ollama", "deepseek"):
    client = OpenAI(api_key=_openai_key, timeout=_LLM_TIMEOUT)
    DEFAULT_MODEL = "gpt-4o-mini"
else:
    # 默认：Ollama 本地，免费，无需 API Key
    client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama", timeout=_LLM_TIMEOUT)
    # qwen2.5:7b 平衡效果与速度；轻量可改为 qwen2.5:3b，追求更好可改为 qwen2.5:14b（ollama pull qwen2.5:7b）
    DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b").strip() or "qwen2.5:7b"


def ask_llm(system, user, model=None, temperature=None, max_tokens=None):
    if model is None:
        model = DEFAULT_MODEL
    temp = temperature if temperature is not None else LLM_TEMPERATURE
    max_tok = max_tokens if max_tokens is not None else LLM_MAX_TOKENS
    kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temp,
    }
    if max_tok is not None and max_tok > 0:
        kwargs["max_tokens"] = max_tok
    try:
        resp = client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""
    except Exception as e:
        err_msg = str(e).strip()
        if "connection" in err_msg.lower() or "111" in err_msg or "localhost" in err_msg.lower():
            raise RuntimeError(
                "无法连接 Ollama。请先安装并启动 Ollama，并拉取模型：\n"
                "  https://ollama.com\n"
                "  ollama pull qwen2.5:7b"
            ) from e
        if "429" in err_msg or "quota" in err_msg.lower() or "insufficient_quota" in err_msg.lower():
            raise RuntimeError(
                "API 配额不足或已超限，请检查计费或改用本地 Ollama（免费）。"
            ) from e
        if "401" in err_msg or "invalid" in err_msg.lower():
            raise RuntimeError(
                "API Key 无效，请检查环境变量或改用本地 Ollama（免费）。"
            ) from e
        raise
