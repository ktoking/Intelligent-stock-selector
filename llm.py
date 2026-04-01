"""
LLM 调用：默认使用本地 Ollama，可选 MiniMax、DeepSeek、OpenAI。
配置见 .env.example；后端解析统一走 config.llm_resolve，与 LangChain 工厂一致。
支持 tenacity 重试与可选 JSONL 观测日志（LLM_CALL_LOG_PATH）。
"""
import re
from typing import Any

from openai import OpenAI

from config.llm_resolve import resolve_openai_compat_config
from config.llm_config import (
    LLM_TEMPERATURE,
    LLM_MAX_TOKENS,
    LLM_RETRY_ATTEMPTS,
    LLM_RETRY_MIN_WAIT,
    LLM_RETRY_MAX_WAIT,
)

try:
    from openai import APIConnectionError, APITimeoutError, RateLimitError

    _OPENAI_RETRY_TYPES = (APIConnectionError, APITimeoutError, RateLimitError)
except ImportError:
    _OPENAI_RETRY_TYPES = tuple()

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

_cfg = resolve_openai_compat_config()
_client_kwargs: dict[str, Any] = {"api_key": _cfg.api_key, "timeout": _cfg.timeout}
if _cfg.base_url:
    _client_kwargs["base_url"] = _cfg.base_url
client = OpenAI(**_client_kwargs)
DEFAULT_MODEL = _cfg.default_model


def _retryable_llm_error(exc: BaseException) -> bool:
    if isinstance(exc, _OPENAI_RETRY_TYPES):
        return True
    msg = str(exc).lower()
    if "429" in msg or "rate limit" in msg or "too many requests" in msg:
        return True
    if "connection" in msg or "timeout" in msg or "timed out" in msg:
        return True
    if "remote" in msg and "closed" in msg:
        return True
    return False


@retry(
    stop=stop_after_attempt(LLM_RETRY_ATTEMPTS),
    wait=wait_exponential(multiplier=1, min=LLM_RETRY_MIN_WAIT, max=LLM_RETRY_MAX_WAIT),
    retry=retry_if_exception(_retryable_llm_error),
    reraise=True,
)
def _chat_completions_create(**kwargs):
    return client.chat.completions.create(**kwargs)


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

    from utils.llm_observability import log_llm_call
    import time as _time

    t0 = _time.perf_counter()
    try:
        resp = _chat_completions_create(**kwargs)
        text = resp.choices[0].message.content or ""
        if text and "<redacted_thinking>" in text and "</redacted_thinking>" in text:
            text = re.sub(r"<redacted_thinking>[\s\S]*?</redacted_thinking>", "", text, flags=re.DOTALL).strip()
        ms = (_time.perf_counter() - t0) * 1000
        log_llm_call(step="ask_llm", model=model, ok=True, latency_ms=ms)
        return text
    except Exception as e:
        ms = (_time.perf_counter() - t0) * 1000
        log_llm_call(step="ask_llm", model=model, ok=False, latency_ms=ms, error_short=str(e))
        _raise_friendly(e)


def _raise_friendly(e: BaseException) -> None:
    err_msg = str(e).strip()
    if "connection" in err_msg.lower() or "111" in err_msg or "localhost" in err_msg.lower():
        raise RuntimeError(
            "无法连接 Ollama。请先安装并启动 Ollama，并拉取模型：\n"
            "  https://ollama.com\n"
            "  ollama pull qwen3.5:9b"
        ) from e
    # 避免把含 "quota" 子串的无关错误误判为计费（如 JSON 字段名）；仅匹配典型计费/限流
    em = err_msg.lower()
    if (
        "insufficient_quota" in em
        or "billing_hard_limit" in em
        or "exceeded your current quota" in em
        or ("429" in err_msg and ("rate" in em or "limit" in em or "quota" in em))
    ):
        raise RuntimeError(
            "API 配额不足或已超限，请检查计费；若本意用本机 Ollama，请在 .env 设置 LLM_BACKEND=ollama（"
            "若同时配置了 MINIMAX_API_KEY，否则会走 MiniMax 开放平台而非 127.0.0.1:11434）。"
        ) from e
    if "401" in err_msg or "invalid" in err_msg.lower():
        raise RuntimeError(
            "API Key 无效，请检查环境变量或改用本地 Ollama（免费）。"
        ) from e
    raise e
