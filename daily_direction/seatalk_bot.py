from __future__ import annotations

import os
import html
import re
from typing import Any, Callable, Optional


def clean_brief_text(text: str) -> str:
    """Plain Unicode content avoids SeaTalk Markdown/HTML escaping artifacts."""
    text = html.unescape(str(text)).replace("\u200b", "").replace("\xa0", " ")
    text = re.sub(r"\[([^\]]+)\]\(\s*(https?://[^)]+)\)", r"\1\n\2", text)
    text = text.replace("**", "").replace("\\(", "(").replace("\\)", ")")
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def build_brief_card(text: str) -> dict:
    text = clean_brief_text(text)
    title, _, body = text.partition("\n")
    if len(body.strip()) > 1000:
        raise ValueError("Brief card body exceeds message limit")
    return {"tag": "interactive_message", "interactive_message": {"elements": [
        {"element_type": "title", "title": {"text": title[:80]}},
        {"element_type": "description", "description": {"format": 1, "text": body.strip()}},
    ]}}


def build_brief_cards(text: str) -> list[dict]:
    title, _, body = clean_brief_text(text).partition("\n")
    if not title:
        raise ValueError("Brief content is empty")
    if not body.strip():
        body = title
    paragraphs = []
    for paragraph in body.strip().split("\n\n"):
        remaining = paragraph.strip()
        while remaining:
            cut = len(remaining) if len(remaining) <= 990 else remaining.rfind("\n", 0, 990)
            if cut <= 0: cut = 990
            paragraphs.append(remaining[:cut])
            remaining = remaining[cut:].strip()
    chunks, current = [], ""
    for paragraph in paragraphs:
        combined = (current + "\n\n" + paragraph).strip()
        if len(combined) > 990 and current:
            chunks.append(current)
            current = paragraph
        else:
            current = combined
    if current: chunks.append(current)
    cards = []
    for index, chunk in enumerate(chunks):
        heading = title if index == 0 else "📅 今天方向简报 · 续页" if any(flag in chunk for flag in ("🇺🇸", "🇨🇳", "🇭🇰")) else "📢 今天方向简报 · 事件与关注" if "📢" in chunk else title
        if len(chunks) > 1: heading += f" ({index + 1}/{len(chunks)})"
        cards.append(build_brief_card(heading + "\n" + chunk))
    return cards


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
) -> dict[str, Any]:
    if not (text or "").strip():
        raise ValueError("text is required")

    receipt = {}
    if settings is None or send_group_func is None:
        from scripts.seatalk_hermes_adapter import _post_openapi, get_settings

        settings = settings or get_settings()
        if send_group_func is None:
            def send_card(target_group, target_thread, content, cfg):
                ids = []
                for message in build_brief_cards(content):
                    if target_thread:
                        message["thread_id"] = target_thread
                    response = _post_openapi(cfg.group_chat_path, {"group_id": target_group, "message": message}, cfg)
                    if response.get("code") != 0:
                        raise RuntimeError("SeaTalk did not explicitly confirm brief delivery")
                    ids.append(response.get("message_id") or (response.get("data") or {}).get("message_id"))
                receipt.update(api_code=0, message_id=ids[0] if ids else None, message_ids=ids, presentation="native_cards", card_count=len(ids))
            send_group_func = send_card

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
        **receipt,
    }
