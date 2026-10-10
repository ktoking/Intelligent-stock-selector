"""Render facts from validated inputs; the LLM may only select evidence IDs."""
from __future__ import annotations

import json
import re
import pandas as pd
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
    "LITE": ("Lumentum", "光通信"), "PANW": ("Palo Alto Networks", "网络安全"),
    "TMUS": ("T-Mobile US", "电信"), "SBUX": ("星巴克", "餐饮"),
    "601899.SS": ("紫金矿业", "有色金属"), "300059.SZ": ("东方财富", "金融信息服务"),
    "600887.SS": ("伊利股份", "乳制品"), "1024.HK": ("快手", "短视频"),
    "0285.HK": ("比亚迪电子", "电子制造"), "0836.HK": ("华润电力", "电力"),
    "0316.HK": ("东方海外国际", "航运"), "1810.HK": ("小米集团", "消费电子"),
    "2628.HK": ("中国人寿", "保险"), "1093.HK": ("石药集团", "制药"),
}


def identity(ticker: str) -> tuple[str, str]:
    return IDENTITIES.get(ticker, (TICKER_ZH_NAMES.get(ticker, ""), ""))


def opportunity_rank(row: Mapping[str, Any], snap: Mapping[str, Any]) -> float:
    benchmark = next((b["daily_pct"] for b in snap.get("benchmarks", []) if b.get("ticker") in ("QQQ", "000001.SS", "^HSI")), 0)
    return (row.get("quant_baseline_score", 0) + 2 * (row.get("daily_pct", 0) - benchmark)
            + 3 * bool(row.get("breakout_20d")) + 2 * bool(row.get("breakout_ma5"))
            + min(row.get("vol_ratio", 0), 3))


def candidates(snapshots: Mapping[str, Mapping[str, Any]], *, style: str = "aggressive") -> dict:
    result = {"focus": {}, "risk": {}}
    for key, snap in snapshots.items():
        pool = list({row["ticker"]: row for field in ("top_gainers", "top_signals") for row in snap.get(field, [])}.values())
        pool.sort(key=lambda row: opportunity_rank(row, snap), reverse=True)
        result["focus"][key] = [row["ticker"] for row in pool
                                if 0 < row.get("daily_pct", 0) < 7 and
                                (row.get("quant_baseline_score", 0) >= 58 or
                                 (style == "aggressive" and row.get("quant_baseline_score", 0) >= 52 and
                                  (row.get("breakout_20d") or row.get("breakout_ma5") or
                                   (row.get("vol_ratio", 0) >= 1.2 and row.get("daily_pct", 0) >= 1))))]
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


def default_news_ids(news: list[dict]) -> list[str]:
    chosen = []
    for market in ("us", "cn", "hk"):
        item = next((row for row in news if row.get("market") == market), None)
        if item:
            chosen.append(item["id"])
    chosen.extend(row["id"] for row in news if row["id"] not in chosen)
    return chosen[:3]


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
    if not isinstance(ids, list) or len(ids) > 3 or len(set(ids)) != len(ids) or any(not isinstance(i, str) or i not in {n["id"] for n in news} for i in ids):
        raise ValueError("LLM selected unsupported news")
    return selection


def market_stance(snap: Mapping[str, Any], *, style: str = "aggressive") -> str:
    breadth = snap.get("breadth") or {}
    total = breadth.get("total", 0)
    down_ratio = breadth.get("declining", 0) / total if total else 0
    benchmarks = snap.get("benchmarks") or []
    worst = min((row["daily_pct"] for row in benchmarks), default=0)
    if worst <= -3 or (worst <= -1 and down_ratio >= 0.55) or down_ratio >= 0.75:
        return "防守"
    if style == "aggressive" and benchmarks and total and worst > -1.5 and sum(row["daily_pct"] for row in benchmarks) / len(benchmarks) >= 0 and breadth.get("advancing", 0) / total >= 0.5:
        return "进攻"
    if benchmarks and all(row["daily_pct"] > 0 for row in benchmarks) and down_ratio <= 0.25:
        return "进攻"
    return "观察"


def render_report(snapshots: Mapping[str, Mapping[str, Any]], *, selection: dict | None = None,
                  news_context: Mapping[str, Any] | None = None, mode: str = "rule_only", style: str = "aggressive") -> str:
    allowed = candidates(snapshots, style=style)
    news = news_evidence(news_context or {})
    selection = selection or {
        "focus": {key: items[:2] for key, items in allowed["focus"].items()},
        "risk": {key: items[:2] for key, items in allowed["risk"].items()},
        "news_ids": default_news_ids(news),
    }
    stances = {key: market_stance(snap, style=style) for key, snap in snapshots.items()}
    verdict = "防守" if "防守" in stances.values() else ("进攻" if stances and all(v == "进攻" for v in stances.values()) else "观察")
    eligible = [key for key in snapshots if stances[key] != "防守" and selection["focus"].get(key) and snapshots[key].get("benchmarks")]
    if style == "aggressive" and eligible:
        verdict = "进攻" if all(v == "进攻" for v in stances.values()) else "分市场进攻"
    lines = ["📅 今天方向简报", "", f"**总判断：今天建议【{verdict}】。**"]
    if eligible and style == "aggressive":
        lines.append("主攻 " + " / ".join(snapshots[key]["label"] for key in eligible) + "的强势候选；放量突破再试探，跳空过大等回踩。")
    else:
        lines.append("优先盯逆势强股，放量突破再试探；市场承压时缩小试探范围。" if verdict == "防守" else "优先盯强势候选，放量续强再行动。")
    icons = {"us": "🇺🇸", "cn": "🇨🇳", "hk": "🇭🇰"}
    for key, snap in snapshots.items():
        lines += ["", f"{icons.get(key, '')} **{snap['label']} · {stances[key]}** ｜ {snap.get('data_date', '未标注')}收盘"]
        benchmarks = snap.get("benchmarks", [])
        if benchmarks:
            lines.append(" / ".join(f"{row['name']} {row['daily_pct']:+.2f}%" for row in benchmarks))
        breadth = snap.get("breadth") or {}
        lines.append(f"样本 ↑{breadth.get('advancing', 0)} ↓{breadth.get('declining', 0)} →{breadth.get('unchanged', 0)} · 覆盖 {snap.get('downloaded', 0)}/{snap.get('universe_size', 0)}")
        rows = {row["ticker"]: row for field in ("top_signals", "top_gainers", "top_losers") for row in snap.get(field, [])}
        focus = selection["focus"].get(key, [])
        for ticker in focus:
            row = rows[ticker]
            name, sector = identity(ticker)
            marks = []
            if row.get("breakout_ma5"): marks.append("突破五日线")
            if row.get("breakout_20d"): marks.append("突破20日高点")
            if row.get("daily_long_align"): marks.append("均线多头排列")
            mark = " · " + "、".join(marks[:2]) if marks else ""
            display = f"{ticker} {name}{'（' + sector + '）' if sector else ''}".strip()
            label = "逆势候选" if stances[key] == "防守" else "进攻候选"
            lines.append(f"• **{label}** {display} {row['daily_pct']:+.2f}% · 量比 {row['vol_ratio']:.2f}{mark}")
            if row.get("high") and row.get("low"):
                lines.append(f"  触发：放量站上 {row['high']:g}；失效：跌破 {row['low']:g}。")
            else:
                lines.append("  触发：放量突破收盘日高点；跌破该日低点取消观察。")
        if not focus:
            lines.append("• 暂无合格进攻候选，等待放量转强。")
        for ticker in selection["risk"].get(key, [])[:1]:
            row = rows[ticker]
            name, sector = identity(ticker)
            display = f"{ticker} {name}{'（' + sector + '）' if sector else ''}".strip()
            warning = "涨幅过大，等回踩" if row["daily_pct"] >= 7 else "承压，暂不抄底"
            lines.append(f"• 回避：{display} {row['daily_pct']:+.2f}% · {warning}")
    lines += ["", "📢 **关键事件 · 近72小时**"]
    chosen = [item for item in news if item["id"] in selection["news_ids"]]
    if chosen:
        for item in chosen:
            stamp = pd.to_datetime(item.get("published"), utc=True, errors="coerce")
            date = stamp.tz_convert("Asia/Shanghai").strftime("%m-%d %H:%M") if not pd.isna(stamp) else "日期未标注"
            source = item.get("publisher") or "Yahoo"
            raw_title = str(item.get("title", ""))
            title = (raw_title[:90] + ("…" if len(raw_title) > 90 else "")).replace("[", "（").replace("]", "）").replace("*", "")
            link = str(item.get("url") or "")
            title = f"[{title}]({link})" if link.startswith("https://") else title
            lines.append(f"• {title} ｜ {source} {date}")
        lines.append("事件可能晚于收盘；作为下一交易时段线索，非涨跌归因。")
    else:
        lines.append("资讯源暂不可用，事件线索待补；本次仅按量价筛选。")
    lines += ["", "🎯 **优先顺序**"]
    focus_list = [ticker for values in selection["focus"].values() for ticker in values]
    focus_list.sort(key=lambda ticker: next((stances[key] != "防守", opportunity_rank(row, snap)) for key, snap in snapshots.items() for field in ("top_signals", "top_gainers") for row in snap.get(field, []) if row["ticker"] == ticker), reverse=True)
    lines.append(" → ".join(focus_list[:3]) + "；等触发，跳空过大等回踩。" if focus_list else "等待强势候选，不强行进攻。")
    verified = bool((snapshots.get("us") or {}).get("crosscheck_comparisons"))
    lines += ["", "行情：Yahoo 完整收盘" + (" / 富途核对" if verified else "") + "；样本不代表全市场。",
              "触发/失效取收盘日高/低点（复权价），需在下一交易时段确认。",
              ("风格：积极寻找机会" if style == "aggressive" else "风格：稳健筛选") + "；仅研究参考，不构成投资建议。"]
    if mode != "llm_selection":
        lines.append("规则版扫描，本次未调用 LLM。" if mode == "rule_only" else "模型选择未通过校验，使用规则版候选。")
    text = "\n".join(lines)
    if len(text) > 1500:
        # Preserve numerical facts. Long news URLs remain in the audit JSON.
        text = re.sub(r"\[([^\]]+)\]\(https?://[^)]+\)", r"\1", text)
    if len(text) > 1500:
        compact = {category: {key: items[:1] for key, items in selection[category].items()} for category in ("focus", "risk")}
        compact["news_ids"] = selection["news_ids"]
        if compact != selection:
            return render_report(snapshots, selection=compact, news_context=news_context, mode=mode, style=style)
        raise ValueError("Direction report exceeds message budget")
    return text
