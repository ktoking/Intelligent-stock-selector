"""
LLM 调用可观测：可选 JSONL 追加（延迟、成功/失败、模型名）。
路径由环境变量 LLM_CALL_LOG_PATH 指定；未设置则不写盘。
"""
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _log_path() -> Optional[str]:
    p = (os.environ.get("LLM_CALL_LOG_PATH") or "").strip()
    return p or None


def log_llm_call(
    *,
    step: str,
    model: str,
    ok: bool,
    latency_ms: float,
    error_short: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    path = _log_path()
    if not path:
        return
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "step": step,
        "model": model,
        "ok": ok,
        "latency_ms": round(latency_ms, 2),
        "error": (error_short or "")[:500],
    }
    if extra:
        row["extra"] = extra
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def timed_llm_step(step: str, model: str):
    """上下文管理器：记录一步耗时。"""

    class _Ctx:
        def __init__(self):
            self._t0 = 0.0

        def __enter__(self):
            self._t0 = time.perf_counter()
            return self

        def __exit__(self, exc_type, exc, tb):
            ms = (time.perf_counter() - self._t0) * 1000
            err = ""
            ok = exc_type is None
            if not ok and exc:
                err = str(exc)[:200]
            log_llm_call(step=step, model=model, ok=ok, latency_ms=ms, error_short=err)
            return False

    return _Ctx()
