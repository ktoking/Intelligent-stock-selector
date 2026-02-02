"""
单标的综合分析：技术面 + 消息面 + 财报，调用 LLM 输出趋势结构、MACD、KDJ、分析原因、评分、交易动作、加仓/减仓价。
"""
import re
from typing import Dict, Any, Optional

import yfinance as yf
from llm import ask_llm

from agents.technical import get_technical_summary
from agents.news import get_news_summary, get_news_summary_llm
from agents.fundamental import get_fundamental_data, get_financials_interpretation
from agents.options import get_put_call_summary
from config.tickers import TICKER_ZH_NAMES


def _market_from_ticker(ticker: str) -> str:
    """根据 ticker 后缀识别市场：.HK=港股，.SZ/.SS=A股，否则美股。"""
    t = (ticker or "").upper()
    if ".HK" in t:
        return "港股"
    if ".SZ" in t or ".SS" in t:
        return "A股"
    return "美股"


def _interval_label(interval: str, prepost: bool) -> str:
    """用于报告标题/卡片的 K 线周期描述。"""
    if interval == "1d":
        return "日K"
    labels = {"1m": "1分钟K", "5m": "5分钟K", "15m": "15分钟K", "30m": "30分钟K", "60m": "60分钟K"}
    s = labels.get(interval, f"{interval}K")
    if prepost:
        s += "（含盘前盘后）"
    return s


def _build_prompt(
    ticker: str,
    technical: dict,
    news: dict,
    fundamental: dict,
    options_summary: dict,
    interval: str = "1d",
    include_prepost: bool = False,
    news_llm_summary: str = "",
    financials_interpretation: str = "",
) -> str:
    tech_text = "无数据"
    tech_levels = technical.get("tech_levels") or {}
    if technical.get("ok"):
        t = technical
        tm = t.get("trend_ma") or {}
        ms = t.get("macd_summary") or {}
        ks = t.get("kdj_summary") or {}
        long_align = t.get("daily_long_align", False)
        k_label = "日K" if interval == "1d" else f"{interval}分K"
        tech_text = f"""
【{k_label} / 均线趋势】
当前价: {tm.get('price')}  MA5: {tm.get('ma5')}  MA10: {tm.get('ma10')}  MA20: {tm.get('ma20')}  MA60: {tm.get('ma60')}
多头排列(价>MA5>MA10>MA20>MA60): {long_align}
价格在MA5之上: {tm.get('above_ma5')}  在MA20之上: {tm.get('above_ma20')}  在MA60之上: {tm.get('above_ma60')}

【MACD】
MACD: {ms.get('macd')}  Signal: {ms.get('signal')}  柱: {ms.get('histogram')}
零轴上方: {ms.get('above_zero')}  金叉: {ms.get('golden_cross')}

【KDJ】
K: {ks.get('k')}  D: {ks.get('d')}  J: {ks.get('j')}
超买(>80): {ks.get('overbought')}  超卖(<20): {ks.get('oversold')}

【技术面入场/离场参考（供你评估加仓价与减仓价）】
入场参考: {tech_levels.get('entry_note') or '—'}
离场参考: {tech_levels.get('exit_note') or '—'}
"""

    news_text = "无"
    if news.get("news"):
        raw_titles = "\n".join(
            f"- {n.get('title', '')} ({n.get('published', '')})"
            for n in news["news"][:5]
        )
        if news_llm_summary and news_llm_summary.strip():
            news_text = f"新闻摘要：{news_llm_summary.strip()}\n近期标题：\n{raw_titles}"
        else:
            news_text = raw_titles

    fund = fundamental
    pe_str = fund.get("pe") or "—"
    opt_desc = options_summary.get("description") or "—"
    opt_ratio = options_summary.get("ratio")
    opt_str = f"{opt_desc}" + (f" (put/call={opt_ratio})" if opt_ratio is not None else "")

    fund_intro = ""
    if financials_interpretation and financials_interpretation.strip():
        fund_intro = f"财报解读：{financials_interpretation.strip()}\n"
    fund_raw = (fund.get("financials_str") or "")[:600]
    fund_text = f"""
公司: {fund.get('short_name')} ({fund.get('ticker')})
行业: {fund.get('industry')}  板块: {fund.get('sector')}
市值: {fund.get('market_cap')}  当前价: {fund.get('current_price')}  涨跌幅: {fund.get('change_pct')}%
市盈率PE: {pe_str}
近日多空期权: {opt_str}
{fund_intro}财报原始摘要(参考): {fund_raw}...
"""

    is_intraday = interval != "1d"
    prepost_note = "（含盘前盘后数据）" if include_prepost else ""
    role = "美股多维度分析师（日K技术面+消息面+财报+期权）" if not is_intraday else "美股超短线/日内分析师（分K技术面+消息面+期权，侧重短线信号）"
    time_scope = "日线维度" if not is_intraday else f"{interval}分K/短线维度{prepost_note}"
    trend_hint = "日K均线排列与趋势，是否多头排列" if not is_intraday else "分K均线排列与短线趋势"

    return f"""
你是一位{role}。请以{time_scope}，根据下面【技术面】【消息面】【财报/估值/期权】数据，用中文输出以下 10 项，每项单独一行，格式严格如下（不要多写其他内容）：

核心结论：<一句话总结该标的当前是否值得关注及主要理由>
趋势结构：<一句话描述{trend_hint}>
MACD状态：<一句话描述MACD位置与金叉死叉>
KDJ状态：<一句话描述超买超卖与钝化>
分析原因：<2-4句综合结论，可结合PE、期权多空、均线排列>
评分：<10-1的数字，仅数字，10=最强 1=最弱>
评分理由：<一句话说明为何给该评分，如 均线多头+PE合理+期权偏多 或 技术承压+估值偏高>
交易动作：<仅填其一：买入 / 观察 / 离场。偏多或可加仓填「买入」，偏空或减仓填「离场」，不确定填「观察」。>
加仓价格：<尽量根据上方【技术面入场/离场参考】给出具体价位数字，如 185.50；仅当确实无参考或无法给出时填“—”—>
减仓价格：<尽量根据上方离场参考（跌破MA20/MA60等）给出具体价位数字；仅当确实无参考时填“—”—>

【技术面】
{tech_text}

【消息面】
{news_text}

【财报/估值/期权】
{fund_text}
"""


def _parse_llm_output(text: str) -> Dict[str, Any]:
    out = {
        "core_conclusion": "",
        "trend_structure": "",
        "macd_status": "",
        "kdj_status": "",
        "analysis_reason": "",
        "score": 5,
        "score_reason": "",
        "action": "观察",
        "add_price": "—",
        "reduce_price": "—",
    }
    for line in (text or "").strip().split("\n"):
        line = line.strip()
        if line.startswith("核心结论："):
            out["core_conclusion"] = line.replace("核心结论：", "").strip()
        elif line.startswith("趋势结构："):
            out["trend_structure"] = line.replace("趋势结构：", "").strip()
        elif line.startswith("MACD状态："):
            out["macd_status"] = line.replace("MACD状态：", "").strip()
        elif line.startswith("KDJ状态："):
            out["kdj_status"] = line.replace("KDJ状态：", "").strip()
        elif line.startswith("分析原因："):
            out["analysis_reason"] = line.replace("分析原因：", "").strip()
        elif line.startswith("评分："):
            try:
                s = re.sub(r"[^\d.]", "", line.split("：")[-1].strip()) or "5"
                raw = float(s) if s else 5
                out["score"] = max(1, min(10, raw))  # 10-1 分制，超出则截断
            except Exception:
                out["score"] = 5
        elif line.startswith("评分理由："):
            out["score_reason"] = line.replace("评分理由：", "").strip()
        elif line.startswith("交易动作："):
            raw_action = line.replace("交易动作：", "").strip() or "观察"
            out["action"] = _normalize_action(raw_action)
        elif line.startswith("加仓价格："):
            v = line.replace("加仓价格：", "").strip()
            out["add_price"] = v if v and v != "—" and v != "-" else "—"
        elif line.startswith("减仓价格："):
            v = line.replace("减仓价格：", "").strip()
            out["reduce_price"] = v if v and v != "—" and v != "-" else "—"
    return out


def _normalize_action(raw: str) -> str:
    """将 LLM 输出的交易动作归一为：买入 / 观察 / 离场。"""
    a = (raw or "").strip()
    if not a:
        return "观察"
    if "买入" in a or "多头" in a or "加仓" in a or "轻仓" in a or "做多" in a:
        return "买入"
    if "离场" in a or "空头" in a or "减仓" in a or "禁止" in a or "卖出" in a or "做空" in a:
        return "离场"
    return "观察"


def run_full_analysis(
    ticker: str,
    interval: str = "1d",
    include_prepost: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    对单只标的做技术+消息+财报+期权综合分析，返回报告卡片所需字段。
    interval: 1d=日K（波段），5m/15m/1m=分K（超短线）。
    include_prepost: 是否含盘前盘后数据（仅分K时常用）。
    若某步失败则返回 None 或部分数据。
    """
    ticker = ticker.upper().strip()
    interval = (interval or "1d").strip().lower()
    technical = get_technical_summary(ticker, interval=interval, prepost=include_prepost)
    news = get_news_summary(ticker)
    news_llm = get_news_summary_llm(ticker, news.get("news") or []) if news.get("news") else ""
    # 日 K 且勾选盘前/盘后时，涨跌幅与当前价使用盘前/盘后数据
    fundamental = get_fundamental_data(ticker, use_prepost=(interval == "1d" and include_prepost))
    financials_interpretation = get_financials_interpretation(ticker, fundamental.get("financials_str") or "")
    options_summary = get_put_call_summary(ticker)

    prompt = _build_prompt(
        ticker, technical, news, fundamental, options_summary,
        interval=interval, include_prepost=include_prepost,
        news_llm_summary=news_llm, financials_interpretation=financials_interpretation,
    )
    try:
        print(f"[Report] {ticker} LLM 综合评分开始（等待 Ollama/API）…", flush=True)
        raw = ask_llm(
            system="你是美股多维度分析师。请严格按用户要求的 10 项格式输出，每行一项，不要遗漏。",
            user=prompt,
        )
        print(f"[Report] {ticker} LLM 综合评分完成", flush=True)
    except Exception as e:
        print(f"[Report] {ticker} LLM 综合评分异常: {e}", flush=True)
        raw = ""
    parsed = _parse_llm_output(raw)

    price = fundamental.get("current_price")
    change_pct = fundamental.get("change_pct")
    if price is not None and change_pct is not None:
        price_str = f"{price:.2f}"
        change_str = f"{change_pct:+.2f}%"
    else:
        price_str = "—"
        change_str = "—"

    market = _market_from_ticker(ticker)
    # A股/港股展示中文名称（有映射用映射，否则用 yfinance short_name）
    if market in ("A股", "港股"):
        name = TICKER_ZH_NAMES.get(ticker) or fundamental.get("short_name") or ticker
    else:
        name = fundamental.get("short_name") or ticker
    return {
        "ticker": ticker,
        "name": name,
        "core_conclusion": parsed["core_conclusion"] or "—",
        "score": parsed["score"],
        "score_reason": parsed.get("score_reason") or "—",
        "action": parsed["action"],
        "sector": fundamental.get("industry") or fundamental.get("sector") or "—",
        "current_price": price_str,
        "change_pct": change_str,
        "change_pct_raw": change_pct,
        "market_cap": fundamental.get("market_cap") or "—",
        "market": market,
        "add_price": parsed["add_price"],
        "reduce_price": parsed["reduce_price"],
        "tech_entry_note": (technical.get("tech_levels") or {}).get("entry_note") or "—",
        "tech_exit_note": (technical.get("tech_levels") or {}).get("exit_note") or "—",
        "trend_structure": parsed["trend_structure"] or "—",
        "macd_status": parsed["macd_status"] or "—",
        "kdj_status": parsed["kdj_status"] or "—",
        "analysis_reason": parsed["analysis_reason"] or "—",
        "daily_long_align": technical.get("daily_long_align", False),
        "pe": fundamental.get("pe") or "—",
        "put_call": options_summary.get("description") or "—",
        "last_date": technical.get("last_date"),
        "week52_high": fundamental.get("week52_high"),
        "week52_low": fundamental.get("week52_low"),
        "volume_ratio": fundamental.get("volume_ratio"),
        "dividend_yield": fundamental.get("dividend_yield"),
        "recommendation": fundamental.get("recommendation"),
        "next_earnings": fundamental.get("next_earnings"),
        "interval_label": _interval_label(interval, include_prepost),
        "interval": interval,
        "prepost": include_prepost,
    }
