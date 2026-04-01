"""config.llm_resolve：OpenAI 兼容配置结构单测。"""
from config.llm_resolve import resolve_openai_compat_config, OpenAICompatConfig


def test_resolve_returns_dataclass():
    c = resolve_openai_compat_config()
    assert isinstance(c, OpenAICompatConfig)
    assert c.default_model
    assert c.timeout > 0
    assert c.api_key
