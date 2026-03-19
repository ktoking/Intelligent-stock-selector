"""
LangChain ChatModel 工厂：根据 config.llm_backend 构建 Ollama/MiniMax/DeepSeek/OpenAI。
"""
from typing import Optional

from langchain_openai import ChatOpenAI
from langchain_core.language_models.chat_models import BaseChatModel

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
)

_llm_instance: Optional[BaseChatModel] = None


def _common_kwargs():
    kwargs = {"temperature": LLM_TEMPERATURE}
    if LLM_MAX_TOKENS is not None and LLM_MAX_TOKENS > 0:
        kwargs["max_tokens"] = LLM_MAX_TOKENS
    return kwargs


def get_llm() -> BaseChatModel:
    """单例：返回当前配置的 LangChain ChatModel。"""
    global _llm_instance
    if _llm_instance is not None:
        return _llm_instance

    common = _common_kwargs()
    if LLM_BACKEND == "minimax" and MINIMAX_API_KEY:
        _llm_instance = ChatOpenAI(api_key=MINIMAX_API_KEY, base_url=MINIMAX_API_BASE, model=MINIMAX_MODEL, **common)
    elif MINIMAX_API_KEY and LLM_BACKEND != "ollama":
        _llm_instance = ChatOpenAI(api_key=MINIMAX_API_KEY, base_url=MINIMAX_API_BASE, model=MINIMAX_MODEL, **common)
    elif LLM_BACKEND == "deepseek" and DEEPSEEK_API_KEY:
        _llm_instance = ChatOpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE, model=DEEPSEEK_MODEL, **common)
    elif LLM_BACKEND == "openai" and OPENAI_API_KEY:
        _llm_instance = ChatOpenAI(api_key=OPENAI_API_KEY, model=OPENAI_MODEL, **common)
    elif DEEPSEEK_API_KEY and LLM_BACKEND not in ("ollama", "minimax"):
        _llm_instance = ChatOpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE, model=DEEPSEEK_MODEL, **common)
    elif OPENAI_API_KEY and LLM_BACKEND not in ("ollama", "deepseek", "minimax"):
        _llm_instance = ChatOpenAI(api_key=OPENAI_API_KEY, model=OPENAI_MODEL, **common)
    else:
        _llm_instance = ChatOpenAI(base_url="http://localhost:11434/v1", api_key="ollama", model=OLLAMA_MODEL, **common)
    return _llm_instance
