# -*- coding: utf-8 -*-
"""
本地只读查库：仅 env + db_type + sql + limit；结果 JSON 字符串。

- sql：完整 SELECT，**schema 写在语句里**（如 sg_seabank_ekyc_center_db.retail_application_tab）
- limit：若 sql 末尾尚无 LIMIT，自动追加 `LIMIT n`（上限 MAX_LIMIT）
- 连接：MySQL 用 **主机名 + 端口**，不是 HTTP。IP 不通时可用内网域名（见下方 HOST_OVERRIDES / 环境变量 EKYC_DB_HOST）

账号密码：LOCAL_DB_CREDENTIALS（仅 nonlive，勿提交）

依赖: pip install pymysql

main(input) 键名（SMAR）: env, db_type, sql, limit

输出: result_json, row_count, error_message
"""

from __future__ import annotations

import argparse
import json
import os
import re
from typing import Any, Dict, List, Tuple

# ---------------------------------------------------------------------------
# 与 npt-sg/mcp databaseTools.ts 一致的主机表（不含密码）
# ---------------------------------------------------------------------------

# IP 直连不通时：在下面 HOST_OVERRIDES 填域名（不要 http://），或 export EKYC_DB_HOST=域名 全局覆盖
ENV_HOSTS: Dict[str, Dict[str, str]] = {
    "dev1": {"ibank": "10.213.17.145", "coreBank": "10.213.17.145"},
    "dev2": {"ibank": "10.213.17.95", "coreBank": "10.213.17.95"},
    "sit1": {"ibank": "10.213.17.208", "coreBank": "10.213.17.74"},
    "sit2": {"ibank": "10.213.17.3", "coreBank": "10.213.17.2"},
    "sit3": {"ibank": "10.213.16.106", "coreBank": "10.213.16.106"},
    "uat1": {"ibank": "10.213.16.76", "coreBank": "10.213.16.134"},
    "uat2": {"ibank": "10.213.16.88", "coreBank": "10.213.16.183"},
    "uat3": {"ibank": "10.213.16.167", "coreBank": "10.213.16.216"},
}

# 非空则替代 ENV_HOSTS 里对应 host；coreBank 留空则继续用 IP
HOST_OVERRIDES: Dict[str, Dict[str, str]] = {
    "dev1": {
        "ibank": "ibank-master-00-6606-dev1.db.sg.seabanksvc.com",
        "coreBank": "",
    },
}

DEFAULT_MYSQL_PORT = 6606
DEFAULT_LIMIT = 100
MAX_LIMIT = 5000
AVAILABLE_ENVS = list(ENV_HOSTS.keys())
AVAILABLE_DB_TYPES = ("ibank", "coreBank")

_SELECT_RE = re.compile(
    r"^\s*select\b",
    flags=re.IGNORECASE | re.DOTALL,
)

_LIMIT_TAIL_RE = re.compile(
    r"\blimit\s+\d+\s*$",
    flags=re.IGNORECASE | re.DOTALL,
)

# 仅本地 nonlive 使用，勿提交仓库
LOCAL_DB_CREDENTIALS: Dict[str, Dict[str, str]] = {
    "dev1": {"user": "bank_test", "password": "asd!Qwe123"},
    "dev2": {"user": "bank_test", "password": "asd!Qwe123"},
    "sit1": {"user": "npt_dml", "password": "tLk6M_hwywGGEGIpsYCy"},
    "sit2": {"user": "npt_dml", "password": "tLk6M_hwywGGEGIpsYCy"},
    "sit3": {"user": "npt_dml", "password": "tLk6M_hwywGGEGIpsYCy"},
    "uat1": {"user": "npt_dml", "password": "tLk6M_hwywGGEGIpsYCy"},
    "uat2": {"user": "npt_dml", "password": "tLk6M_hwywGGEGIpsYCy"},
    "uat3": {"user": "npt_dml", "password": "tLk6M_hwywGGEGIpsYCy"},
}


def _normalize_db_type(db_type: str) -> str:
    s = (db_type or "ibank").strip()
    low = s.lower()
    if low == "ibank":
        return "ibank"
    if low in ("corebank", "core_bank"):
        return "coreBank"
    if s in AVAILABLE_DB_TYPES:
        return s
    raise ValueError(
        f'db_type 无效: {db_type!r}，应为 ibank 或 coreBank（可写 corebank）'
    )


def _credentials_for_env(env: str) -> Tuple[str, str]:
    cred = LOCAL_DB_CREDENTIALS.get(env)
    if not cred:
        raise RuntimeError(f"LOCAL_DB_CREDENTIALS 中未配置环境: {env}")
    u = (cred.get("user") or "").strip()
    if not u:
        raise RuntimeError(f"LOCAL_DB_CREDENTIALS[{env}] 缺少 user")
    return u, str(cred.get("password", ""))


def _resolve_mysql_host(env: str, db_type_n: str) -> str:
    """优先 EKYC_DB_HOST；其次 HOST_OVERRIDES；否则 ENV_HOSTS IP。"""
    global_host = (os.environ.get("EKYC_DB_HOST") or "").strip()
    if global_host:
        return global_host
    alt = (HOST_OVERRIDES.get(env) or {}).get(db_type_n) or ""
    if alt.strip():
        return alt.strip()
    return ENV_HOSTS[env][db_type_n]


def build_connection_params(env: str, db_type: str) -> Dict[str, Any]:
    env = (env or "dev1").strip()
    if env not in ENV_HOSTS:
        raise ValueError(
            f'环境 "{env}" 不存在。可用: {", ".join(AVAILABLE_ENVS)}'
        )

    db_type_n = _normalize_db_type(db_type)
    db_user, db_password = _credentials_for_env(env)
    host = _resolve_mysql_host(env, db_type_n)

    return {
        "host": host,
        "port": DEFAULT_MYSQL_PORT,
        "user": db_user,
        "password": db_password,
        "database": None,
        "connect_timeout": 10,
        "charset": "utf8mb4",
    }


def _ensure_read_only_sql(sql: str) -> str:
    s = (sql or "").strip()
    if not s:
        raise ValueError("sql 不能为空")
    if not _SELECT_RE.match(s):
        raise ValueError("仅允许 SELECT 查询")
    if ";" in s.rstrip(";"):
        raise ValueError("请勿在单条语句中包含多个分号")
    return s


def _apply_limit(sql: str, limit: int) -> str:
    """若末尾无 LIMIT，则追加 LIMIT n。"""
    lim = max(1, min(int(limit), MAX_LIMIT))
    s = sql.strip().rstrip(";")
    if _LIMIT_TAIL_RE.search(s):
        return s
    return f"{s} LIMIT {lim}"


def _rows_to_serializable(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        item: Dict[str, Any] = {}
        for k, v in row.items():
            if hasattr(v, "isoformat"):
                item[k] = v.isoformat()
            elif isinstance(v, (bytes, bytearray)):
                item[k] = v.decode("utf-8", errors="replace")
            else:
                item[k] = v
        out.append(item)
    return out


def run_select(
    env: str,
    db_type: str,
    sql: str,
    limit: int,
) -> List[Dict[str, Any]]:
    base = _ensure_read_only_sql(sql)
    final_sql = _apply_limit(base, limit)

    params_conn = build_connection_params(env, db_type)
    try:
        import pymysql
        from pymysql.cursors import DictCursor
    except ImportError as e:
        raise RuntimeError("需要安装 pymysql: pip install pymysql") from e

    conn = pymysql.connect(cursorclass=DictCursor, **params_conn)
    try:
        with conn.cursor() as cur:
            cur.execute(final_sql)
            rows = list(cur.fetchall())
    finally:
        conn.close()
    return _rows_to_serializable(rows)


def main(input: Dict[str, Any]) -> Dict[str, Any]:
    """
    输入: env, db_type, sql, limit（库名写在 sql 内）
    输出: result_json, row_count, error_message
    """
    env = (input.get("env") or "dev1").strip()
    db_type = (input.get("db_type") or "ibank").strip()
    sql = (input.get("sql") or "").strip()

    try:
        limit = int(input.get("limit", DEFAULT_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT

    if not sql:
        return {
            "result_json": "[]",
            "row_count": 0,
            "error_message": "请传入 sql（SELECT 语句）。",
        }

    try:
        rows = run_select(env, db_type, sql, limit)
        return {
            "result_json": json.dumps(rows, ensure_ascii=False),
            "row_count": len(rows),
            "error_message": "",
        }
    except Exception as e:
        return {
            "result_json": "[]",
            "row_count": 0,
            "error_message": str(e),
        }


def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="只读查库：env + db_type + sql + limit；输出 result_json（JSON 数组字符串）"
    )
    parser.add_argument("--env", default="dev1", help="dev1/dev2/sit1/...")
    parser.add_argument(
        "--db_type",
        default="ibank",
        help="ibank 或 corebank/coreBank",
    )
    parser.add_argument(
        "--sql",
        "-s",
        required=True,
        help="SELECT 语句（外部传入）",
    )
    parser.add_argument(
        "--limit",
        "-l",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"LIMIT 上限，默认 {DEFAULT_LIMIT}，最大 {MAX_LIMIT}；sql 末尾已有 LIMIT 则不改写",
    )
    args = parser.parse_args()

    inp: Dict[str, Any] = {
        "env": args.env,
        "db_type": args.db_type,
        "sql": args.sql,
        "limit": args.limit,
    }
    out = main(inp)
    if out.get("error_message"):
        print(json.dumps({"error_message": out["error_message"]}, ensure_ascii=False), flush=True)
        raise SystemExit(1)
    print(out["result_json"], flush=True)


if __name__ == "__main__":
    _cli()
