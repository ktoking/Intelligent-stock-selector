from __future__ import annotations

import os
from typing import Any, Callable, Optional


def _first_group_id(value: str) -> str:
    for item in (value or "").replace(";", ",").split(","):
        group_id = item.strip().strip('"').strip("'")
        if group_id:
            return group_id
    return ""


def _default_group_id(settings: Any) -> str:
    explicit = _first_group_id(os.environ.get("DAILY_DIRECTION_SEATALK_GROUP_ID", ""))
    if explicit:
        return explicit

    allowed_group_ids = getattr(settings, "allowed_group_ids", set()) or set()
    if not allowed_group_ids:
        return ""
    return sorted(str(group_id) for group_id in allowed_group_ids if str(group_id).strip())[0]


def send_group_text_via_bot(
    text: str,
    *,
    group_id: str = "",
    thread_id: str = "",
    settings: Optional[Any] = None,
    send_group_func: Optional[Callable[[str, str, str, Any], None]] = None,
) -> dict[str, str]:
    if not (text or "").strip():
        raise ValueError("text is required")

    if settings is None or send_group_func is None:
        from scripts.seatalk_hermes_adapter import _send_group, get_settings

        settings = settings or get_settings()
        send_group_func = send_group_func or _send_group

    target_group_id = (group_id or "").strip() or _default_group_id(settings)
    if not target_group_id:
        raise RuntimeError(
            "未配置 SeaTalk bot 群 ID，请设置 --group-id、DAILY_DIRECTION_SEATALK_GROUP_ID "
            "或 SEATALK_ALLOWED_GROUP_IDS"
        )

    send_group_func(target_group_id, thread_id.strip(), text, settings)
    return {
        "delivery": "seatalk-bot",
        "group_id": target_group_id,
        "thread_id": thread_id.strip(),
    }
