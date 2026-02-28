#!/usr/bin/env python3
"""
回测诊断脚本：分市场、分持有期、分基准环境统计胜率与收益，清晰看到「哪里在变差」。

用法：
  python scripts/diagnose_backtest.py [--since-days 90]
  python scripts/diagnose_backtest.py --since-days 60 --verbose

输出维度：
  - 分市场：美股 / A股 / 港股 各自胜率、平均收益
  - 分持有期：1周 / 1月 / 2月 / 3月 胜率与收益
  - 分基准环境：牛市（基准涨≥5%）/ 熊市（基准跌≥5%）/ 震荡 下的表现
  - 近期 vs 整体：最近 10 条 vs 全部，便于发现近期是否变差
"""
import argparse
import sys
from pathlib import Path
from collections import defaultdict

# 确保项目根目录在 path 中
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from config.analysis_config import (
    DIAGNOSE_SINCE_DAYS,
    DIAGNOSE_BULL_THRESHOLD_PCT,
    DIAGNOSE_BEAR_THRESHOLD_PCT,
)


def _win_rate(returns: list) -> tuple:
    """(胜率%, 平均收益%, 样本数)"""
    valid = [x for x in returns if x is not None]
    if not valid:
        return 0.0, 0.0, 0
    win = sum(1 for x in valid if x > 0)
    return round(win / len(valid) * 100, 1), round(sum(valid) / len(valid), 2), len(valid)


def _env_label(bench_pct: float) -> str:
    """根据基准同期涨跌幅判定环境。"""
    if bench_pct is None:
        return "未知"
    if bench_pct >= DIAGNOSE_BULL_THRESHOLD_PCT:
        return "牛市"
    if bench_pct <= DIAGNOSE_BEAR_THRESHOLD_PCT:
        return "熊市"
    return "震荡"


def main():
    parser = argparse.ArgumentParser(description="回测诊断：分维度胜率与收益")
    parser.add_argument("--since-days", type=int, default=DIAGNOSE_SINCE_DAYS, help="回溯天数")
    parser.add_argument("--verbose", "-v", action="store_true", help="输出每条推荐明细")
    args = parser.parse_args()

    try:
        from data.recommendations import get_past_recommendations_with_returns
    except ImportError as e:
        print(f"导入失败: {e}", file=sys.stderr)
        sys.exit(1)

    rows, summary = get_past_recommendations_with_returns(since_days=args.since_days)
    if not rows:
        print("无推荐记录，请先运行报告生成 9/10 分买入推荐。")
        return

    print("=" * 60)
    print("回测诊断：哪里在变差")
    print("=" * 60)
    print(f"回溯: {args.since_days} 天 | 有效记录: {summary.get('total_count', 0)} 条")
    print(f"整体胜率: {summary.get('win_rate_pct', 0)}% | 平均收益: {summary.get('avg_return_pct', 0):+.2f}%")
    print(f"近期 {summary.get('recent_n', 10)} 条胜率: {summary.get('recent_win_rate_pct', 0)}%")
    print()

    # 分市场
    by_market: dict[str, list] = defaultdict(list)
    by_market_1w: dict[str, list] = defaultdict(list)
    by_market_1m: dict[str, list] = defaultdict(list)
    by_market_2m: dict[str, list] = defaultdict(list)
    by_market_3m: dict[str, list] = defaultdict(list)
    for r in rows:
        m = (r.get("market") or "美股").strip()
        ret = r.get("return_pct")
        if ret is not None:
            by_market[m].append(ret)
        r1w = r.get("return_1w_pct")
        if r1w is not None:
            by_market_1w[m].append(r1w)
        r1m = r.get("return_1m_pct")
        if r1m is not None:
            by_market_1m[m].append(r1m)
        r2m = r.get("return_2m_pct")
        if r2m is not None:
            by_market_2m[m].append(r2m)
        r3m = r.get("return_3m_pct")
        if r3m is not None:
            by_market_3m[m].append(r3m)

    print("【分市场】")
    for m in sorted(by_market.keys()):
        wr, avg, n = _win_rate(by_market[m])
        wr1w, avg1w, n1w = _win_rate(by_market_1w[m])
        wr1m, avg1m, n1m = _win_rate(by_market_1m[m])
        wr2m, avg2m, n2m = _win_rate(by_market_2m[m])
        wr3m, avg3m, n3m = _win_rate(by_market_3m[m])
        print(f"  {m}: 至今 胜率{wr}% 收益{avg:+.2f}% (n={n})")
        print(f"       1周 {wr1w}%/{avg1w:+.2f}%  1月 {wr1m}%/{avg1m:+.2f}%  2月 {wr2m}%/{avg2m:+.2f}%  3月 {wr3m}%/{avg3m:+.2f}%")
    print()

    # 分持有期（汇总）
    print("【分持有期】")
    total_1w = summary.get("total_1w", 0)
    total_1m = summary.get("total_1m", 0)
    total_2m = summary.get("total_2m", 0)
    total_3m = summary.get("total_3m", 0)
    print(f"  1周: 胜率 {summary.get('win_rate_1w_pct', 0)}%  平均 {summary.get('avg_return_1w_pct', 0):+.2f}%  (n={total_1w})")
    print(f"  1月: 胜率 {summary.get('win_rate_1m_pct', 0)}%  平均 {summary.get('avg_return_1m_pct', 0):+.2f}%  (n={total_1m})")
    print(f"  2月: 胜率 {summary.get('win_rate_2m_pct', 0)}%  平均 {summary.get('avg_return_2m_pct', 0):+.2f}%  (n={total_2m})")
    print(f"  3月: 胜率 {summary.get('win_rate_3m_pct', 0)}%  平均 {summary.get('avg_return_3m_pct', 0):+.2f}%  (n={total_3m})")
    print()

    # 分基准环境
    by_env: dict[str, list] = defaultdict(list)
    by_env_market: dict[tuple, list] = defaultdict(list)  # (env, market) -> returns
    for r in rows:
        bench = r.get("benchmark_return_pct")
        env = _env_label(bench)
        ret = r.get("return_pct")
        if ret is not None:
            by_env[env].append(ret)
            m = (r.get("market") or "美股").strip()
            by_env_market[(env, m)].append(ret)

    print("【分基准环境】")
    for env in ["牛市", "震荡", "熊市", "未知"]:
        if env not in by_env:
            continue
        wr, avg, n = _win_rate(by_env[env])
        print(f"  {env}: 胜率 {wr}%  平均 {avg:+.2f}%  (n={n})")
    print()

    # 分基准环境 × 市场（交叉）
    print("【分基准环境 × 市场】")
    for env in ["牛市", "震荡", "熊市"]:
        for m in sorted(by_market.keys()):
            key = (env, m)
            if key not in by_env_market:
                continue
            wr, avg, n = _win_rate(by_env_market[key])
            print(f"  {env}+{m}: 胜率 {wr}%  平均 {avg:+.2f}%  (n={n})")
    print()

    # 风险与收益分布
    print("【收益分布】")
    print(f"  最佳: {summary.get('best_return_pct')}%  最差: {summary.get('worst_return_pct')}%")
    print(f"  >10%: {summary.get('dist_up_10', 0)}  0~10%: {summary.get('dist_0_10', 0)}  -10~0%: {summary.get('dist_neg10_0', 0)}  <-10%: {summary.get('dist_down_10', 0)}")
    print(f"  触及减仓价: {summary.get('triggered_exit_count', 0)} 条")
    print()

    # 诊断结论：哪里在变差
    print("【诊断结论】")
    overall_wr = summary.get("win_rate_pct", 0)
    recent_wr = summary.get("recent_win_rate_pct", 0)
    if recent_wr < overall_wr - 10:
        print(f"  ⚠ 近期胜率({recent_wr}%)明显低于整体({overall_wr}%)，近期推荐质量下降")
    else:
        print(f"  近期胜率与整体接近，无明显恶化")

    worst_market = None
    worst_market_wr = 100
    for m, rets in by_market.items():
        if len(rets) >= 3:
            wr, _, _ = _win_rate(rets)
            if wr < worst_market_wr:
                worst_market_wr = wr
                worst_market = m
    if worst_market and worst_market_wr < 50:
        print(f"  ⚠ {worst_market} 胜率仅 {worst_market_wr}%，需关注该市场选股逻辑")

    worst_env = None
    worst_env_wr = 100
    for env in ["牛市", "震荡", "熊市"]:
        if env in by_env and len(by_env[env]) >= 3:
            wr, _, _ = _win_rate(by_env[env])
            if wr < worst_env_wr:
                worst_env_wr = wr
                worst_env = env
    if worst_env and worst_env_wr < 50:
        print(f"  ⚠ {worst_env}环境下胜率仅 {worst_env_wr}%，需优化该环境下的策略")

    if args.verbose:
        print()
        print("【明细】")
        for r in rows[:20]:
            ticker = r.get("ticker", "")
            name = r.get("name", "")
            rd = r.get("report_date", "")
            ret = r.get("return_pct")
            bench = r.get("benchmark_return_pct")
            env = _env_label(bench)
            ret_s = f"{ret:+.2f}%" if ret is not None else "—"
            bench_s = f"{bench:+.2f}%" if bench is not None else "—"
            print(f"  {ticker} {name} {rd} 收益{ret_s} 基准{bench_s} {env}")


if __name__ == "__main__":
    main()
