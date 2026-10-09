"""Render facts from validated inputs; the LLM may only select evidence IDs."""
from __future__ import annotations

import json
from typing import Any, Mapping

from config.tickers import TICKER_ZH_NAMES

# Verified identities. Unknown tickers stay unclassified instead of being guessed.
IDENTITIES = {
    "MU": ("美光科技", "存储芯片"), "AMGN": ("安进", "生物医药"),
    "NVDA": ("英伟达", "计算芯片"), "AMD": ("AMD", "计算芯片"),
    "MSTR": ("Strategy", "比特币相关"), "NXPI": ("恩智浦", "汽车/工业芯片"),
    "MCHP": ("微芯科技", "微控制器"), "TXN": ("德州仪器", "模拟芯片"),
    "AMAT": ("应用材料", "半导体设备"), "LRCX": ("泛林集团", "半导体设备"),
    "KLAC": ("科磊", "半导体设备"), "1299.HK": ("友邦保险", "保险"),
    "1088.HK": ("中国神华", "煤炭"), "0688.HK": ("中国海外发展", "地产"),
    "600900.SS": ("长江电力", "水电"), "600000.SS": ("浦发银行", "银行"),
    "601818.SS": ("光大银行", "银行"), "601398.SS": ("工商银行", "银行"),
    "000002.SZ": ("万科A", "地产"),
}


def identity(ticker: str) -> tuple[str, str]:
    return IDENTITIES.get(ticker, (TICKER_ZH_NAMES.get(ticker, ""), ""))


def candidates(snapshots: Mapping[str, Mapping[str, Any]]) -> dict:
    result = {"focus": {}, "risk": {}}
    for key, snap in snapshots.items():
        result["focus"][key] = [row["ticker"] for row in snap.get("top_signals", []) if row.get("quant_baseline_score", 0) >= 58 and row.get("daily_pct", 0) > 0]
        result["risk"][key] = list(dict.fromkeys(
            [row["ticker"] for row in snap.get("top_losers", []) if row.get("daily_pct", 0) <= -2]
            + [row["ticker"] for row in snap.get("top_gainers", []) if row.get("daily_pct", 0) >= 7]
        ))
    return result


def news_evidence(context: Mapping[str, Any]) -> list[dict]:
    items = list(context.get("market_news") or [])
    items.extend(item for rows in (context.get("ticker_news") or {}).values() for item in rows)
    unique = []
    seen = set()
    for item in items:
        key = item.get("url") or item.get("title")
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append({**item, "id": f"N{len(unique) + 1}"})
    return unique


def parse_selection(text: str, allowed: dict, news: list[dict]) -> dict:
    value = str(text).strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    selection = json.loads(value)
    if not isinstance(selection, dict) or set(selection) != {"focus", "risk", "news_ids"}:
        raise ValueError("LLM selection must contain only focus/risk/news_ids")
    for category in ("focus", "risk"):
        choices = selection[category]
        if not isinstance(choices, dict) or set(choices) != set(allowed[category]):
            raise ValueError("LLM market selection is invalid")
        for market, tickers in choices.items():
            if not isinstance(tickers, list) or len(tickers) > 2 or any(not isinstance(t, str) or t not in allowed[category][market] for t in tickers):
                raise ValueError("LLM selected unsupported ticker")
            if len(set(tickers)) != len(tickers):
                raise ValueError("LLM selected duplicate ticker")
    ids = selection["news_ids"]
    if not isinstance(ids, list) or len(ids) > 2 or any(not isinstance(i, str) or i not in {n["id"] for n in news} for i in ids):
        raise ValueError("LLM selected unsupported news")
    return selection


def market_stance(snap: Mapping[str, Any]) -> str:
    breadth = snap.get("breadth") or {}
    total = breadth.get("total", 0)
    down_ratio = breadth.get("declining", 0) / total if total else 0
    benchmarks = snap.get("benchmarks") or []
    worst = min((row["daily_pct"] for row in benchmarks), default=0)
    if worst <= -3 or (worst <= -1 and down_ratio >= 0.55) or down_ratio >= 0.75:
        return "防守"
    if benchmarks and all(row["daily_pct"] > 0 for row in benchmarks) and down_ratio <= 0.25:
        return "进攻"
    return "观察"


def render_report(snapshots: Mapping[str, Mapping[str, Any]], *, selection: dict | None = None,
                  news_context: Mapping[str, Any] | None = None, mode: str = "rule_only") -> str:
    allowed = candidates(snapshots)
    news = news_evidence(news_context or {})
    selection = selection or {
        "focus": {key: items[:2] for key, items in allowed["focus"].items()},
        "risk": {key: items[:2] for key, items in allowed["risk"].items()},
        "news_ids": [item["id"] for item in news[:2]],
    }
    stances = {key: market_stance(snap) for key, snap in snapshots.items()}
    verdict = "防守" if "防守" in stances.values() else ("进攻" if stances and all(v == "进攻" for v in stances.values()) else "观察")
    dates = "；".join(f"{snap['label']} {snap.get('data_date', '未标注')}" for snap in snapshots.values())
    lines = ["📅 今天方向简报", f"行情截至各市场完整收盘：{dates}", "", f"总判断：今天建议【{verdict}】。"]
    lines.append("；".join(f"{snap['label']}偏{stances[key]}" for key, snap in snapshots.items()) + "。" + ("个别逆势上涨不代表整体转强，先控制追涨节奏。" if verdict == "防守" else "关注基准走势和样本广度的持续确认。"))
    if mode != "llm_selection":
        lines.append("规则版扫描，本次未调用 LLM。" if mode == "rule_only" else "模型选择未通过校验，使用规则版候选。")
    icons = {"us": "🇺🇸", "cn": "🇨🇳", "hk": "🇭🇰"}
    for key, snap in snapshots.items():
        lines += ["", f"{icons.get(key, '')} {snap['label']}（{snap.get('data_date', '未标注')} 收盘）"]
        benchmarks = snap.get("benchmarks", [])
        if benchmarks:
            lines.append("市场基准：" + "、".join(f"{row['name']} {row['daily_pct']:+.2f}%" for row in benchmarks) + "。")
        breadth = snap.get("breadth") or {}
        lines.append(f"样本广度：上涨 {breadth.get('advancing', 0)} / 下跌 {breadth.get('declining', 0)} / 平盘 {breadth.get('unchanged', 0)}；有效 {snap.get('downloaded', 0)}/{snap.get('universe_size', 0)} 只，不能代表全市场。")
        rows = {row["ticker"]: row for field in ("top_signals", "top_gainers", "top_losers") for row in snap.get(field, [])}
        focus = selection["focus"].get(key, [])
        for ticker in focus:
            row = rows[ticker]
            name, sector = identity(ticker)
            marks = []
            if row.get("breakout_ma5"): marks.append("突破五日线")
            if row.get("breakout_20d"): marks.append("突破20日高点")
            if row.get("daily_long_align"): marks.append("均线多头排列")
            mark = "，" + "、".join(marks) if marks else ""
            display = f"{ticker} {name}{'（' + sector + '）' if sector else ''}".strip()
            lines.append(f"• 观察：{display} {row['daily_pct']:+.2f}%，量比 {row['vol_ratio']:.2f}，基准分 {row['quant_baseline_score']}/100{mark}。")
        if not focus:
            lines.append("• 暂无达到筛选门槛的强信号，不强行选方向。")
        for ticker in selection["risk"].get(key, []):
            row = rows[ticker]
            name, sector = identity(ticker)
            display = f"{ticker} {name}{'（' + sector + '）' if sector else ''}".strip()
            lines.append(f"• 风险：{display} {row['daily_pct']:+.2f}%，量比 {row['vol_ratio']:.2f}。")
    lines += ["", "📢 今日资讯/事件"]
    chosen = [item for item in news if item["id"] in selection["news_ids"]]
    if chosen:
        for item in chosen:
            lines.append(f"• {item.get('title', '')[:65]}（{item.get('publisher') or 'Yahoo 资讯'}，{item.get('published', '')[:10]}）")
            if item.get("url"): lines.append(str(item["url"]))
        lines.append("资讯仅列已取得来源，未验证其与涨跌的因果关系。")
    else:
        lines.append("未取得可用的近期资讯；无法据此认定没有事件或宏观驱动。")
    lines += ["", "🔍 异动观察", "；".join(f"{snap['label']}样本偏{stances[key]}" for key, snap in snapshots.items()) + "，个股信号与市场方向分开观察。", "", "✅ 今天优先关注"]
    focus_list = [ticker for values in selection["focus"].values() for ticker in values]
    lines.append("、".join(focus_list) + "；只作候选，等待后续量价确认。" if focus_list else "暂无强信号，等待基准和量价确认。")
    lines += ["", "⚠️ 今天避免追高"]
    risk_list = [ticker for values in selection["risk"].values() for ticker in values]
    lines.append("、".join(risk_list) + "；承压或波动较大，关注风险。" if risk_list else "避免未获量价确认的追涨。")
    verified = bool((snapshots.get("us") or {}).get("crosscheck_comparisons"))
    lines += ["", "数据：Yahoo 完整收盘日线" + ("；美股已由富途正规时段快照交叉核对。" if verified else "。"), "仅作研究参考，不构成投资建议。"]
    text = "\n".join(lines)
    if len(text) > 1500:
        # Preserve numerical facts. Long news URLs remain in the audit JSON.
        text = "\n".join(line for line in lines if not line.startswith(("https://", "http://")))
    if len(text) > 1500:
        compact = {category: {key: items[:1] for key, items in selection[category].items()} for category in ("focus", "risk")}
        compact["news_ids"] = selection["news_ids"][:1]
        if compact != selection:
            return render_report(snapshots, selection=compact, news_context=news_context, mode=mode)
        raise ValueError("Direction report exceeds message budget")
    return text
