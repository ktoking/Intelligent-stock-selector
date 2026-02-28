"""
既往推荐追踪与回测：记录每次报告中 9/10 分且「买入」的标的，并在后续报告中展示「今日表现」与胜率统计。
仅使用 JSONL 持久化，与 memory_store 同目录；不依赖外部 DB。
记录条件见 config/analysis_config（RECOMMEND_MIN_SCORE、RECOMMEND_ACTION）。
"""
import os
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

# 与 memory_store 同目录，便于统一备份
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DIR = _PROJECT_ROOT / "data" / "memory"
_STORE_DIR = Path(os.environ.get("STOCK_AGENT_MEMORY_DIR", "").strip() or str(_DEFAULT_DIR))
_FILENAME = "recommendations.jsonl"


def _file_path() -> Path:
    _STORE_DIR.mkdir(parents=True, exist_ok=True)
    return _STORE_DIR / _FILENAME


def save_recommendation(card: Dict[str, Any], report_date: str) -> None:
    """
    若卡片达到最低评分且动作为「买入」，则追加一条推荐记录。
    条件见 config/analysis_config（RECOMMEND_MIN_SCORE、RECOMMEND_ACTION）。
    report_date 建议为 YYYY-MM-DD（与报告生成日一致）。
    """
    try:
        from config.analysis_config import RECOMMEND_MIN_SCORE, RECOMMEND_ACTION
    except ImportError:
        RECOMMEND_MIN_SCORE = 9
        RECOMMEND_ACTION = "买入"
    score = card.get("score")
    try:
        s = float(score) if score is not None else 0
    except (TypeError, ValueError):
        s = 0
    action = (card.get("action") or "").strip()
    if s < RECOMMEND_MIN_SCORE or action != RECOMMEND_ACTION:
        return
    ticker = (card.get("ticker") or "").upper().strip()
    if not ticker:
        return
    price_raw = card.get("current_price") or ""
    try:
        price_at_report = float(str(price_raw).replace(",", "").strip()) if price_raw else None
    except (TypeError, ValueError):
        price_at_report = None
    reduce_price_raw = (card.get("reduce_price") or "").strip()
    reduce_price = None
    if reduce_price_raw and reduce_price_raw not in ("—", "-", ""):
        try:
            reduce_price = float(str(reduce_price_raw).replace(",", "").strip())
        except (TypeError, ValueError):
            pass
    record = {
        "ticker": ticker,
        "report_date": (report_date or "")[:10],
        "score": s,
        "action": action,
        "price_at_report": price_at_report,
        "reduce_price": reduce_price,
        "name": (card.get("name") or ticker).strip(),
        "market": (card.get("market") or "美股").strip(),
    }
    fp = _file_path()
    try:
        with open(fp, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def get_past_recommendations(since_days: int = 30) -> List[Dict[str, Any]]:
    """
    读取过去 since_days 天内的推荐记录（report_date 在范围内），按日期倒序。
    """
    cutoff = (datetime.now() - timedelta(days=max(1, since_days))).date().isoformat()
    fp = _file_path()
    if not fp.exists():
        return []
    records: List[Dict[str, Any]] = []
    try:
        with open(fp, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    rd = (r.get("report_date") or "")[:10]
                    if rd >= cutoff:
                        records.append(r)
                except (json.JSONDecodeError, TypeError):
                    continue
    except Exception:
        return []
    records.sort(key=lambda x: x.get("report_date") or "", reverse=True)
    return records


def _close_on_or_after(hist, target_date) -> Optional[float]:
    """取 hist 中第一个 index.date() >= target_date 的 Close；无则返回 None。"""
    if hist is None or hist.empty or "Close" not in hist.columns:
        return None
    try:
        for ts, row in hist.iterrows():
            try:
                d = ts.date() if hasattr(ts, "date") else ts
            except Exception:
                d = ts
            if d >= target_date:
                return float(row["Close"])
    except Exception:
        pass
    return None


def _latest_close(hist) -> Optional[float]:
    if hist is None or hist.empty or "Close" not in hist.columns:
        return None
    try:
        return float(hist["Close"].iloc[-1])
    except Exception:
        return None


def get_past_recommendations_with_returns(
    since_days: int = 30,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    在 get_past_recommendations 基础上，为每条记录拉取当前价、持有 1 周/1 月价，并计算涨跌幅与持有天数；
    拉取基准（标普500/沪深300/恒生）同期收益；计算最差收益与收益分布。
    同一标的多次推荐只保留报告日最早的一条（合并到第一条），避免重复统计。
    返回 (rows, summary)。summary 含多持有期胜率/收益、基准对比、风险指标。
    """
    rows = get_past_recommendations(since_days=since_days)
    if not rows:
        return [], {"total_count": 0, "win_count": 0, "win_rate_pct": 0.0, "avg_return_pct": 0.0}

    # 同一股票只保留报告日最早的一条（合并到第一条）
    by_ticker: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        t = (r.get("ticker") or "").upper().strip()
        if not t:
            continue
        rd = (r.get("report_date") or "")[:10]
        if t not in by_ticker or (rd and rd < (by_ticker[t].get("report_date") or "")[:10]):
            by_ticker[t] = r
    rows = list(by_ticker.values())
    rows.sort(key=lambda x: x.get("report_date") or "", reverse=True)

    try:
        import yfinance as yf
    except Exception:
        for r in rows:
            r["current_price"] = None
            r["return_pct"] = None
            r["holding_days"] = None
        return rows, {"total_count": len(rows), "win_count": 0, "win_rate_pct": 0.0, "avg_return_pct": 0.0}

    today = datetime.now().date()
    min_report_date = today
    for r in rows:
        rd = (r.get("report_date") or "")[:10]
        try:
            d = datetime.strptime(rd, "%Y-%m-%d").date()
            if d < min_report_date:
                min_report_date = d
        except Exception:
            pass
    start = min_report_date - timedelta(days=5)
    end = today + timedelta(days=1)

    tickers = list({(r.get("ticker") or "").upper().strip() for r in rows if r.get("ticker")})
    hist_by_ticker: Dict[str, Any] = {}
    for t in tickers:
        try:
            h = yf.Ticker(t).history(start=start, end=end)
            hist_by_ticker[t] = h if h is not None and not h.empty else None
        except Exception:
            hist_by_ticker[t] = None

    # 基准：标普500 / 沪深300 / 恒生 / 恒科（港股双基准）
    bench_tickers = {"美股": "^GSPC", "A股": "000300.SS", "港股": "^HSI", "恒科": "^HSTECH"}
    bench_hist: Dict[str, Any] = {}
    for _m, bt in bench_tickers.items():
        try:
            h = yf.Ticker(bt).history(start=start, end=end)
            bench_hist[bt] = h if h is not None and not h.empty else None
        except Exception:
            bench_hist[bt] = None

    returns: List[Optional[float]] = []
    returns_1w: List[Optional[float]] = []
    returns_1m: List[Optional[float]] = []
    returns_2m: List[Optional[float]] = []
    returns_3m: List[Optional[float]] = []
    bench_returns_us: List[float] = []
    bench_returns_cn: List[float] = []
    bench_returns_hk: List[float] = []
    bench_returns_hstech: List[float] = []

    for r in rows:
        t = (r.get("ticker") or "").upper().strip()
        rd = (r.get("report_date") or "")[:10]
        try:
            report_d = datetime.strptime(rd, "%Y-%m-%d").date()
        except Exception:
            report_d = today
        r["holding_days"] = (today - report_d).days

        hist = hist_by_ticker.get(t)
        price_then = r.get("price_at_report")
        price_now = _latest_close(hist) if hist is not None else None
        r["current_price"] = price_now

        if price_then and price_now and price_then > 0:
            r["return_pct"] = round((price_now - price_then) / price_then * 100, 2)
            returns.append(r["return_pct"])
        else:
            r["return_pct"] = None

        # 跌破卖出：当前价 ≤ 当时报告给出的减仓/离场价（reduce_price）则视为触及卖出信号
        reduce_price = r.get("reduce_price")
        if reduce_price is not None and price_now is not None and price_now <= reduce_price:
            r["triggered_exit"] = True
        else:
            r["triggered_exit"] = False

        # 持有 1 周 / 1 月 / 2 月 / 3 月 收益
        r["return_1w_pct"] = None
        r["return_1m_pct"] = None
        r["return_2m_pct"] = None
        r["return_3m_pct"] = None
        if hist is not None and price_then and price_then > 0:
            d7 = report_d + timedelta(days=7)
            d30 = report_d + timedelta(days=30)
            d60 = report_d + timedelta(days=60)
            d90 = report_d + timedelta(days=90)
            p7 = _close_on_or_after(hist, d7)
            p30 = _close_on_or_after(hist, d30)
            p60 = _close_on_or_after(hist, d60)
            p90 = _close_on_or_after(hist, d90)
            if p7 is not None and r["holding_days"] >= 7:
                r["return_1w_pct"] = round((p7 - price_then) / price_then * 100, 2)
                returns_1w.append(r["return_1w_pct"])
            if p30 is not None and r["holding_days"] >= 30:
                r["return_1m_pct"] = round((p30 - price_then) / price_then * 100, 2)
                returns_1m.append(r["return_1m_pct"])
            if p60 is not None and r["holding_days"] >= 60:
                r["return_2m_pct"] = round((p60 - price_then) / price_then * 100, 2)
                returns_2m.append(r["return_2m_pct"])
            if p90 is not None and r["holding_days"] >= 90:
                r["return_3m_pct"] = round((p90 - price_then) / price_then * 100, 2)
                returns_3m.append(r["return_3m_pct"])

        # 基准同期收益
        market = (r.get("market") or "美股").strip()
        bt = bench_tickers.get(market, "^GSPC")
        bh = bench_hist.get(bt)
        r["benchmark_return_pct"] = None
        if bh is not None:
            close_then = _close_on_or_after(bh, report_d)
            close_now = _latest_close(bh)
            if close_then and close_now and close_then > 0:
                r["benchmark_return_pct"] = round((close_now - close_then) / close_then * 100, 2)
                if market == "美股":
                    bench_returns_us.append(r["benchmark_return_pct"])
                elif market == "A股":
                    bench_returns_cn.append(r["benchmark_return_pct"])
                elif market == "港股":
                    bench_returns_hk.append(r["benchmark_return_pct"])
        # 港股额外：恒科同期收益（用于报告双列展示）
        r["benchmark_hstech_return_pct"] = None
        if market == "港股":
            bh_hstech = bench_hist.get("^HSTECH")
            if bh_hstech is not None:
                close_then = _close_on_or_after(bh_hstech, report_d)
                close_now = _latest_close(bh_hstech)
                if close_then and close_now and close_then > 0:
                    r["benchmark_hstech_return_pct"] = round((close_now - close_then) / close_then * 100, 2)
                    bench_returns_hstech.append(r["benchmark_hstech_return_pct"])

    valid_returns = [x for x in returns if x is not None]
    win_count = sum(1 for x in valid_returns if x > 0)
    total = len(rows)

    # 跌破卖出条数（当前价 ≤ 当时减仓价的记录）
    triggered_exit_count = sum(1 for r in rows if r.get("triggered_exit") is True)

    # 最近 N 条胜率（按推荐日倒序取前 N 条，便于一眼看近期表现）
    recent_n = min(10, len(rows))
    recent_rows = rows[:recent_n]
    recent_returns = [r.get("return_pct") for r in recent_rows if r.get("return_pct") is not None]
    recent_win_count = sum(1 for x in recent_returns if x > 0)
    recent_win_rate_pct = round(recent_win_count / len(recent_returns) * 100, 1) if recent_returns else 0.0

    # 1 周 / 1 月 / 2 月 / 3 月 胜率与平均收益
    total_1w = len(returns_1w)
    win_count_1w = sum(1 for x in returns_1w if x > 0)
    total_1m = len(returns_1m)
    win_count_1m = sum(1 for x in returns_1m if x > 0)
    total_2m = len(returns_2m)
    win_count_2m = sum(1 for x in returns_2m if x > 0)
    total_3m = len(returns_3m)
    win_count_3m = sum(1 for x in returns_3m if x > 0)

    # 风险：最差收益、最佳收益、收益分布
    worst_return_pct = min(valid_returns) if valid_returns else None
    best_return_pct = max(valid_returns) if valid_returns else None
    dist_up_10 = sum(1 for x in valid_returns if x > 10)
    dist_0_10 = sum(1 for x in valid_returns if 0 < x <= 10)
    dist_neg10_0 = sum(1 for x in valid_returns if -10 <= x < 0)
    dist_down_10 = sum(1 for x in valid_returns if x < -10)

    summary = {
        "total_count": total,
        "win_count": win_count,
        "win_rate_pct": round(win_count / total * 100, 1) if total else 0.0,
        "avg_return_pct": round(sum(valid_returns) / len(valid_returns), 2) if valid_returns else 0.0,
        "since_days": since_days,
        "triggered_exit_count": triggered_exit_count,
        "recent_n": recent_n,
        "recent_win_count": recent_win_count,
        "recent_win_rate_pct": recent_win_rate_pct,
        "best_return_pct": round(best_return_pct, 2) if best_return_pct is not None else None,
        "total_1w": total_1w,
        "win_count_1w": win_count_1w,
        "win_rate_1w_pct": round(win_count_1w / total_1w * 100, 1) if total_1w else 0.0,
        "avg_return_1w_pct": round(sum(returns_1w) / len(returns_1w), 2) if returns_1w else 0.0,
        "total_1m": total_1m,
        "win_count_1m": win_count_1m,
        "win_rate_1m_pct": round(win_count_1m / total_1m * 100, 1) if total_1m else 0.0,
        "avg_return_1m_pct": round(sum(returns_1m) / len(returns_1m), 2) if returns_1m else 0.0,
        "total_2m": total_2m,
        "win_count_2m": win_count_2m,
        "win_rate_2m_pct": round(win_count_2m / total_2m * 100, 1) if total_2m else 0.0,
        "avg_return_2m_pct": round(sum(returns_2m) / len(returns_2m), 2) if returns_2m else 0.0,
        "total_3m": total_3m,
        "win_count_3m": win_count_3m,
        "win_rate_3m_pct": round(win_count_3m / total_3m * 100, 1) if total_3m else 0.0,
        "avg_return_3m_pct": round(sum(returns_3m) / len(returns_3m), 2) if returns_3m else 0.0,
        "benchmark_avg_us_pct": round(sum(bench_returns_us) / len(bench_returns_us), 2) if bench_returns_us else None,
        "benchmark_avg_cn_pct": round(sum(bench_returns_cn) / len(bench_returns_cn), 2) if bench_returns_cn else None,
        "benchmark_avg_hk_pct": round(sum(bench_returns_hk) / len(bench_returns_hk), 2) if bench_returns_hk else None,
        "benchmark_avg_hstech_pct": round(sum(bench_returns_hstech) / len(bench_returns_hstech), 2) if bench_returns_hstech else None,
        "worst_return_pct": round(worst_return_pct, 2) if worst_return_pct is not None else None,
        "dist_up_10": dist_up_10,
        "dist_0_10": dist_0_10,
        "dist_neg10_0": dist_neg10_0,
        "dist_down_10": dist_down_10,
    }
    return rows, summary
