"""Audit and report the fixed adaptive/cost experiment; no external actions."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

import numpy as np
import pandas as pd

from research.daily_factor_lab.core import DEFAULT_DATA, load_panel, simulate
from research.daily_factor_lab.experiment import WINDOWS, write_json
from research.watchlist_adaptive_cost import adaptive_scores, registry, score_panels
from scripts.verify_daily_factor_run import audit_ledger, compare_metrics


def read_json(path):
    return json.loads(Path(path).read_text())


def audit(directory, data_dir, baseline):
    directory = Path(directory)
    protocol = read_json(directory / "protocol.json")
    summary = read_json(directory / "summary.json")
    completed = read_json(directory / "completed.json")
    if completed["status"] != "complete":
        raise AssertionError("Incomplete run")
    for filename, digest in protocol["source_files_sha256"].items():
        if sha256(Path(filename).read_bytes()).hexdigest() != digest:
            raise AssertionError(f"Source changed: {filename}")
    if completed["source_files_sha256"] != protocol["source_files_sha256"]:
        raise AssertionError("Completion marker source mismatch")
    panel = load_panel(data_dir)
    if panel.metadata["input_sha256"] != completed["data_sha256"]:
        raise AssertionError("Input data changed")
    old = read_json(Path(baseline) / "summary.json")
    if old["data"]["input_sha256"] != panel.metadata["input_sha256"]:
        raise AssertionError("Baseline has different data")
    for name, windows in old["policies"].items():
        for window, metrics in windows.items():
            new = summary["policies"][name]["25"][window]
            for key, value in metrics.items():
                if new[key] != value:
                    raise AssertionError(f"Baseline regression: {name} {window} {key}")
    rules, additions = registry()
    scores = adaptive_scores(panel)
    panels = score_panels(panel, scores)
    ledgers = {}
    metrics_verified = 0
    for name in ["trend_heavy32", *additions]:
        rule = rules[name]
        curve = pd.read_csv(directory / f"{name}_full_curve.csv")
        orders = pd.read_csv(directory / f"{name}_full_orders.csv").to_dict("records")
        ledgers[name] = audit_ledger(panel, curve, orders, 25)
        for bps in (25, 50):
            for window, (start, end) in WINDOWS.items():
                result = simulate(panels[rule["score"]], rule["policy"], start, end, bps)
                compare_metrics(result["metrics"], summary["policies"][name][str(bps)][window])
                metrics_verified += 1
                if bps == 25 and window == "full":
                    pd.testing.assert_frame_equal(curve, result["curve"], check_exact=False, atol=1e-7)
    prefixes = []
    for cutoff in ("2025-03-31", "2026-06-30"):
        truncated = load_panel(data_dir, asof=cutoff)
        partial_scores = adaptive_scores(truncated)
        n = len(truncated.dates)
        np.testing.assert_allclose(scores["weights"][:n], partial_scores["weights"], atol=1e-14)
        partial_panels = score_panels(truncated, partial_scores)
        for name in ("adaptive_blend32", "adaptive_weekly32", "static_blend32"):
            rule = rules[name]
            expected = simulate(panels[rule["score"]], rule["policy"], WINDOWS["full"][0], cutoff)
            actual = simulate(partial_panels[rule["score"]], rule["policy"], WINDOWS["full"][0], cutoff)
            if actual["orders"] != expected["orders"]:
                raise AssertionError(f"Future truncation changed orders: {name} {cutoff}")
            np.testing.assert_allclose(actual["curve"].equity, expected["curve"].equity, atol=1e-7, rtol=0)
            prefixes.append(f"{name}/{cutoff}")
    return {
        "status": "reproducibility_ledger_and_causal_prefix_passed",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_files_sha256": protocol["source_files_sha256"],
        "review_script_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "data_sha256": panel.metadata["input_sha256"],
        "baseline_policies_unchanged": len(old["policies"]), "window_metrics_verified": metrics_verified,
        "independent_ledgers": ledgers, "real_data_prefix_checks": prefixes,
        "qualified_fixed_candidates": summary["qualified_fixed_candidates"],
        "walk_forward_gate_passed": summary["walk_forward"]["gate_passed"],
        "execution_authority": "none", "independent_oos": False,
    }


def pair(metrics):
    return f"{metrics['return_pct']:+.2f}% / {metrics['max_drawdown_pct']:.2f}%"


def report(directory, verification):
    directory = Path(directory)
    summary = read_json(directory / "summary.json")
    policies = summary["policies"]
    benchmarks = summary["benchmarks"]
    rows = [
        "# 日 K 研究续篇：降低换手与动态因子权重",
        "",
        f"日期：2026-09-23。运行目录：{directory.resolve()}。",
        "",
        "## 结论",
        "",
        "降低调仓频率改善了完整区间的收益和费用敏感性，但损失了最近半年的跟随能力。"
        "基于已兑现因子收益动态加权，也没有得到满足完整门槛的新版本。"
        "本轮不替换上一轮的冻结候选，不把新方法包装成可跨越所有区间的盈利策略。",
        "",
        "这一轮增加 9 个固定假设，同时保留此前全部 19 个方案参与对照和滚动选型，共 28 个方案。"
        "同一富途缓存、同一执行引擎、同一 $10,000 初始资本，分别扣每边 25 / 50 基点摩擦。"
        "所有窗口在之前的研究中已被查看过，因此整轮属于事后研究，不是独立样本外证明。",
        "",
        "## 新的规则是什么",
        "",
        "成本方向：只改变调仓频率，或把已有股票的保留范围从前5名放宽至前10名，"
        "同时将小额调仓容忍区从2.5个百分点放宽到5个百分点。止损、趋势过滤、单票目标上限30%保持不变。"
        "达到买入前5名才能新买，不是从前10名任意买入。",
        "",
        "因子方向：先将63日风险调整动量、相对QQQ的简化残差动量、接近年高程度转为横截面百分位，"
        "测试等权与动态加权。动态版只使用已经完整兑现的20日收益标签，"
        "计算最近63个交易日可用 Rank IC，至少20个有效观察。",
        "",
        "动态权重的一半固定为等权，另一半按非负历史IC分配；全无正IC时退回等权。"
        "每个因子权重在1/6至2/3之间，没有反向做空。日期s的收益标签到s+20收盘才可使用，"
        "不允许提前用未来收益评分。这是可审计的统计规则，不是AI每天任意改策略。",
        "",
        "## 主要对照",
        "",
        "每格为总收益 / 收盘最大回撤。完整区间2024-05-01—2026-09-22；半年2026-03-23—2026-09-22。",
        "",
        "| 方案 | 完整区间25bps | 完整区间50bps | 最近半年25bps | 最近半年50bps |",
        "|---|---:|---:|---:|---:|",
    ]
    for name in ["trend_heavy32", *summary["new_hypotheses"]]:
        values = policies[name]
        rows.append(f"| {name} | " + " | ".join(
            pair(values[b][w]) for w, b in (("full", "25"), ("full", "50"),
                                            ("recent_half", "25"), ("recent_half", "50"))) + " |")
    rows.append("| QQQ | " + " | ".join(
        pair(benchmarks[b][w]) for w, b in (("full", "25"), ("full", "50"),
                                            ("recent_half", "25"), ("recent_half", "50"))) + " |")
    baseline = policies["trend_heavy32"]["25"]["full"]
    weekly = policies["cost_weekly32"]["25"]["full"]
    rows += [
        "",
        "### 有效的改进：降低部分换手",
        "",
        f"仅从周一/周四改为每周调仓，完整区间订单从{baseline['orders']}笔降至{weekly['orders']}笔，"
        f"模拟摩擦从${baseline['cost_dollars']:,.2f}降至${weekly['cost_dollars']:,.2f}，"
        f"收益从{baseline['return_pct']:.2f}%提高至{weekly['return_pct']:.2f}%。"
        "在每边50基点时仍胜过QQQ的完整区间收益与回撤，说明它比原版更耐成本。",
        "",
        "但同一规则的最近半年只赚21.09%，低于QQQ的25.08%。这说明减少交易既节省费用，"
        "也会错过一部分排名变化；不能用完整区间改善推导近期必然改善。",
        "",
        "### 没有验证成功：动态因子权重",
        "",
        f"动态混合32%风险目标版本，完整区间为{pair(policies['adaptive_blend32']['25']['full'])}，"
        f"最近半年为{pair(policies['adaptive_blend32']['25']['recent_half'])}。"
        "相对等权混合有部分改善，但没有全面超过QQQ；扩大风险预算也没有修复这个问题。",
        "",
        "IC度量的是整个符合流动性条件股票池的排序关联，而交易组合还受到最多5只、趋势过滤、"
        "持有缓冲、退出和仓位限制影响。两者并不是同一个优化目标。过去IC有信息，"
        "不代表按它调整权重就会提高扣费后的组合收益；重叠20日标签也不能当作63个独立样本。",
        "",
        "## 所有区间，不能只看完整净值",
        "",
        "| 方案 | 较早区间 | 2025-04至12 | 2026-01至09 |",
        "|---|---:|---:|---:|",
    ]
    for name in ["trend_heavy32", "cost_weekly32", "cost_buffer32", "adaptive_blend32"]:
        rows.append(f"| {name} | " + " | ".join(
            pair(policies[name]["25"][w]) for w in ("early", "middle", "recent")) + " |")
    rows.append("| QQQ | " + " | ".join(
        pair(benchmarks["25"][w]) for w in ("early", "middle", "recent")) + " |")
    wf = summary["walk_forward"]
    rows += [
        "",
        "## 事先定义的检查门槛",
        "",
        "固定方案须在25/50基点下，完整区间与最近半年同时胜过QQQ收益和回撤，"
        "且早/中/近三个非重叠区间至少两个也同时占优。满足者："
        + ("、".join(summary["qualified_fixed_candidates"]) or "无") + "。",
        "",
        "滚动选型仍保留全部旧方案，不把失败的旧候选从选择池删除。"
        "三个训练区间仍选中 blend_weekly20、blend_weekly20、blend_buffer28；"
        "增加新方案没有改变历史上这个选择器会做出的决定。",
        "",
        f"连续滚动净值（25基点）：{pair(wf['costs']['25'])}，"
        f"QQQ：{pair(wf['costs']['25']['qqq'])}。门槛："
        + ("通过" if wf["gate_passed"] else "未通过") + "。",
        "",
        "## 实际验证",
        "",
        f"- {verification['baseline_policies_unchanged']}个旧方案的25基点、五窗口结果与上一轮完全一致；未更换底层成交口径。",
        f"- 复算{verification['window_metrics_verified']}组新方案/基线窗口指标，两档成本均一致。",
        "- 新方案与基线共10份完整区间订单账本，独立重建现金、股数、每日净值，均通过。",
        "- 截断至2025-03-31及2026-06-30，只用截止日期前的真实缓存重新生成权重，"
        "动态/静态代表方案的历史成交不变，净值数值一致。",
        "- 旧的core.py、paper.py和已冻结的纸面实例没有修改；本轮没有部署新交易策略。",
        "",
        "## 下一步的边界",
        "",
        "当前证据更支持研究成本和持有路径，不支持继续提高交易频率，也不足以支持自动因子轮换。"
        "若继续做条件化因子研究，应让因子评价样本与实际可买股票池一致，并明确考虑换手成本；"
        "仍需事先固定少量假设，不能在现有历史上无限挑选，直到只剩一条好看的曲线。",
        "",
        "自选池后见偏差、复权单位、未计现金分红、开盘股数事后计算、现金即时再用、"
        "缺乏独立未知数据和真实成交的限制全部保留。更好看的历史收益不代表未来两个月的收益。",
        "",
        "## 理论参考与复现",
        "",
        "成本缓冲的研究动机可参考美联储原始工作论文 Zeroing in on the Expected Returns of Anomalies，"
        "其中讨论buy/hold spread及成本优化，并强调扣费后和发表后的表现。"
        "本项目的前5/前10、周一/周四等是自己的实现，不是该论文参数的验证复制。",
        "",
        "原始论文：https://www.federalreserve.gov/econres/feds/files/2020039pap.pdf",
        "",
        "```bash",
        ".venv/bin/python -m research.watchlist_adaptive_cost \\",
        "  --output outputs/daily_factor_lab/adaptive_cost_my_run",
        ".venv/bin/python -m pytest -q tests/test_watchlist_adaptive_cost.py",
        "```",
        "",
        "全部方案、研究阶段、规则和代码哈希在protocol.json；权重、已兑现IC、订单、净值、"
        "成本对照、失败门槛在同一运行目录。没有AI历史收益，没有OpenD或券商订单。",
    ]
    return "\n".join(rows) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--audit", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA))
    parser.add_argument("--baseline", default="outputs/daily_factor_lab/20260923_delivery")
    args = parser.parse_args()
    if Path(args.audit).exists() or Path(args.report).exists():
        raise FileExistsError("Use new audit and report filenames")
    verification = audit(args.run, args.data_dir, args.baseline)
    text = report(args.run, verification)
    write_json(args.audit, verification)
    with Path(args.report).open("x", encoding="utf-8") as handle:
        handle.write(text)
    print(json.dumps({"status": verification["status"], "report": args.report,
                      "window_metrics_verified": verification["window_metrics_verified"]}))


if __name__ == "__main__":
    main()
