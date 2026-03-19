"""
LLM 调用：默认使用本地 Ollama，可选 MiniMax、DeepSeek、OpenAI。
配置见 .env.example，复制为 .env 后取消注释对应预设即可切换。
"""
import re
from openai import OpenAI

from config.llm_config import LLM_TEMPERATURE, LLM_MAX_TOKENS
from config.llm_backend import (
    LLM_BACKEND,
    MINIMAX_API_KEY,
    DEEPSEEK_API_KEY,
    OPENAI_API_KEY,
    OLLAMA_MODEL,
    MINIMAX_API_BASE,
    MINIMAX_MODEL,
    DEEPSEEK_BASE,
    DEEPSEEK_MODEL,
    OPENAI_MODEL,
    LLM_TIMEOUT,
)

if LLM_BACKEND == "minimax" and MINIMAX_API_KEY:
    client = OpenAI(api_key=MINIMAX_API_KEY, base_url=MINIMAX_API_BASE, timeout=LLM_TIMEOUT)
    DEFAULT_MODEL = MINIMAX_MODEL
elif MINIMAX_API_KEY and LLM_BACKEND != "ollama":
    client = OpenAI(api_key=MINIMAX_API_KEY, base_url=MINIMAX_API_BASE, timeout=LLM_TIMEOUT)
    DEFAULT_MODEL = MINIMAX_MODEL
elif LLM_BACKEND == "deepseek" and DEEPSEEK_API_KEY:
    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE, timeout=LLM_TIMEOUT)
    DEFAULT_MODEL = DEEPSEEK_MODEL
elif LLM_BACKEND == "openai" and OPENAI_API_KEY:
    client = OpenAI(api_key=OPENAI_API_KEY, timeout=LLM_TIMEOUT)
    DEFAULT_MODEL = OPENAI_MODEL
elif DEEPSEEK_API_KEY and LLM_BACKEND not in ("ollama", "minimax"):
    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE, timeout=LLM_TIMEOUT)
    DEFAULT_MODEL = DEEPSEEK_MODEL
elif OPENAI_API_KEY and LLM_BACKEND not in ("ollama", "deepseek", "minimax"):
    client = OpenAI(api_key=OPENAI_API_KEY, timeout=LLM_TIMEOUT)
    DEFAULT_MODEL = OPENAI_MODEL
else:
    client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama", timeout=LLM_TIMEOUT)
    DEFAULT_MODEL = OLLAMA_MODEL


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
        text = resp.choices[0].message.content or ""
        # MiniMax M2.1 等模型会返回 <think>...</think> 推理块，需剥离后返回
        if text and "<think>" in text and "</think>" in text:
            text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.DOTALL).strip()
        return text
    except Exception as e:
        err_msg = str(e).strip()
        if "connection" in err_msg.lower() or "111" in err_msg or "localhost" in err_msg.lower():
            raise RuntimeError(
                "无法连接 Ollama。请先安装并启动 Ollama，并拉取模型：\n"
                "  https://ollama.com\n"
                "  ollama pull qwen3.5:9b"
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
