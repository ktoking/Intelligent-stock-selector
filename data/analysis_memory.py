"""
Persistent analysis memory for stock-agent.

The local SQLite database is the source of truth. Mem0 sync is optional and
best-effort: failures are recorded but never block analysis or report calls.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from config.yf_suppress import suppress_yf_noise

suppress_yf_noise()

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_MEMORY_DIR = _PROJECT_ROOT / "data" / "memory"
_DEFAULT_DB_PATH = _DEFAULT_MEMORY_DIR / "analysis_memory.sqlite"
_DEFAULT_HORIZONS = (1, 3, 5, 10, 20)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _db_path() -> Path:
    raw = os.environ.get("STOCK_AGENT_ANALYSIS_MEMORY_DB", "").strip()
    path = Path(raw) if raw else _DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS report_runs (
            report_job_id TEXT PRIMARY KEY,
            generated_at TEXT NOT NULL,
            title TEXT,
            market TEXT,
            pool TEXT,
            deep INTEGER,
            interval TEXT,
            prepost INTEGER,
            report_path TEXT,
            card_count INTEGER,
            top_picks_json TEXT,
            action_counts_json TEXT,
            summary_text TEXT,
            snapshot_json TEXT,
            request_json TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS analysis_runs (
            run_id TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            name TEXT,
            market TEXT,
            analyzed_at TEXT NOT NULL,
            source TEXT NOT NULL,
            mode TEXT,
            interval TEXT,
            prepost INTEGER,
            score REAL,
            action TEXT,
            confidence REAL,
            price_at_analysis REAL,
            price_raw TEXT,
            summary TEXT,
            score_reason TEXT,
            analysis_reason TEXT,
            bullish_reasons_json TEXT,
            risks_json TEXT,
            suggestions_json TEXT,
            card_json TEXT,
            brief_json TEXT,
            request_json TEXT,
            report_job_id TEXT,
            report_path TEXT,
            mem0_status TEXT NOT NULL DEFAULT 'pending',
            mem0_memory_id TEXT,
            mem0_error TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(report_job_id) REFERENCES report_runs(report_job_id)
        );

        CREATE TABLE IF NOT EXISTS analysis_outcomes (
            run_id TEXT NOT NULL,
            horizon_days INTEGER NOT NULL,
            target_date TEXT NOT NULL,
            price_at_horizon REAL,
            return_pct REAL,
            is_win INTEGER,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (run_id, horizon_days),
            FOREIGN KEY(run_id) REFERENCES analysis_runs(run_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_analysis_runs_ticker_time
            ON analysis_runs(ticker, analyzed_at DESC);
        CREATE INDEX IF NOT EXISTS idx_analysis_runs_report
            ON analysis_runs(report_job_id);
        CREATE INDEX IF NOT EXISTS idx_analysis_runs_mem0
            ON analysis_runs(mem0_status);
        """
    )
    conn.commit()


def _json_dumps(value: Any) -> str:
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return json.dumps(str(value), ensure_ascii=False)


def _json_loads(value: Any, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _float_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except Exception:
            return None
    text = str(value).strip().replace(",", "")
    if not text or text in {"—", "-", "None", "null", "nan"}:
        return None
    for prefix in ("$", "¥", "HK$"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    try:
        return float(text)
    except Exception:
        return None


def _int_bool(value: Any) -> int:
    if isinstance(value, str):
        return 1 if value.strip().lower() in {"1", "true", "yes", "y"} else 0
    return 1 if bool(value) else 0


def _pick_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text and text != "—":
            return text
    return ""


def _list_from_value(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item or "").strip()]
    text = str(value).strip()
    return [text] if text and text != "—" else []


def _dedupe(items: Iterable[Any], max_items: int = 6) -> List[str]:
    result: List[str] = []
    seen = set()
    for item in items:
        text = str(item or "").strip()
        if not text or text == "—":
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= max_items:
            break
    return result


def _extract_card(card: Dict[str, Any], brief: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    brief = brief or {}
    price_raw = _pick_non_empty(card.get("current_price"), card.get("price"), card.get("last_price"))
    score = _float_or_none(card.get("score"))
    reasons = _dedupe(
        _list_from_value(brief.get("reasons"))
        + [
            card.get("core_conclusion"),
            card.get("analysis_reason"),
            card.get("trend_structure"),
            card.get("score_reason"),
        ],
        max_items=8,
    )
    risks = _dedupe(
        _list_from_value(brief.get("risks"))
        + [
            card.get("tech_exit_note"),
            card.get("data_quality_note"),
            card.get("macd_status"),
            card.get("kdj_status"),
        ],
        max_items=8,
    )
    suggestions = _dedupe(
        _list_from_value(brief.get("suggestions"))
        + [
            card.get("tech_entry_note"),
            f"加仓参考：{card.get('add_price')}" if card.get("add_price") and card.get("add_price") != "—" else "",
            f"减仓参考：{card.get('reduce_price')}" if card.get("reduce_price") and card.get("reduce_price") != "—" else "",
        ],
        max_items=8,
    )
    return {
        "ticker": str(card.get("ticker") or "").upper().strip(),
        "name": str(card.get("name") or card.get("ticker") or "").strip(),
        "market": str(card.get("market") or "").strip(),
        "score": score,
        "action": str(card.get("action") or "观察").strip() or "观察",
        "price_raw": price_raw,
        "price_at_analysis": _float_or_none(price_raw),
        "summary": _pick_non_empty(
            brief.get("summary_text"),
            card.get("core_conclusion"),
            card.get("analysis_reason"),
        ),
        "score_reason": _pick_non_empty(card.get("score_reason")),
        "analysis_reason": _pick_non_empty(card.get("analysis_reason")),
        "bullish_reasons": reasons,
        "risks": risks,
        "suggestions": suggestions,
        "interval": str(card.get("interval") or card.get("interval_label") or "").strip(),
        "prepost": _int_bool(card.get("prepost")),
    }


def _load_mem0_api_key() -> str:
    key = os.environ.get("MEM0_API_KEY", "").strip()
    if key:
        return key
    try:
        from config.env_loader import load_env

        load_env()
    except Exception:
        pass
    key = os.environ.get("MEM0_API_KEY", "").strip()
    if key:
        return key
    hermes_config = Path.home() / ".hermes" / "mem0.json"
    try:
        if hermes_config.exists():
            data = json.loads(hermes_config.read_text(encoding="utf-8"))
            return str(data.get("api_key") or data.get("apiKey") or "").strip()
    except Exception:
        return ""
    return ""


def build_mem0_summary(record: Dict[str, Any]) -> str:
    ticker = record.get("ticker") or ""
    name = record.get("name") or ticker
    score = record.get("score")
    score_text = f"{score:.1f}/10" if isinstance(score, (int, float)) else (str(score) if score else "N/A")
    action = record.get("action") or "观察"
    price = record.get("price_at_analysis") or record.get("price_raw") or "N/A"
    reasons = "；".join((record.get("bullish_reasons") or [])[:3]) or record.get("summary") or "无明确理由"
    risks = "；".join((record.get("risks") or [])[:3]) or "未记录额外风险"
    suggestions = "；".join((record.get("suggestions") or [])[:2]) or "暂无操作建议"
    return (
        f"stock-agent 分析记录 {record.get('analyzed_at')}: {name}({ticker}) "
        f"评分 {score_text}，动作 {action}，分析价 {price}。"
        f"理由：{reasons}。风险：{risks}。建议：{suggestions}。"
        f"run_id={record.get('run_id')} source={record.get('source')}。"
    )


def _extract_mem0_id(result: Any) -> str:
    if isinstance(result, dict):
        for key in ("id", "memory_id", "memoryId"):
            if result.get(key):
                return str(result[key])
        memories = result.get("results") or result.get("memories")
        if isinstance(memories, list) and memories:
            return _extract_mem0_id(memories[0])
    if isinstance(result, list) and result:
        return _extract_mem0_id(result[0])
    return ""


def _sync_mem0(record: Dict[str, Any]) -> Dict[str, str]:
    if os.environ.get("STOCK_AGENT_MEM0_SYNC", "1").strip().lower() in {"0", "false", "no"}:
        return {"status": "skipped", "memory_id": "", "error": "disabled by STOCK_AGENT_MEM0_SYNC"}
    api_key = _load_mem0_api_key()
    if not api_key:
        return {"status": "skipped", "memory_id": "", "error": "MEM0_API_KEY not configured"}
    try:
        from mem0 import MemoryClient
    except Exception as exc:
        return {"status": "skipped", "memory_id": "", "error": f"mem0ai not installed: {exc}"}
    try:
        client = MemoryClient(api_key=api_key)
        user_id = os.environ.get("STOCK_AGENT_MEM0_USER_ID", "kaiyi-wang")
        agent_id = os.environ.get("STOCK_AGENT_MEM0_AGENT_ID", "stock-agent")
        messages = [{"role": "user", "content": build_mem0_summary(record)}]
        result = client.add(messages, user_id=user_id, agent_id=agent_id, infer=False)
        return {"status": "synced", "memory_id": _extract_mem0_id(result), "error": ""}
    except Exception as exc:
        return {"status": "failed", "memory_id": "", "error": str(exc)[:1000]}


def _update_mem0_status(conn: sqlite3.Connection, run_id: str, sync_result: Dict[str, str]) -> None:
    conn.execute(
        """
        UPDATE analysis_runs
        SET mem0_status = ?, mem0_memory_id = ?, mem0_error = ?
        WHERE run_id = ?
        """,
        (
            sync_result.get("status") or "failed",
            sync_result.get("memory_id") or "",
            sync_result.get("error") or "",
            run_id,
        ),
    )
    conn.commit()


def record_analysis_run(
    card: Dict[str, Any],
    *,
    brief: Optional[Dict[str, Any]] = None,
    source: str = "agent_analyze",
    mode: str = "",
    request: Optional[Dict[str, Any]] = None,
    report_job_id: str = "",
    report_path: str = "",
    sync_mem0: bool = True,
) -> Dict[str, Any]:
    """Persist a single analysis card and optionally sync a compact Mem0 summary."""
    if not isinstance(card, dict):
        return {"recorded": False, "reason": "card is not a dict"}
    extracted = _extract_card(card, brief=brief)
    ticker = extracted["ticker"]
    if not ticker:
        return {"recorded": False, "reason": "ticker is empty"}
    run_id = uuid.uuid4().hex
    analyzed_at = _now_iso()
    created_at = analyzed_at
    record = {
        "run_id": run_id,
        "analyzed_at": analyzed_at,
        "source": source,
        "mode": mode,
        "report_job_id": report_job_id or "",
        "report_path": report_path,
        **extracted,
    }
    report_job_id_or_none = report_job_id or None
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO analysis_runs (
                run_id, ticker, name, market, analyzed_at, source, mode, interval, prepost,
                score, action, confidence, price_at_analysis, price_raw, summary,
                score_reason, analysis_reason, bullish_reasons_json, risks_json,
                suggestions_json, card_json, brief_json, request_json, report_job_id,
                report_path, mem0_status, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                extracted["ticker"],
                extracted["name"],
                extracted["market"],
                analyzed_at,
                source,
                mode,
                extracted["interval"],
                extracted["prepost"],
                extracted["score"],
                extracted["action"],
                None,
                extracted["price_at_analysis"],
                extracted["price_raw"],
                extracted["summary"],
                extracted["score_reason"],
                extracted["analysis_reason"],
                _json_dumps(extracted["bullish_reasons"]),
                _json_dumps(extracted["risks"]),
                _json_dumps(extracted["suggestions"]),
                _json_dumps(card),
                _json_dumps(brief),
                _json_dumps(request),
                report_job_id_or_none,
                report_path,
                "pending" if sync_mem0 else "skipped",
                created_at,
            ),
        )
        conn.commit()
        if sync_mem0:
            sync_result = _sync_mem0(record)
            _update_mem0_status(conn, run_id, sync_result)
        else:
            sync_result = {"status": "skipped", "memory_id": "", "error": "sync disabled for call"}
        return {
            "recorded": True,
            "run_id": run_id,
            "db_path": str(_db_path()),
            "mem0_status": sync_result.get("status"),
            "mem0_memory_id": sync_result.get("memory_id") or "",
            "mem0_error": sync_result.get("error") or "",
        }
    finally:
        conn.close()


def record_report_run(
    snapshot: Dict[str, Any],
    cards: Sequence[Dict[str, Any]],
    *,
    request: Optional[Dict[str, Any]] = None,
    sync_mem0: bool = True,
) -> List[Dict[str, Any]]:
    """Persist a report snapshot plus one analysis run per report card."""
    if not isinstance(snapshot, dict):
        snapshot = {}
    report_job_id = str(snapshot.get("job_id") or uuid.uuid4().hex)
    generated_at = str(snapshot.get("generated_at") or _now_iso())
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO report_runs (
                report_job_id, generated_at, title, market, pool, deep, interval, prepost,
                report_path, card_count, top_picks_json, action_counts_json,
                summary_text, snapshot_json, request_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report_job_id,
                generated_at,
                snapshot.get("title") or "",
                snapshot.get("market") or "",
                snapshot.get("pool") or "",
                int(snapshot.get("deep") or 0),
                snapshot.get("interval") or "",
                int(snapshot.get("prepost") or 0),
                snapshot.get("report_path") or "",
                int(snapshot.get("card_count") or len(cards or [])),
                _json_dumps(snapshot.get("top_picks") or []),
                _json_dumps(snapshot.get("action_counts") or {}),
                snapshot.get("summary_text") or "",
                _json_dumps(snapshot),
                _json_dumps(request),
                _now_iso(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    cards = list(cards or [])
    max_mem0_cards = max(0, int(os.environ.get("STOCK_AGENT_MEM0_REPORT_MAX", "10") or 10))
    mem0_min_score = _float_or_none(os.environ.get("STOCK_AGENT_MEM0_REPORT_MIN_SCORE", "8.5"))
    scored_cards = sorted(
        cards,
        key=lambda c: _float_or_none(c.get("score")) or 0,
        reverse=True,
    )
    selected_mem0_tickers: List[str] = []
    for card in scored_cards:
        ticker = str(card.get("ticker") or "").upper().strip()
        if not ticker or ticker in selected_mem0_tickers:
            continue
        should_sync = (
            (mem0_min_score is not None and (_float_or_none(card.get("score")) or 0) >= mem0_min_score)
            or str(card.get("action") or "").strip() == "买入"
        )
        if should_sync:
            selected_mem0_tickers.append(ticker)
        if len(selected_mem0_tickers) >= max_mem0_cards:
            break
    selected_mem0_tickers_set = set(selected_mem0_tickers)

    results: List[Dict[str, Any]] = []
    for card in cards:
        ticker = str(card.get("ticker") or "").upper().strip()
        result = record_analysis_run(
            card,
            source="report",
            mode="deep" if int(snapshot.get("deep") or 0) == 1 else "basic",
            request=request,
            report_job_id=report_job_id,
            report_path=str(snapshot.get("report_path") or ""),
            sync_mem0=bool(sync_mem0 and ticker in selected_mem0_tickers_set),
        )
        results.append(result)
    return results


def _row_to_record(row: sqlite3.Row, include_payload: bool = False) -> Dict[str, Any]:
    record = {
        "run_id": row["run_id"],
        "ticker": row["ticker"],
        "name": row["name"],
        "market": row["market"],
        "analyzed_at": row["analyzed_at"],
        "source": row["source"],
        "mode": row["mode"],
        "interval": row["interval"],
        "prepost": bool(row["prepost"]),
        "score": row["score"],
        "action": row["action"],
        "price_at_analysis": row["price_at_analysis"],
        "price_raw": row["price_raw"],
        "summary": row["summary"],
        "score_reason": row["score_reason"],
        "analysis_reason": row["analysis_reason"],
        "bullish_reasons": _json_loads(row["bullish_reasons_json"], []),
        "risks": _json_loads(row["risks_json"], []),
        "suggestions": _json_loads(row["suggestions_json"], []),
        "report_job_id": row["report_job_id"],
        "report_path": row["report_path"],
        "mem0_status": row["mem0_status"],
        "mem0_memory_id": row["mem0_memory_id"],
        "mem0_error": row["mem0_error"],
        "created_at": row["created_at"],
    }
    if include_payload:
        record["card"] = _json_loads(row["card_json"], {})
        record["brief"] = _json_loads(row["brief_json"], {})
        record["request"] = _json_loads(row["request_json"], {})
    return record


def get_recent_analysis(
    ticker: str = "",
    *,
    limit: int = 20,
    include_payload: bool = False,
) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit or 20), 200))
    ticker_norm = (ticker or "").upper().strip()
    conn = _connect()
    try:
        if ticker_norm:
            rows = conn.execute(
                """
                SELECT * FROM analysis_runs
                WHERE ticker = ?
                ORDER BY analyzed_at DESC
                LIMIT ?
                """,
                (ticker_norm, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM analysis_runs
                ORDER BY analyzed_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_row_to_record(row, include_payload=include_payload) for row in rows]
    finally:
        conn.close()


def _parse_date(value: Any) -> Optional[date]:
    text = str(value or "").strip()
    if not text:
        return None
    for candidate in (text[:10], text):
        try:
            return datetime.fromisoformat(candidate).date()
        except Exception:
            pass
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _close_on_or_after(hist: Any, target_date: date) -> Optional[float]:
    if hist is None or getattr(hist, "empty", True) or "Close" not in hist.columns:
        return None
    try:
        for ts, row in hist.iterrows():
            try:
                row_date = ts.date() if hasattr(ts, "date") else ts
            except Exception:
                row_date = ts
            if row_date >= target_date:
                return float(row["Close"])
    except Exception:
        return None
    return None


def _is_win_for_action(action: str, return_pct: Optional[float]) -> Optional[int]:
    if return_pct is None:
        return None
    action = (action or "").strip()
    if action == "买入":
        return 1 if return_pct > 0 else 0
    if action in {"卖出", "离场", "减仓"}:
        return 1 if return_pct < 0 else 0
    return 1 if return_pct > 0 else 0


def update_analysis_outcomes(
    *,
    horizons: Sequence[int] = _DEFAULT_HORIZONS,
    max_runs: int = 200,
) -> Dict[str, Any]:
    """Backfill future-return outcomes for stored analysis runs."""
    try:
        import logging

        logging.getLogger("yfinance").setLevel(logging.ERROR)
        import yfinance as yf
        from utils.yf_retry import fetch_with_retry
    except Exception as exc:
        return {"updated": 0, "skipped": 0, "errors": [f"yfinance unavailable: {exc}"]}

    horizons = tuple(sorted({int(h) for h in horizons if int(h) > 0}))
    if not horizons:
        horizons = _DEFAULT_HORIZONS
    max_horizon = max(horizons)
    today = datetime.now().date()
    conn = _connect()
    updated = 0
    skipped = 0
    errors: List[str] = []
    try:
        rows = conn.execute(
            """
            SELECT run_id, ticker, analyzed_at, action, price_at_analysis
            FROM analysis_runs
            WHERE price_at_analysis IS NOT NULL AND price_at_analysis > 0
            ORDER BY analyzed_at DESC
            LIMIT ?
            """,
            (max(1, min(int(max_runs or 200), 2000)),),
        ).fetchall()
        for row in rows:
            analyzed_date = _parse_date(row["analyzed_at"])
            price_then = _float_or_none(row["price_at_analysis"])
            ticker = str(row["ticker"] or "").upper().strip()
            if not analyzed_date or not price_then or not ticker:
                skipped += 1
                continue
            eligible_horizons = [h for h in horizons if analyzed_date + timedelta(days=h) <= today]
            if not eligible_horizons:
                skipped += 1
                continue
            start = analyzed_date
            end = min(today + timedelta(days=1), analyzed_date + timedelta(days=max_horizon + 10))
            try:
                hist = fetch_with_retry(
                    lambda: yf.Ticker(ticker).history(start=start, end=end),
                    ticker=ticker,
                    op_name=f"analysis_outcome_history(start={start},end={end})",
                )
            except Exception as exc:
                errors.append(f"{ticker}: {str(exc)[:160]}")
                continue
            if hist is None or hist.empty:
                skipped += 1
                continue
            for horizon in eligible_horizons:
                target = analyzed_date + timedelta(days=horizon)
                price_at_horizon = _close_on_or_after(hist, target)
                if not price_at_horizon:
                    skipped += 1
                    continue
                return_pct = round((price_at_horizon - price_then) / price_then * 100, 2)
                is_win = _is_win_for_action(row["action"], return_pct)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO analysis_outcomes (
                        run_id, horizon_days, target_date, price_at_horizon,
                        return_pct, is_win, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["run_id"],
                        horizon,
                        target.isoformat(),
                        price_at_horizon,
                        return_pct,
                        is_win,
                        _now_iso(),
                    ),
                )
                updated += 1
            conn.commit()
        return {"updated": updated, "skipped": skipped, "errors": errors[:20], "horizons": list(horizons)}
    finally:
        conn.close()


def get_outcome_summary(ticker: str = "", *, since_days: int = 180) -> Dict[str, Any]:
    ticker_norm = (ticker or "").upper().strip()
    cutoff = (datetime.now() - timedelta(days=max(1, since_days))).isoformat(timespec="seconds")
    params: List[Any] = [cutoff]
    where = "WHERE ar.analyzed_at >= ?"
    if ticker_norm:
        where += " AND ar.ticker = ?"
        params.append(ticker_norm)
    conn = _connect()
    try:
        rows = conn.execute(
            f"""
            SELECT ao.horizon_days, ao.return_pct, ao.is_win, ar.action
            FROM analysis_outcomes ao
            JOIN analysis_runs ar ON ar.run_id = ao.run_id
            {where}
            """,
            params,
        ).fetchall()
    finally:
        conn.close()
    by_horizon: Dict[int, List[sqlite3.Row]] = {}
    for row in rows:
        by_horizon.setdefault(int(row["horizon_days"]), []).append(row)
    summary: Dict[str, Any] = {
        "ticker": ticker_norm,
        "since_days": since_days,
        "total_outcomes": len(rows),
        "horizons": {},
    }
    for horizon, horizon_rows in sorted(by_horizon.items()):
        returns = [float(r["return_pct"]) for r in horizon_rows if r["return_pct"] is not None]
        wins = [int(r["is_win"]) for r in horizon_rows if r["is_win"] is not None]
        summary["horizons"][str(horizon)] = {
            "count": len(horizon_rows),
            "win_rate_pct": round(sum(wins) / len(wins) * 100, 1) if wins else None,
            "avg_return_pct": round(sum(returns) / len(returns), 2) if returns else None,
            "best_return_pct": round(max(returns), 2) if returns else None,
            "worst_return_pct": round(min(returns), 2) if returns else None,
        }
    return summary
