#!/usr/bin/env python3
"""One SeaTalk recap per US trading day, after 16:10 New York time."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.okx_intraday_agent import NY, ROOT, OKX, dashboard_snapshot, notify, settings
from scripts.okx_trade_replay import replay_day
from llm import ask_llm

STATE_PATH = ROOT / "data" / "okx_close_recap_state.json"
LOG = logging.getLogger("okx-close-recap")


def sent_day() -> str:
    try:
        return json.loads(STATE_PATH.read_text()).get("trade_day", "")
    except (OSError, ValueError, json.JSONDecodeError):
        return ""


def due(now: datetime) -> bool:
    return now.weekday() < 5 and (now.hour, now.minute) >= (16, 10) and sent_day() != now.date().isoformat()


def build_message(data: dict, now: datetime, quant_summary: dict | None = None) -> str:
    funds, today, positions = data["funds"], data["today"], data["positions"]
    quant_summary = quant_summary or {}
    facts = {
        "date": now.date().isoformat(), "fills": today["fills"], "realized_pnl": today["realized_pnl"],
        "open_upl": today["open_upl"], "equity": funds["total_equity"], "available": funds["usdt_available"],
        "positions": positions, "events": data.get("events", {}).get("events", [])[:5],
        "headlines": data.get("events", {}).get("news", [])[:5],
        "multi_timeframe_trade_replay": quant_summary,
    }
    system = "你是量化交易复盘助手。仅根据给定事实写中文复盘；不得承诺收益，不得编造数据或给出新下单指令。"
    prompt = (
        "请用不超过220字总结：当日交易、1m/5m/10m回放反映的入场和退出质量、风险、"
        "持仓事件风险、明日观察。事实：" + json.dumps(facts, ensure_ascii=False)
    )
    try:
        analysis = ask_llm(system, prompt, temperature=0.2, max_tokens=300).strip()
    except Exception as exc:
        LOG.warning("LLM recap unavailable: %s", exc)
        analysis = "LLM 复盘暂不可用；请以账户数据与风险限制为准。"
    replay_line = (
        f"回放 {quant_summary.get('analyzed', 0)}/{quant_summary.get('trades', 0)} 笔｜"
        f"前三分钟假突破 {float(quant_summary.get('failed_breakout_rate') or 0) * 100:.1f}%｜"
        f"曾达到 1R {float(quant_summary.get('reached_1r_rate') or 0) * 100:.1f}%｜"
        f"平均 MFE/MAE {float(quant_summary.get('avg_mfe_bps') or 0):.1f}/"
        f"{float(quant_summary.get('avg_mae_bps') or 0):.1f} bps"
    )
    return (
        f"📘 **OKX 美股代币收盘复盘（模拟盘）**\n\n"
        f"**账户**\n权益 ${funds['total_equity']:.2f}｜可用 ${funds['usdt_available']:.2f}\n"
        f"**当日**\n成交 {today['fills']} 笔｜已实现 ${today['realized_pnl']:.4f}｜持仓浮盈亏 ${today['open_upl']:.4f}\n"
        f"**多周期交易回放**\n{replay_line}\n"
        f"**持仓**\n" + ("\n".join(f"- {p['inst_id']} {p['side']}｜浮盈亏 {p['upl']:.4f}" for p in positions) if positions else "- 无") +
        f"\n\n**LLM 复盘**\n{analysis}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    now = datetime.now(NY)
    if not args.force and not due(now):
        return
    cfg = settings()
    data = dashboard_snapshot()
    if not data.get("ok"):
        raise RuntimeError(data.get("error", "dashboard snapshot failed"))
    quant_summary: dict = {}
    try:
        replay = replay_day(OKX(cfg), now.date())
        replay_path = ROOT / "data" / f"okx_trade_replay_{now.date().isoformat()}.json"
        replay_path.write_text(json.dumps(replay, ensure_ascii=False, indent=2))
        quant_summary = replay["summary"]
    except Exception as exc:
        LOG.exception("multi-timeframe trade replay unavailable: %s", exc)
    notify(cfg, build_message(data, now, quant_summary))
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({"trade_day": now.date().isoformat(), "sent_at": now.isoformat()}))


if __name__ == "__main__":
    logging.basicConfig(level="INFO")
    main()
