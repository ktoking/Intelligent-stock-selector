"""
环境变量加载：从 .env、.env.local 读取，便于切换 LLM 等配置。
优先级：Shell 环境 > .env.local > .env
"""
import os
from pathlib import Path

def load_env():
    """加载 .env 和 .env.local。Shell 已存在的变量不覆盖；.env.local 覆盖 .env。"""
    try:
        from dotenv import dotenv_values
    except ImportError:
        return
    root = Path(__file__).resolve().parent.parent
    merged = {}
    for path in [root / ".env", root / ".env.local"]:
        if path.exists():
            merged.update(dotenv_values(path) or {})
    for k, v in merged.items():
        if k and os.environ.get(k) is None:
            os.environ[k] = str(v) if v else ""
