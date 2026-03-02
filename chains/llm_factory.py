"""
LangChain ChatModel 工厂：根据环境变量构建 Ollama / DeepSeek / OpenAI，与 llm.py 逻辑一致。
温度与 max_tokens 从 config.llm_config 读取，便于与 llm.ask_llm 统一调优。
"""
import os
from typing import Optional

from langchain_openai import ChatOpenAI
from langchain_core.language_models.chat_models import BaseChatModel

from config.llm_config import LLM_TEMPERATURE, LLM_MAX_TOKENS

_backend = os.environ.get("LLM_BACKEND", "").strip().lower()
_deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
_openai_key = os.environ.get("OPENAI_API_KEY", "").strip()

_default_model = "gpt-4o-mini"
_llm_instance: Optional[BaseChatModel] = None


def _common_kwargs():
    kwargs = {"temperature": LLM_TEMPERATURE}
    if LLM_MAX_TOKENS is not None and LLM_MAX_TOKENS > 0:
        kwargs["max_tokens"] = LLM_MAX_TOKENS
    return kwargs


def get_llm() -> BaseChatModel:
    """单例：返回当前配置的 LangChain ChatModel（Ollama/DeepSeek/OpenAI）。"""
    global _llm_instance
    if _llm_instance is not None:
        return _llm_instance

    common = _common_kwargs()
    if _backend == "deepseek" and _deepseek_key:
        _llm_instance = ChatOpenAI(
            api_key=_deepseek_key,
            base_url="https://api.deepseek.com",
            model="deepseek-chat",
            **common,
        )
    elif _backend == "openai" and _openai_key:
        _llm_instance = ChatOpenAI(
            api_key=_openai_key,
            model="gpt-4o-mini",
            **common,
        )
    elif _deepseek_key and _backend != "ollama":
        _llm_instance = ChatOpenAI(
            api_key=_deepseek_key,
            base_url="https://api.deepseek.com",
            model="deepseek-chat",
            **common,
        )
    elif _openai_key and _backend not in ("ollama", "deepseek"):
        _llm_instance = ChatOpenAI(
            api_key=_openai_key,
            model="gpt-4o-mini",
            **common,
        )
    else:
        model = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b").strip() or "qwen2.5:7b"
        _llm_instance = ChatOpenAI(
            base_url="http://localhost:11434/v1",
            api_key="ollama",
            model=model,
            **common,
        )
    return _llm_instance
