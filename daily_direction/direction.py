from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

import pandas as pd

from config.tickers import (
    MARKET_CN,
    MARKET_HK,
    MARKET_US,
    POOL_CSI300,
    POOL_HK_HSI,
    POOL_NASDAQ100,
    get_report_tickers,
)
from data.us_movers_scan import _download_ohlcv_by_ticker
from agents.score_baseline import compute_quant_baseline
from daily_direction.hithink_market import fetch_hithink_cn_quotes
from daily_direction.news import collect_direction_news


@dataclass(frozen=True)
class MarketJob:
    key: str
    label: str
    market: str
    pool: str
    limit: int = 80


DEFAULT_MARKET_JOBS: List[MarketJob] = [
    MarketJob(key="us", label="美股", market=MARKET_US, pool=POOL_NASDAQ100, limit=80),
    MarketJob(key="cn", label="A股", market=MARKET_CN, pool=POOL_CSI300, limit=80),
    MarketJob(key="hk", label="港股", market=MARKET_HK, pool=POOL_HK_HSI, limit=80),
]


def _to_float(value: Any) -> Optional[float]:
    try:
        if pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


def _last(series: pd.Series) -> Optional[float]:
    if series is None or series.empty:
        return None
    return _to_float(series.iloc[-1])


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))


def _build_baseline_inputs(d: pd.DataFrame) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    close = d["Close"].astype(float)
    high = d["High"].astype(float)
    volume = d["Volume"].astype(float)

    price = _last(close)
    ma5 = _last(close.rolling(5).mean())
    ma10 = _last(close.rolling(10).mean())
    ma20 = _last(close.rolling(20).mean())
    ma60 = _last(close.rolling(60).mean())
    daily_long_align = bool(
        price is not None
        and ma5 is not None
        and ma10 is not None
        and ma20 is not None
        and ma60 is not None
        and price > ma5 > ma10 > ma20 > ma60
    )

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    macd_now = _last(macd)
    signal_now = _last(signal)
    macd_prev = _to_float(macd.iloc[-2]) if len(macd) >= 2 else None
    signal_prev = _to_float(signal.iloc[-2]) if len(signal) >= 2 else None
    macd_summary = {
        "above_zero": bool(macd_now is not None and macd_now > 0),
        "golden_cross": bool(
            macd_now is not None
            and signal_now is not None
            and macd_prev is not None
            and signal_prev is not None
            and macd_now > signal_now
            and macd_prev <= signal_prev
        ),
    }

    rsi_now = _last(_rsi(close))
    rsi_summary = {
        "rsi": rsi_now,
        "overbought": bool(rsi_now is not None and rsi_now >= 70),
        "oversold": bool(rsi_now is not None and rsi_now <= 30),
    }
    vol_ma20 = _last(volume.iloc[:-1].tail(20).rolling(20).mean())
    if vol_ma20 is None:
        vol_ma20 = _to_float(volume.iloc[:-1].tail(20).mean()) or 0.0
    vol_ratio = (_last(volume) or 0.0) / vol_ma20 if vol_ma20 > 0 else None

    prev_close = _to_float(close.iloc[-2]) if len(close) >= 2 else None
    change_pct = (price / prev_close - 1.0) * 100.0 if price and prev_close and prev_close > 0 else None
    close_20d_ago = _to_float(close.iloc[-21]) if len(close) >= 21 else None
    return_20d = (price / close_20d_ago - 1.0) * 100.0 if price and close_20d_ago and close_20d_ago > 0 else None
    high_52w = _to_float(high.tail(252).max()) or _to_float(high.max())
    dist_to_52w_high = (price / high_52w - 1.0) * 100.0 if price and high_52w and high_52w > 0 else None

    technical = {
        "ok": True,
        "daily_long_align": daily_long_align,
        "macd_summary": macd_summary,
        "kdj_summary": {},
        "rsi_summary": rsi_summary,
        "divergence_summary": {},
        "volume_context": {"volume_ratio": vol_ratio},
        "momentum_summary": {
            "return_20d_pct": return_20d,
            "dist_to_52w_high_pct": dist_to_52w_high,
        },
    }
    fundamental = {"change_pct": change_pct}
    return technical, fundamental, {}


def _ma5_breakout(close: pd.Series) -> tuple[bool, Optional[float], Optional[float]]:
    if len(close) < 6:
        return False, None, None
    ma5 = close.rolling(5).mean()
    today_close = _last(close)
    yesterday_close = _to_float(close.iloc[-2])
    today_ma5 = _last(ma5)
    yesterday_ma5 = _to_float(ma5.iloc[-2])
    breakout = bool(
        today_close is not None
        and yesterday_close is not None
        and today_ma5 is not None
        and yesterday_ma5 is not None
        and today_close > today_ma5
        and yesterday_close <= yesterday_ma5
    )
    return breakout, today_ma5, yesterday_ma5


def eval_daily_signal(df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """Convert daily OHLCV rows into a rankable signal for one ticker."""
    if df is None or df.empty:
        return None
    need = ("Close", "High", "Low", "Volume")
    if not all(c in df.columns for c in need):
        return None

    d = df[list(need)].copy()
    for col in need:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.dropna(subset=["Close", "High", "Low"])
    if len(d) < 22:
        return None

    today = d.iloc[-1]
    yesterday = d.iloc[-2]
    prior = d.iloc[:-1]

    close = _to_float(today["Close"])
    prev_close = _to_float(yesterday["Close"])
    volume = _to_float(today["Volume"]) or 0.0
    if close is None or prev_close is None or close <= 0 or prev_close <= 0:
        return None

    daily_pct = (close / prev_close - 1.0) * 100.0
    vol_ma20 = _to_float(prior["Volume"].tail(20).mean()) or 0.0
    vol_ratio = volume / vol_ma20 if vol_ma20 > 0 else 0.0
    high_20 = _to_float(prior["High"].tail(20).max()) or close
    breakout_20d = close >= high_20 * 0.999
    sma20 = _to_float(prior["Close"].tail(20).mean())
    sma50 = _to_float(prior["Close"].tail(50).mean()) if len(prior) >= 50 else None
    above_sma20 = bool(sma20 is not None and close >= sma20)
    above_sma50 = bool(sma50 is not None and close >= sma50)
    avg_turnover_20d = _to_float((prior["Close"] * prior["Volume"]).tail(20).mean()) or 0.0
    breakout_ma5, ma5, previous_ma5 = _ma5_breakout(d["Close"].astype(float))

    technical, fundamental, options_summary = _build_baseline_inputs(d)
    quant_baseline_score, baseline_note = compute_quant_baseline(technical, fundamental, options_summary)
    if breakout_ma5:
        quant_baseline_score = min(100, quant_baseline_score + 6)
        baseline_note = f"{baseline_note}；突破五日线+6"

    signal_score = daily_pct + min(vol_ratio, 6.0) * 0.8
    if breakout_20d:
        signal_score += 2.0
    if above_sma50:
        signal_score += 1.0
    if daily_pct < 0:
        signal_score -= 1.0

    return {
        "daily_pct": round(daily_pct, 2),
        "vol_ratio": round(vol_ratio, 2),
        "breakout_20d": breakout_20d,
        "above_sma20": above_sma20,
        "above_sma50": above_sma50,
        "breakout_ma5": breakout_ma5,
        "ma5": round(ma5, 4) if ma5 is not None else None,
        "previous_ma5": round(previous_ma5, 4) if previous_ma5 is not None else None,
        "close": round(close, 4),
        "volume": round(volume, 0),
        "avg_turnover_20d": round(avg_turnover_20d, 0),
        "signal_score": round(signal_score, 2),
        "quant_baseline_score": quant_baseline_score,
        "baseline_note": baseline_note,
    }


def _row_for_ticker(ticker: str, df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    signal = eval_daily_signal(df)
    if not signal:
        return None
    return {"ticker": ticker, **signal}


def _apply_quote_overlay(frame: pd.DataFrame, quote: Dict[str, float]) -> pd.DataFrame:
    if frame is None or frame.empty or not quote:
        return frame
    out = frame.copy()
    idx = out.index[-1]
    close = quote.get("close")
    daily_pct = quote.get("daily_pct")
    if close is not None:
        out.loc[idx, "Close"] = close
    if daily_pct is not None and close is not None and daily_pct > -99:
        out.iloc[-2, out.columns.get_loc("Close")] = close / (1 + daily_pct / 100.0)
    if quote.get("high") is not None:
        out.loc[idx, "High"] = quote["high"]
    elif close is not None and "High" in out.columns:
        out.loc[idx, "High"] = max(float(out.loc[idx, "High"]), close)
    if quote.get("low") is not None:
        out.loc[idx, "Low"] = quote["low"]
    elif close is not None and "Low" in out.columns:
        out.loc[idx, "Low"] = min(float(out.loc[idx, "Low"]), close)
    if quote.get("open") is not None and "Open" in out.columns:
        out.loc[idx, "Open"] = quote["open"]
    if quote.get("volume") is not None:
        out.loc[idx, "Volume"] = quote["volume"]
    return out


def scan_market(job: MarketJob, *, max_items: int = 15, period: str = "6mo") -> Dict[str, Any]:
    """Scan one market pool and return compact rows for LLM summarisation."""
    try:
        tickers = get_report_tickers(limit=job.limit, market=job.market, pool=job.pool)
    except Exception:
        tickers = []
    frames = _download_ohlcv_by_ticker(tickers, period=period, chunk=60) if tickers else {}
    quote_overlay_error = ""
    data_source = "yfinance"
    hithink_quotes: Dict[str, Dict[str, float]] = {}
    if job.market == MARKET_CN and frames:
        try:
            hithink_quotes = fetch_hithink_cn_quotes(frames.keys(), limit=max(job.limit, len(frames)))
            if hithink_quotes:
                data_source = "yfinance+hithink"
        except Exception as exc:
            quote_overlay_error = str(exc)[:300]

    rows: List[Dict[str, Any]] = []
    for ticker, frame in frames.items():
        quote = hithink_quotes.get(ticker) or {}
        if quote:
            frame = _apply_quote_overlay(frame, quote)
        row = _row_for_ticker(ticker, frame)
        if row:
            row["quote_source"] = "hithink" if quote else "yfinance"
            rows.append(row)

    positive = [r for r in rows if r["daily_pct"] > 0]
    positive.sort(key=lambda r: (r.get("quant_baseline_score", 0), r["signal_score"], r["daily_pct"]), reverse=True)
    gainers = sorted(rows, key=lambda r: r["daily_pct"], reverse=True)
    losers = sorted(rows, key=lambda r: r["daily_pct"])

    return {
        "key": job.key,
        "label": job.label,
        "market": job.market,
        "pool": job.pool,
        "requested_limit": job.limit,
        "downloaded": len(frames),
        "data_source": data_source,
        "quote_overlay_error": quote_overlay_error,
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "top_signals": positive[:max_items],
        "top_gainers": gainers[:max_items],
        "top_losers": losers[:max_items],
    }


def scan_markets(jobs: Iterable[MarketJob], *, max_items: int = 15, period: str = "6mo") -> Dict[str, Dict[str, Any]]:
    snapshots: Dict[str, Dict[str, Any]] = {}
    for job in jobs:
        snapshots[job.key] = scan_market(job, max_items=max_items, period=period)
    return snapshots


def build_llm_prompt(
    snapshots: Mapping[str, Mapping[str, Any]],
    jobs: Iterable[MarketJob],
    *,
    news_context: Optional[Mapping[str, Any]] = None,
) -> str:
    job_labels = "、".join(job.label for job in jobs)
    compact = json.dumps(snapshots, ensure_ascii=False, separators=(",", ":"))
    news_compact = json.dumps(news_context or {}, ensure_ascii=False, separators=(",", ":"))
    return f"""请基于以下 {job_labels} 的日线异动扫描，生成一份中文“今天方向简报”。

输出风格必须贴近这个结构，不要使用 Markdown 标题符号（例如 #、##、###），不要使用表格：

📅 今天方向简报

总判断：今天建议【观察/进攻/防守】。
用 1 段话解释跨市场主线、风险和操作节奏。

---

🇺🇸 美股
*   方向或板块（强信号）： 标的、信号分、涨跌幅、突破/均线/量能，并结合资讯解释原因。
*   方向或板块（突破/回暖/风险）： ...

🇨🇳 A股
*   个股异动： ...
*   权重观察： ...
*   风险警示： ...

🇭🇰 港股
*   高弹性标的： ...
*   科技权重/金融权重/其他线索： ...

---

📢 今日资讯/事件
*   美股： ...
*   中国/港股： ...
*   个股： ...

🔍 异动观察
*   多头共振： ...
*   恐慌抛售/风险扩散： ...

---

✅ 今天优先关注
*   板块或主题： 标的列表（逻辑：一句话）

⚠️ 今天避免追高
*   板块或标的： 标的列表（原因：一句话）

仅作研究参考，不构成投资建议。

硬性要求：
1. 总判断只能在【观察】、【进攻】、【防守】中选一个，整体偏谨慎。
2. 分市场写：美股 / A股 / 港股，各给 2-3 个方向或标的线索。
3. 必须包含“今日资讯/事件”和“异动观察”：资讯来自输入新闻，异动来自 top_gainers/top_losers/top_signals。
4. 给出“今天优先关注”和“今天避免追高”的清单。
5. 只使用输入数据，不要编造新闻或未给出的财报；没有新闻时明确写“暂无直接新闻触发”。
6. 输出适合发到 SeaTalk 群，控制在 1500 字内。
7. 可以使用 emoji、粗体和短列表；不要输出复杂 Markdown、代码块、表格、LaTeX 公式。
8. 结尾必须保留：仅作研究参考，不构成投资建议。

扫描数据 JSON：
{compact}

今日资讯 JSON：
{news_compact}
"""


def format_for_seatalk(text: str) -> str:
    """Keep markdown styling, but remove heading markers SeaTalk renders poorly."""
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines: List[str] = []
    for raw in value.split("\n"):
        line = raw.rstrip()
        if not line:
            lines.append("")
            continue
        line = re.sub(r"^\s*#{1,6}\s*", "", line)
        lines.append(line)
    compact: List[str] = []
    previous_blank = False
    for line in lines:
        blank = not line.strip()
        if blank and previous_blank:
            continue
        compact.append(line)
        previous_blank = blank
    return "\n".join(compact).strip()


def build_fallback_direction(snapshots: Mapping[str, Mapping[str, Any]], *, reason: str = "rule_only") -> str:
    note = "规则版扫描，本次未调用 LLM"
    if reason == "llm_failed":
        note = "LLM 暂不可用，先按规则扫描给出方向"
    lines = [
        "📅 今天方向简报",
        "",
        "总判断：今天建议【观察】。",
        f"{note}，优先看量价共振和关键均线突破，暂不做全线进攻。",
        "",
        "---",
        "",
    ]
    focus: List[str] = []
    avoid: List[str] = []
    unusual: List[str] = []
    market_icons = {"us": "🇺🇸", "cn": "🇨🇳", "hk": "🇭🇰"}
    for key in ("us", "cn", "hk"):
        snap = snapshots.get(key) or {}
        label = str(snap.get("label") or key)
        signals = list(snap.get("top_signals") or [])
        losers = list(snap.get("top_losers") or [])
        lines.append(f"{market_icons.get(key, '')} {label}".strip())
        if not signals:
            lines.append("*   观察为主： 无明显强信号，先观察指数和成交量是否确认。")
            lines.append("")
            continue
        for idx, row in enumerate(signals[:3]):
            ticker = row.get("ticker")
            pct = row.get("daily_pct")
            vol = row.get("vol_ratio")
            marks = []
            if row.get("breakout_ma5"):
                marks.append("突破五日线")
            if row.get("breakout_20d"):
                marks.append("20日突破")
            mark_text = f" / {'、'.join(marks)}" if marks else ""
            baseline = row.get("quant_baseline_score")
            baseline_text = f"，信号分 {baseline}" if baseline is not None else ""
            label_text = "强信号" if idx == 0 else ("突破观察" if marks else "结构线索")
            lines.append(f"*   {label_text}： {ticker} 当日 {pct:+.2f}%，量比 {vol:.2f}{baseline_text}{mark_text}。")
            if row.get("quant_baseline_score", 0) >= 65 or (
                row.get("breakout_ma5") and row.get("quant_baseline_score", 0) >= 58
            ):
                focus.append(str(ticker))
            if row.get("daily_pct", 0) >= 7:
                avoid.append(str(ticker))
        for row in losers[:2]:
            if row.get("daily_pct", 0) <= -5:
                unusual.append(f"{label} {row.get('ticker')} {row.get('daily_pct'):+.2f}%")
        lines.append("")

    lines.extend(["---", "", "📢 今日资讯/事件"])
    lines.append("*   暂无直接新闻触发；本段主要基于行情异动和已有候选股新闻源。")
    lines.append("")
    lines.append("🔍 异动观察")
    if unusual:
        lines.append("*   风险扩散： " + "；".join(unusual[:8]))
    else:
        lines.append("*   风险扩散： 暂无极端同步下跌异动。")
    lines.append("*   多头共振： " + ("、".join(focus[:8]) if focus else "等待更清晰的放量突破信号"))
    lines.extend(["", "---", "", "✅ 今天优先关注"])
    lines.append("*   强势突破/量价共振： " + ("、".join(focus[:8]) if focus else "暂不强行选方向，等待放量突破确认"))
    lines.append("")
    lines.append("⚠️ 今天避免追高")
    lines.append("*   高波动标的： " + ("、".join(avoid[:8]) if avoid else "暂无极端涨幅标的") + "。")
    lines.append("")
    lines.append("仅作研究参考，不构成投资建议。")
    return "\n".join(lines).strip()


def generate_direction_report(
    snapshots: Mapping[str, Mapping[str, Any]],
    jobs: Iterable[MarketJob],
    *,
    use_llm: bool = True,
    llm_func: Optional[Callable[..., str]] = None,
    news_context: Optional[Mapping[str, Any]] = None,
) -> str:
    if not use_llm:
        return build_fallback_direction(snapshots, reason="rule_only")

    if news_context is None:
        news_context = collect_direction_news(snapshots)
    prompt = build_llm_prompt(snapshots, jobs, news_context=news_context)
    system = "你是谨慎的跨市场交易研究助手，擅长把日线异动转成当天可执行的观察方向。"
    try:
        ask = llm_func
        if ask is None:
            from llm import ask_llm

            ask = ask_llm
        text = ask(system=system, user=prompt, temperature=0.2, max_tokens=1800)
        text = format_for_seatalk(text)
        if text:
            if "投资建议" not in text:
                text += "\n\n仅作研究参考，不构成投资建议。"
            return text
    except Exception as exc:
        fallback = build_fallback_direction(snapshots, reason="llm_failed")
        return f"{fallback}\n\nLLM 总结失败: {str(exc)[:200]}"
    return build_fallback_direction(snapshots, reason="llm_failed")
