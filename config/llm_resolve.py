"""
统一解析 OpenAI 兼容客户端参数（Ollama / MiniMax / DeepSeek / OpenAI）。
供 llm.py 与 chains/llm_factory 共用，避免两套分支不一致。
"""
from dataclasses import dataclass
from typing import Optional

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


@dataclass(frozen=True)
class OpenAICompatConfig:
    """OpenAI SDK / LangChain ChatOpenAI 共用配置。"""

    api_key: str
    base_url: Optional[str]  # None 表示官方 api.openai.com
    default_model: str
    timeout: float


def resolve_openai_compat_config() -> OpenAICompatConfig:
    """与 llm.py 原分支顺序保持一致。

    注意：仅当显式设置 LLM_BACKEND=minimax 时才走 MiniMax 开放平台 API。
    若同时配置了 MINIMAX_API_KEY 与本地 Ollama，未写 LLM_BACKEND 时以前会误走 MiniMax，
    导致「配额」类报错；本地 Ollama / Ollama 云端模型请设 LLM_BACKEND=ollama。
    """
    if LLM_BACKEND == "minimax" and MINIMAX_API_KEY:
        return OpenAICompatConfig(
            api_key=MINIMAX_API_KEY,
            base_url=MINIMAX_API_BASE,
            default_model=MINIMAX_MODEL,
            timeout=LLM_TIMEOUT,
        )
    if LLM_BACKEND == "deepseek" and DEEPSEEK_API_KEY:
        return OpenAICompatConfig(
            api_key=DEEPSEEK_API_KEY,
            base_url=DEEPSEEK_BASE,
            default_model=DEEPSEEK_MODEL,
            timeout=LLM_TIMEOUT,
        )
    if LLM_BACKEND == "openai" and OPENAI_API_KEY:
        return OpenAICompatConfig(
            api_key=OPENAI_API_KEY,
            base_url=None,
            default_model=OPENAI_MODEL,
            timeout=LLM_TIMEOUT,
        )
    if DEEPSEEK_API_KEY and LLM_BACKEND not in ("ollama", "minimax"):
        return OpenAICompatConfig(
            api_key=DEEPSEEK_API_KEY,
            base_url=DEEPSEEK_BASE,
            default_model=DEEPSEEK_MODEL,
            timeout=LLM_TIMEOUT,
        )
    if OPENAI_API_KEY and LLM_BACKEND not in ("ollama", "deepseek", "minimax"):
        return OpenAICompatConfig(
            api_key=OPENAI_API_KEY,
            base_url=None,
            default_model=OPENAI_MODEL,
            timeout=LLM_TIMEOUT,
        )
    return OpenAICompatConfig(
        api_key="ollama",
        base_url="http://localhost:11434/v1",
        default_model=OLLAMA_MODEL,
        timeout=LLM_TIMEOUT,
    )
