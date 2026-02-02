"""
LLM 与 Agent 可调参数：模型参数、输出约束、Prompt 相关。
通过环境变量覆盖，便于调优与 A/B 测试。
"""
import os

# ---------- 模型参数 ----------
# 采样温度：0~2，越低越确定、越适合结构化分析；略高可增加表述多样性
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.3"))

# 单次回复最大 token 数：不设则用模型默认；设小可避免冗长、加快响应
LLM_MAX_TOKENS = os.environ.get("LLM_MAX_TOKENS", "")
if LLM_MAX_TOKENS != "":
    try:
        LLM_MAX_TOKENS = int(LLM_MAX_TOKENS)
    except ValueError:
        LLM_MAX_TOKENS = None
else:
    LLM_MAX_TOKENS = None

# 模型名：Ollama 下由 OLLAMA_MODEL 控制；DeepSeek/OpenAI 在 llm.py 中按 backend 选
# 此处仅作文档说明，实际 DEFAULT_MODEL 在 llm.py 内根据环境变量赋值

# ---------- Report 综合输出约束（可选） ----------
# 要求 LLM 输出中必须包含的「交易动作」关键词之一，用于自检或过滤；空表示不校验
REPORT_ACTION_KEYWORDS = os.environ.get("REPORT_ACTION_KEYWORDS", "").strip()
if REPORT_ACTION_KEYWORDS:
    REPORT_ACTION_KEYWORDS = [k.strip() for k in REPORT_ACTION_KEYWORDS.split(",") if k.strip()]
else:
    REPORT_ACTION_KEYWORDS = []

# ---------- Prompt 风格（预留） ----------
# conservative | neutral | aggressive：可后续在 full_analysis / fundamental 中根据该值微调 system 描述
PROMPT_TONE = (os.environ.get("PROMPT_TONE", "conservative") or "conservative").strip().lower()
