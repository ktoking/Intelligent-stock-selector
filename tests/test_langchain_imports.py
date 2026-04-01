"""LangChain / Pydantic 环境：深度分析 deep=1 依赖，避免 pydantic 1.x 导致 No module named pydantic.v1。"""
import importlib.util


def test_pydantic_v2_has_v1_compat():
    assert importlib.util.find_spec("pydantic.v1") is not None, "需 pydantic>=2（含 pydantic.v1 兼容层）"


def test_langchain_core_loads():
    from langchain_core._api import deprecation  # noqa: F401


def test_analysis_deep_langchain_flag():
    from agents.analysis_deep import _LANGCHAIN_AVAILABLE

    assert _LANGCHAIN_AVAILABLE is True
