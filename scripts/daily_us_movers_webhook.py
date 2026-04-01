#!/usr/bin/env python3
"""
美股日终异动扫描，结果推送到 Webhook（飞书 / 钉钉 / Slack / 通用 JSON）。

规则（默认 4 条，可用参数调整）：
  1) 当日涨幅 >= 阈值（默认 3%）
  2) 当日成交量 >= 前 20 日均量 × 倍数（默认 1.5）
  3) 前 20 日平均成交额（美元）>= 阈值（默认 2000 万）
  4) 收盘价 >= 前 20 根 K 线的最高价（突破近 20 日高）
  可选 5) --above-sma50：收盘高于前 50 日收盘均线

环境变量（推荐写入 .env）：
  SCAN_WEBHOOK_URL       机器人 Webhook 地址（必填，除非 --dry-run）
  SCAN_WEBHOOK_STYLE     feishu | dingtalk | slack | generic（默认 generic）

用法：
  cd /path/to/stock-agent && python scripts/daily_us_movers_webhook.py --dry-run
  python scripts/daily_us_movers_webhook.py --pool nasdaq100 --limit 120
  python scripts/daily_us_movers_webhook.py --webhook-url 'https://...' --style feishu

定时（crontab，美股收盘后数据更完整；北京时间次日清晨示例）：
  30 6 * * 2-6 cd /path/to/stock-agent && .venv/bin/python scripts/daily_us_movers_webhook.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except ImportError:
    print("请安装: pip install requests", file=sys.stderr)
    sys.exit(1)

# 项目根目录
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(_ROOT, ".env"))
    load_dotenv(os.path.join(_ROOT, ".env.local"))
except ImportError:
    pass

from config.tickers import get_report_tickers, MARKET_US, POOL_NASDAQ100
from data.us_movers_scan import scan_us_equity_movers


def _build_message(
    rows: List[Dict[str, Any]],
    *,
    pool: str,
    limit: int,
    rules_desc: str,
) -> str:
    et = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if not rows:
        return (
            f"【美股日终异动】{et}\n"
            f"池子: {pool} (最多 {limit} 只)\n"
            f"规则: {rules_desc}\n"
            f"结果: 无标的满足条件"
        )
    lines = [
        f"【美股日终异动】{et}",
        f"池子: {pool} (扫描上限 {limit} 只)",
        f"规则: {rules_desc}",
        f"命中: {len(rows)} 只（按涨幅排序）",
        "",
    ]
    for r in rows:
        lines.append(
            f"- {r['ticker']}: {r['daily_pct']:+.2f}% | "
            f"量比 {r['vol_ratio']:.2f} | "
            f"20日均额 ${r['avg_dollar_vol_20d']:.0f} | "
            f"收盘 {r['close']}"
        )
    return "\n".join(lines)


def _webhook_body(text: str, style: str) -> Tuple[Any, str]:
    s = (style or "generic").strip().lower()
    if s == "feishu":
        return {"msg_type": "text", "content": {"text": text}}, "application/json; charset=utf-8"
    if s == "dingtalk":
        return {"msgtype": "text", "text": {"content": text}}, "application/json; charset=utf-8"
    if s == "slack":
        return {"text": text}, "application/json; charset=utf-8"
    return {"text": text}, "application/json; charset=utf-8"


def post_webhook(url: str, text: str, style: str, timeout: int = 20) -> None:
    body, _ = _webhook_body(text, style)
    r = requests.post(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=timeout,
    )
    r.raise_for_status()


def main() -> None:
    p = argparse.ArgumentParser(description="美股日终异动扫描 + Webhook")
    p.add_argument("--pool", default=POOL_NASDAQ100, help=f"选股池，默认 {POOL_NASDAQ100}")
    p.add_argument("--limit", type=int, default=120, help="从池中取前 N 只扫描")
    p.add_argument("--min-daily-pct", type=float, default=3.0, help="最低当日涨幅 %%")
    p.add_argument("--min-vol-ratio", type=float, default=1.5, help="量比：当日量 / 前20日均量")
    p.add_argument("--min-avg-dollar-vol", type=float, default=20_000_000, help="前20日日均成交额 USD")
    p.add_argument("--no-breakout", action="store_true", help="不要求创 20 日高")
    p.add_argument("--above-sma50", action="store_true", help="要求收盘高于 50 日均线")
    p.add_argument("--max-alerts", type=int, default=30, help="推送中最多展示条数")
    p.add_argument("--dry-run", action="store_true", help="只打印，不请求 Webhook")
    p.add_argument("--webhook-url", default="", help="覆盖环境变量 SCAN_WEBHOOK_URL")
    p.add_argument(
        "--style",
        default="",
        choices=["", "generic", "feishu", "dingtalk", "slack"],
        help="覆盖 SCAN_WEBHOOK_STYLE",
    )
    args = p.parse_args()

    url = (args.webhook_url or os.environ.get("SCAN_WEBHOOK_URL") or "").strip()
    style = (args.style or os.environ.get("SCAN_WEBHOOK_STYLE") or "generic").strip().lower()
    if style not in ("generic", "feishu", "dingtalk", "slack"):
        style = "generic"

    tickers = get_report_tickers(limit=max(1, min(args.limit, 500)), market=MARKET_US, pool=args.pool)
    rows = scan_us_equity_movers(
        tickers,
        min_daily_pct=args.min_daily_pct,
        min_volume_ratio=args.min_vol_ratio,
        min_avg_dollar_volume_20d=args.min_avg_dollar_vol,
        require_breakout_20d=not args.no_breakout,
        require_above_sma50=args.above_sma50,
    )
    rows = rows[: max(1, args.max_alerts)]

    rules = (
        f"涨幅≥{args.min_daily_pct}% | 量比≥{args.min_vol_ratio} | "
        f"20日均额≥${args.min_avg_dollar_vol/1e6:.0f}M"
    )
    if not args.no_breakout:
        rules += " | 创20日高"
    if args.above_sma50:
        rules += " | 站上50日均线"

    text = _build_message(rows, pool=args.pool, limit=args.limit, rules_desc=rules)

    print(text, flush=True)

    if args.dry_run:
        return
    if not url:
        print("未设置 SCAN_WEBHOOK_URL 且未传 --webhook-url，跳过推送。", file=sys.stderr)
        sys.exit(2)
    try:
        post_webhook(url, text, style)
        print("Webhook 已发送", flush=True)
    except Exception as e:
        print(f"Webhook 失败: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
