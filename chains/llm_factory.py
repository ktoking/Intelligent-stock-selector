"""
LangChain ChatModel 工厂：与 llm.py 共用 config.llm_resolve，避免两套分支不一致。
"""
from typing import Optional

from langchain_openai import ChatOpenAI
from langchain_core.language_models.chat_models import BaseChatModel

from config.llm_resolve import resolve_openai_compat_config
from config.llm_config import LLM_TEMPERATURE, LLM_MAX_TOKENS

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

    cfg = resolve_openai_compat_config()
    common = _common_kwargs()
    kw: dict = {
        "api_key": cfg.api_key,
        "model": cfg.default_model,
        "timeout": cfg.timeout,
        **common,
    }
    if cfg.base_url:
        kw["base_url"] = cfg.base_url
    _llm_instance = ChatOpenAI(**kw)
    return _llm_instance
