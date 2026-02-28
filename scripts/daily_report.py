#!/usr/bin/env python3
"""
每日定时跑三份报告：SP500 100只、A股中证2000 100只、港股恒指 100只。
deep=0（不跑深度分析），报告保存到 report/output/。
周末及中国法定节假日自动跳过（可 --force 强制执行）。

用法：
  python scripts/daily_report.py [--base-url URL]
  # 默认请求 http://127.0.0.1:8000，需先启动 server.py

定时（crontab -e）：
  0 9 * * * cd /path/to/stock-agent && python scripts/daily_report.py
  # 每天早上 9 点执行，周末/节假日自动跳过
"""
import argparse
import sys
from datetime import datetime

try:
    import requests
except ImportError:
    print("请安装: pip install requests", file=sys.stderr)
    sys.exit(1)


def _should_skip_today(force: bool) -> bool:
    """周末或中国法定节假日则跳过。force=True 时强制执行。"""
    if force:
        return False
    today = datetime.now().date()
    # 周末：周六 5、周日 6
    if today.weekday() >= 5:
        return True
    # 中国法定节假日（需 pip install chinese_calendar）
    try:
        import chinese_calendar as cc
        if cc.is_holiday(today):
            return True
    except ImportError:
        pass
    return False

JOBS = [
    {"market": "us", "pool": "sp500", "limit": 100, "label": "美股SP500"},
    {"market": "cn", "pool": "csi2000", "limit": 100, "label": "A股中证2000"},
    {"market": "hk", "pool": "hsi", "limit": 100, "label": "港股恒指"},
]


def main():
    parser = argparse.ArgumentParser(description="每日三份报告")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="服务地址")
    parser.add_argument("--timeout", type=int, default=7200, help="单份报告超时秒数，默认2小时")
    parser.add_argument("--test", action="store_true", help="测试模式：每份只跑 2 只")
    parser.add_argument("--force", action="store_true", help="强制执行，忽略周末/节假日")
    args = parser.parse_args()

    if _should_skip_today(args.force):
        print("今日为周末或节假日，跳过执行。使用 --force 可强制运行。", flush=True)
        sys.exit(0)

    base = args.base_url.rstrip("/")
    limit = 2 if args.test else 100

    for i, job in enumerate(JOBS):
        label = job.get("label", "")
        params = {k: v for k, v in job.items() if k != "label"}
        params.update(limit=limit, deep=0, save_output=1)
        print(f"[{i + 1}/3] 开始: {label} (market={params['market']} pool={params['pool']} limit={params['limit']})", flush=True)
        try:
            r = requests.get(f"{base}/report", params=params, timeout=args.timeout)
            if r.status_code != 200:
                print(f"  失败: HTTP {r.status_code}", flush=True)
                sys.exit(1)
            print(f"  完成", flush=True)
        except requests.exceptions.ConnectionError:
            print("  失败: 无法连接服务，请先启动 python server.py", flush=True)
            sys.exit(1)
        except requests.exceptions.Timeout:
            print("  失败: 超时", flush=True)
            sys.exit(1)
        except Exception as e:
            print(f"  失败: {e}", flush=True)
            sys.exit(1)

    print("全部完成", flush=True)


if __name__ == "__main__":
    main()
