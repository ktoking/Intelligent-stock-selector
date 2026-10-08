#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, List, Mapping

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.env_loader import load_env
from daily_direction.direction import (
    DEFAULT_MARKET_JOBS,
    MarketJob,
    generate_direction_report,
    scan_markets,
)
from daily_direction.seatalk_webhook import post_text


DELIVERY_CHOICES = ("webhook", "seatalk-bot")
DELIVERY_STATE_PATH = Path.home() / ".local" / "state" / "stock-agent" / "daily-direction-delivery.json"


def _delivered_today(path: Path, *, delivery: str, group_id: str) -> bool:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        state.get("date") == datetime.now().astimezone().date().isoformat()
        and state.get("delivery") == delivery
        and state.get("group_id") == group_id
    )


def _record_delivery(path: Path, *, delivery: str, group_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "date": datetime.now().astimezone().date().isoformat(),
        "delivery": delivery,
        "group_id": group_id,
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _missing_market_data(snapshots: Mapping[str, Mapping[str, Any]], jobs: List[MarketJob]) -> List[str]:
    return [job.label for job in jobs if not (snapshots.get(job.key) or {}).get("top_gainers")]


def _selected_jobs(markets: str, *, test: bool) -> List[MarketJob]:
    wanted = {m.strip().lower() for m in (markets or "us,cn,hk").split(",") if m.strip()}
    jobs = [job for job in DEFAULT_MARKET_JOBS if job.key in wanted]
    if test:
        jobs = [MarketJob(job.key, job.label, job.market, job.pool, min(job.limit, 12)) for job in jobs]
    return jobs


def main() -> int:
    load_env()
    parser = argparse.ArgumentParser(description="生成并可选推送美股+A股+港股每日方向简报")
    parser.add_argument("--markets", default="us,cn,hk", help="逗号分隔: us,cn,hk")
    parser.add_argument("--max-items", type=int, default=15, help="每个市场最多交给 LLM 的标的数")
    parser.add_argument("--test", action="store_true", help="测试模式：每个市场只扫少量标的")
    parser.add_argument("--no-llm", action="store_true", help="不调用 LLM，直接输出规则版")
    parser.add_argument("--prefer-gpt", action=argparse.BooleanOptionalAction, default=True, help="有 OPENAI_API_KEY 时优先用 GPT")
    parser.add_argument("--send", action="store_true", help="实际推送到 SeaTalk；不传则只打印")
    parser.add_argument("--force-send", action="store_true", help="忽略今天已发送记录，手动补发")
    parser.add_argument(
        "--delivery",
        default=os.environ.get("DAILY_DIRECTION_DELIVERY", "webhook"),
        choices=DELIVERY_CHOICES,
        help="推送通道：webhook 或 seatalk-bot",
    )
    parser.add_argument("--webhook-url", default=os.environ.get("DAILY_DIRECTION_WEBHOOK_URL", ""), help="SeaTalk 群 webhook URL")
    parser.add_argument("--signing-secret", default=os.environ.get("DAILY_DIRECTION_SIGNING_SECRET", ""), help="SeaTalk webhook signing secret")
    parser.add_argument("--sign-mode", default=os.environ.get("DAILY_DIRECTION_SIGN_MODE", "auto"), choices=["auto", "query", "none"])
    parser.add_argument("--group-id", default=os.environ.get("DAILY_DIRECTION_SEATALK_GROUP_ID", ""), help="SeaTalk bot 群 ID")
    parser.add_argument("--thread-id", default=os.environ.get("DAILY_DIRECTION_SEATALK_THREAD_ID", ""), help="SeaTalk thread ID，可选")
    parser.add_argument("--at-all", action="store_true", help="推送时 @all")
    args = parser.parse_args()

    if args.prefer_gpt and os.environ.get("OPENAI_API_KEY", "").strip():
        os.environ["LLM_BACKEND"] = "openai"

    jobs = _selected_jobs(args.markets, test=args.test)
    if not jobs:
        print("没有可执行市场，请使用 --markets us,cn,hk 中的至少一个", file=sys.stderr)
        return 2

    if args.send and args.delivery == "seatalk-bot" and not args.force_send:
        if _delivered_today(DELIVERY_STATE_PATH, delivery=args.delivery, group_id=args.group_id.strip()):
            print("今天已推送到 SeaTalk bot，跳过重复发送", flush=True)
            return 0

    snapshots = scan_markets(jobs, max_items=max(1, args.max_items))
    missing = _missing_market_data(snapshots, jobs)
    if missing:
        print(f"行情扫描无有效数据：{'、'.join(missing)}；本次不生成或推送简报", file=sys.stderr)
        return 1
    text = generate_direction_report(snapshots, jobs, use_llm=not args.no_llm)
    print(text, flush=True)

    if not args.send:
        return 0
    if args.delivery == "seatalk-bot":
        from daily_direction.seatalk_bot import send_group_text_via_bot

        result = send_group_text_via_bot(text, group_id=args.group_id, thread_id=args.thread_id)
        _record_delivery(DELIVERY_STATE_PATH, delivery=args.delivery, group_id=result["group_id"])
        print("SeaTalk bot 推送成功", flush=True)
        return 0

    if not args.webhook_url.strip():
        print("未提供 --webhook-url 或 DAILY_DIRECTION_WEBHOOK_URL", file=sys.stderr)
        return 2
    post_text(
        args.webhook_url,
        text,
        signing_secret=args.signing_secret,
        sign_mode=args.sign_mode,
        at_all=args.at_all,
    )
    print("SeaTalk 推送已请求发送", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
