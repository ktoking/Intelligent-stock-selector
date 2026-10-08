"""Conditioned IC and cost-realized factor weighting, retrospective only.

This extension preserves every prior hypothesis and the frozen execution engine.
Reference portfolios are simulations, never broker accounts or historical AI.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import time

import numpy as np
import pandas as pd

from research.daily_factor_lab.core import DEFAULT_DATA, Panel, Policy, benchmark, load_panel, simulate
from research.daily_factor_lab.experiment import FOLDS, WINDOWS, comparison, plot_result, write_json
from research.watchlist_adaptive_cost import (
    FACTORS, adaptive_scores, hypothesis_gate, panel_for_policy_changes, registry, score_panels,
)
from scripts.verify_daily_factor_run import audit_ledger


def bounded_weights(signal, learned_share=.5):
    """Long-only, half equal-weight shrinkage; unavailable observations are zero."""
    if not 0 <= learned_share <= 1:
        raise ValueError("Invalid learned share")
    positive = np.maximum(np.nan_to_num(signal, nan=0., posinf=0., neginf=0.), 0.)
    total = positive.sum(axis=1, keepdims=True)
    count = positive.shape[1]
    learned = np.divide(positive, total, out=np.full_like(positive, 1 / count), where=total > 0)
    return (1 - learned_share) / count + learned_share * learned


def combine_rank_scores(ranks, weights):
    """Quantize decision scores so numerical ties use the symbol tie-breaker.

    Adding all-NaN future listings changes vector lengths in a retrospective
    panel. Reference NAV dot products can then vary at ~1e-16 return precision.
    That must not resolve mathematically tied buy rankings or cash priority.
    Twelve decimal places is a declared numerical tolerance, not an alpha
    threshold or a return-tuned parameter.
    """
    return np.round(np.sum(ranks * weights[:, None, :], axis=-1), decimals=12)


def conditional_features(panel, *, min_stocks=10, warmup_sessions=151):
    f, q = panel.fields, panel.qqq
    raw = np.stack([f[name] for name in FACTORS], axis=-1)
    eligible = (
        panel.stock_mask[None, :] & (f["liquid20"] >= 5e6)
        & np.isfinite(f["vol20"]) & (f["vol20"] > 0)
        & (f["close"] > f["ema50"]) & (f["ema50"] > f["ema150"])
        & (f["mom63"] > 0) & np.isfinite(raw).all(axis=-1)
    )
    risk_on = f["close"][:, q] > f["ema100"][:, q]
    ranks = np.full_like(raw, np.nan)
    for k in range(len(FACTORS)):
        ranks[:, :, k] = pd.DataFrame(
            np.where(eligible, raw[:, :, k], np.nan)).rank(axis=1, pct=True).to_numpy()
    matured_ic = np.full((len(panel.dates), len(FACTORS)), np.nan)
    for terminal in range(20, len(panel.dates)):
        signal = terminal - 20
        if not risk_on[signal]:
            continue
        labels = f["close"][terminal] / f["open"][signal + 1] - 1
        valid = eligible[signal] & np.isfinite(labels)
        if valid.sum() < min_stocks:
            continue
        target = pd.Series(labels[valid]).rank().to_numpy()
        for k in range(len(FACTORS)):
            values = ranks[signal, valid, k]
            if values.std() > 0 and target.std() > 0:
                matured_ic[terminal, k] = np.corrcoef(values, target)[0, 1]
    ic_mean = pd.DataFrame(matured_ic).rolling(63, min_periods=20).mean().to_numpy()
    weights = {"conditional": bounded_weights(ic_mean)}
    # The rank mask must NOT include risk_on. A market new-buy gate is not a
    # force-liquidation instruction for existing trend-valid holdings.
    reference_returns = np.full_like(matured_ic, np.nan)
    qqq_returns = np.full(len(panel.dates), np.nan)
    reference_audit = {}
    if len(panel.dates) > warmup_sessions:
        start, end = panel.dates[warmup_sessions], panel.dates[-1]
        reference = benchmark(panel, start, end, 25)
        qqq_returns[warmup_sessions:] = pd.Series(
            np.r_[10000., reference["curve"].equity.to_numpy()]).pct_change().to_numpy()[1:]
        for k, factor in enumerate(FACTORS):
            policy = Policy("reference_" + factor, factor=factor, annual_vol_target=.32,
                            rank_buffer=10, turnover_band=.05)
            result = simulate(panel, policy, start, end, 25)
            reference_returns[warmup_sessions:, k] = pd.Series(
                np.r_[10000., result["curve"].equity.to_numpy()]).pct_change().to_numpy()[1:]
            reference_audit[factor] = {
                "policy": asdict(policy), "metrics": result["metrics"],
                "curve": result["curve"], "orders": result["orders"],
            }
    active = pd.DataFrame(reference_returns - qqq_returns[:, None])
    for name, lookback, minimum in (("net63", 63, 42), ("net126", 126, 63)):
        average = active.rolling(lookback, min_periods=minimum).mean()
        volatility = active.rolling(lookback, min_periods=minimum).std()
        signal = (average / volatility.where(volatility > 1e-12)).to_numpy()
        weights[name] = bounded_weights(signal)
    return {
        "scores": {name: combine_rank_scores(ranks, w) for name, w in weights.items()},
        "weights": weights, "matured_conditional_ic": matured_ic,
        "reference_returns": reference_returns, "qqq_reference_returns": qqq_returns,
        "reference_audit": reference_audit, "eligible": eligible, "risk_on": risk_on,
    }


def all_rules():
    rules, _ = registry()
    additions = []
    for name, score, risk in (
        ("conditional_ic32", "conditional", .32),
        ("conditional_ic36", "conditional", .36),
        ("realized_net63_32", "net63", .32),
        ("realized_net63_36", "net63", .36),
        ("realized_net126_32", "net126", .32),
    ):
        rules[name] = {"policy": Policy(name, annual_vol_target=risk, rank_buffer=10,
                                       turnover_band=.05), "score": score}
        additions.append(name)
    return rules, additions


def build_panels(panel, features):
    panels = score_panels(panel, adaptive_scores(panel))
    for name, score in features["scores"].items():
        panels[name] = Panel(
            panel.dates, panel.symbols, panel.stock_mask,
            {**panel.fields, "momentum_risk": score},
            {**panel.metadata, "scoring_override": name},
        )
    return panels


def prefix_checks(panel, panels, features, rules, data_dir):
    results = []
    for cutoff in ("2025-03-31", "2026-06-30"):
        partial = load_panel(data_dir, asof=cutoff)
        partial_features = conditional_features(partial)
        for mode in ("conditional", "net63", "net126"):
            np.testing.assert_allclose(
                features["weights"][mode][:len(partial.dates)],
                partial_features["weights"][mode], atol=1e-12, rtol=1e-10,
            )
        partial_panels = build_panels(partial, partial_features)
        for name in ("conditional_ic32", "realized_net63_32", "realized_net126_32"):
            rule = rules[name]
            expected = simulate(panels[rule["score"]], rule["policy"], WINDOWS["full"][0], cutoff)
            actual = simulate(partial_panels[rule["score"]], rule["policy"], WINDOWS["full"][0], cutoff)
            if actual["orders"] != expected["orders"]:
                raise AssertionError(f"Prefix order mismatch: {name} {cutoff}")
            np.testing.assert_allclose(actual["curve"].equity, expected["curve"].equity, atol=1e-7, rtol=0)
            results.append(f"{name}/{cutoff}")
    return results


def pair(m):
    return f"{m['return_pct']:+.2f}% / {m['max_drawdown_pct']:.2f}%"


def report(summary, directory):
    selected = ["trend_heavy32", "cost_weekly32", "adaptive_blend32", *summary["new_hypotheses"]]
    rows = [
        "# 日 K 研究第三轮：可买股票条件与扣费后的因子组合",
        "",
        f"日期：2026-09-23。结果目录：{Path(directory).resolve()}。",
        "",
        "## 实验设计",
        "",
        "此前用全流动性股票池的Rank IC动态加权，并未通过稳健性检验。"
        "本轮固定5个新方案，保留此前28个方案参与全部比较，不删除旧失败结果。"
        "仍用同一富途缓存、同一成交引擎、$10,000初始资金，研究后见偏差与执行限制不变。",
        "",
        "- 条件化IC：仅评价当时QQQ允许新买、且个股收盘>EMA50>EMA150、63日动量为正、"
        "流动性与波动率合格的股票。20日收益标签必须兑现后才进入最近63日统计，至少20个有效观察、每次至少10只。",
        "- 已实现扣费组合：3个因子各自按相同的真实模拟交易规则运行参考组合，"
        "最多5只、单股上限30%、32%年化波动目标、周一/周四、持有前10名缓冲、5个百分点调仓容忍。"
        "其资金、趋势退出和每边25基点成本均进入每天净值；以这些已观察净收益相对QQQ的表现评价因子。",
        "- 参考组合评分使用63日或126日平均超额日收益/超额收益波动；"
        "分别要求至少42/63个观察。这里的两个窗口提前固定，不是连续扫描最佳参数。",
        "- 因子权重仍有50%等权底座，另50%按非负评分分配，每因子在1/6—2/3。"
        "最终组合用这些权重合并选股排名，不是直接合并3个参考账户的财富。",
        "- 参考模型的成本预测固定为每边25基点；50基点是最终组合实际成本压力，"
        "不会事后按哪个假设赚得更多来改参考模型。",
        "",
        "所有收盘信息只用于下一开盘，参考组合也不能提前看到当天尚未完成的收益。"
        "QQQ的新买限制与强制清仓是两回事，不能因为QQQ转弱就把持有股票的排名全部改为无效。",
        "",
        "## 回测结果",
        "",
        "每格为区间总收益/收盘最大回撤。完整区间2024-05-01—2026-09-22，"
        "最近半年2026-03-23—2026-09-22；分段均从现金重新起算。",
        "",
        "| 方案 | 完整25bps | 完整50bps | 半年25bps | 半年50bps |",
        "|---|---:|---:|---:|---:|",
    ]
    for name in selected:
        windows = summary["policies"][name]
        rows.append(f"| {name} | " + " | ".join(
            pair(windows[b][w]) for w, b in (("full", "25"), ("full", "50"),
                                             ("recent_half", "25"), ("recent_half", "50"))) + " |")
    rows.append("| QQQ | " + " | ".join(
        pair(summary["benchmarks"][b][w]) for w, b in (("full", "25"), ("full", "50"),
                                                       ("recent_half", "25"), ("recent_half", "50"))) + " |")
    rows += [
        "",
        "| 新方案 | 较早2024-05—2025-03 | 中段2025-04—12 | 近期2026-01—09 |",
        "|---|---:|---:|---:|",
    ]
    for name in summary["new_hypotheses"]:
        rows.append(f"| {name} | " + " | ".join(
            pair(summary["policies"][name]["25"][w]) for w in ("early", "middle", "recent")) + " |")
    rows += [
        "",
        "## 不降低的门槛",
        "",
        "固定方案门槛：25/50基点下完整区间与最近半年均同时胜过QQQ收益、回撤，"
        "且早/中/近三个非重叠区间至少两个也同时占优。满足者："
        + ("、".join(summary["qualified_fixed_candidates"]) or "无") + "。",
        "",
        "滚动选型仍只用各分段之前的训练成绩，从全部33个方案选择；"
        "要求合格训练收益和回撤，再按超额收益减回撤评分，缺省现金。"
        "分析者已经看过历史，因此即使通过也仍是回顾性验证，不是未见过的样本外检验。",
        "",
        "| 测试开始 | 训练选择 | 分段收益/回撤 |",
        "|---|---|---:|",
    ]
    for fold in summary["walk_forward"]["folds"]:
        rows.append(f"| {fold['test_start']} | {fold['selected']} | {pair(fold['test_from_cash'])} |")
    wf = summary["walk_forward"]
    rows += [
        "",
        f"连续滚动25基点：{pair(wf['costs']['25'])}；QQQ：{pair(wf['costs']['25']['qqq'])}。"
        f"连续滚动50基点：{pair(wf['costs']['50'])}；QQQ：{pair(wf['costs']['50']['qqq'])}。"
        "原有滚动推广门槛：" + ("通过" if wf["gate_passed"] else "未通过") + "。",
        "",
        "## 审计与限制",
        "",
        f"- 前28组方案两档成本、五个窗口的已有指标全部原样复现：{summary['audit']['old_policies_unchanged']}组。",
        "- 新增5组完整区间订单均独立核对现金、持仓、逐日净值；"
        "3个扣费参考组合也独立核对，不只核对最终收益百分比。",
        "- 在2025-03-31和2026-06-30截断原始数据，重算因子权重与历史交易；"
        f"通过{len(summary['audit']['prefix_checks'])}组代表方案的实际历史前缀检查。",
        "- 当前自选池有后见/幸存者偏差；参考组合表现也是模拟值，不是真实账户或已发生的AI决策。",
        "- 因子评分与最后的混合排名组合仍有差异。即使参考组合曾经赚钱，"
        "组合其排名也不保证产生同样收益；策略选型与权重追随历史赢家同样可能失效。",
        "- 没有修改原paper实例，没有部署新策略，没有OpenD、富途GUI或订单端点调用。",
        "- 首次运行的真实数据截断检查发现1e-16量级浮点差异打破并列排名，改变了买单顺序；"
        "该运行未通过、没有完成标记。现将最终百分位合成分数固定到12位小数，"
        "并列时仍按股票代码排序；没有放宽订单前缀一致性检查。",
        "",
        "## 复现",
        "",
        "```bash",
        ".venv/bin/python -m research.watchlist_conditional_factor \\",
        "  --output outputs/daily_factor_lab/conditional_my_run",
        ".venv/bin/python -m pytest -q tests/test_watchlist_conditional_factor.py",
        "```",
        "",
        "protocol.json封存全部规则和源码/数据哈希；权重、参考净收益、实际模拟订单、"
        "全部33组对照和失败门槛均保存。不能把模型越复杂或测试通过当作盈利已经验证。",
    ]
    return "\n".join(rows) + "\n"


def run(output, data_dir=DEFAULT_DATA, baseline="outputs/daily_factor_lab/adaptive_cost_20260923_v1",
        report_path=None):
    directory = Path(output)
    directory.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    panel = load_panel(data_dir)
    rules, additions = all_rules()
    source_paths = [
        Path(__file__), Path("research/watchlist_adaptive_cost.py"),
        Path("research/daily_factor_lab/core.py"), Path("research/daily_factor_lab/experiment.py"),
        Path("scripts/verify_daily_factor_run.py"),
    ]
    protocol = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "posthoc_after_previous_hypotheses": 28, "independent_oos": False,
        "data": panel.metadata, "windows": WINDOWS, "folds": FOLDS,
        "new_hypotheses": additions, "cost_bps": [25, 50],
        "rules": {n: {"policy": asdict(r["policy"]), "score": r["score"]} for n, r in rules.items()},
        "conditional_ic": {"horizon": 20, "lookback": 63, "min_observations": 20, "min_stocks": 10},
        "reference": {"start_index": 151, "cost_forecast_bps": 25, "risk": .32,
                      "schedule": "twice", "rank_buffer": 10, "turnover_band": .05},
        "net_score": {"lookbacks_minima": [[63, 42], [126, 63]],
                      "score": "mean daily active return / daily active volatility",
                      "learned_share": .5, "negative_signals": "zero; equal fallback"},
        "score_precision_decimals": 12,
        "source_files_sha256": {str(p): sha256(p.read_bytes()).hexdigest() for p in source_paths},
        "execution_authority": "none",
    }
    write_json(directory / "protocol.json", protocol)
    features = conditional_features(panel)
    panels = build_panels(panel, features)
    for name, weights in features["weights"].items():
        pd.DataFrame(weights, index=panel.dates, columns=FACTORS).to_csv(directory / f"{name}_weights.csv")
    pd.DataFrame(features["matured_conditional_ic"], index=panel.dates, columns=FACTORS).to_csv(
        directory / "matured_conditional_ic.csv")
    pd.DataFrame(features["reference_returns"], index=panel.dates, columns=FACTORS).to_csv(
        directory / "reference_returns.csv")
    old = json.loads((Path(baseline) / "summary.json").read_text())
    if old["data"]["input_sha256"] != panel.metadata["input_sha256"]:
        raise AssertionError("Baseline data differs")
    summary = {"data": panel.metadata, "policies": {}, "benchmarks": {},
               "new_hypotheses": additions, "audit": {}}
    cache = {}
    for bps in (25, 50):
        summary["benchmarks"][str(bps)] = {}
        for window, (start, end) in WINDOWS.items():
            ref = benchmark(panel, start, end, bps)
            summary["benchmarks"][str(bps)][window] = ref["metrics"]
            for name, rule in rules.items():
                result = simulate(panels[rule["score"]], rule["policy"], start, end, bps)
                stats = comparison(result["metrics"], ref["metrics"])
                summary["policies"].setdefault(name, {}).setdefault(str(bps), {})[window] = stats
                if name in old["policies"]:
                    previous = old["policies"][name][str(bps)][window]
                    if any(previous[k] != v for k, v in stats.items()):
                        raise AssertionError(f"Old policy changed: {name} {bps} {window}")
                if bps == 25 and window == "full":
                    cache[name] = result
            print(f"completed {bps}bps {window}: {len(rules)} rules", flush=True)
    summary["audit"]["old_policies_unchanged"] = len(old["policies"])
    changes, folds = {}, []
    for train_end, start, end in FOLDS:
        ref = benchmark(panel, WINDOWS["full"][0], train_end, 25)["metrics"]
        training = {
            n: comparison(simulate(panels[r["score"]], r["policy"],
                                   WINDOWS["full"][0], train_end, 25)["metrics"], ref)
            for n, r in rules.items()
        }
        qualified = [n for n, m in training.items() if m["beats_qqq_return_and_drawdown"]]
        chosen = (max(qualified, key=lambda n: training[n]["excess_return_points"]
                      - training[n]["max_drawdown_pct"]) if qualified else "cash")
        rule = rules.get(chosen, {"policy": Policy("cash", positions=0), "score": "original"})
        date = next(d for d in panel.dates if d >= pd.Timestamp(start))
        changes[str(date.date())] = rule["policy"]
        test = simulate(panels[rule["score"]], rule["policy"], start, end, 25)
        qqq = benchmark(panel, start, end, 25)
        folds.append({"train_end": train_end, "test_start": start, "test_end": end,
                      "training": training, "selected": chosen,
                      "test_from_cash": comparison(test["metrics"], qqq["metrics"])})
    wf_panel = panel_for_policy_changes(panels, rules, changes)
    wf = {"folds": folds, "costs": {}}
    for bps in (25, 50):
        result = simulate(wf_panel, next(iter(changes.values())), FOLDS[0][1], FOLDS[-1][2], bps, changes)
        ref = benchmark(panel, FOLDS[0][1], FOLDS[-1][2], bps)
        wf["costs"][str(bps)] = {**comparison(result["metrics"], ref["metrics"]), "qqq": ref["metrics"]}
        result["curve"].to_csv(directory / f"walk_forward_{bps}_curve.csv", index=False)
        pd.DataFrame(result["orders"]).to_csv(directory / f"walk_forward_{bps}_orders.csv", index=False)
    wf["gate_passed"] = bool(
        all(wf["costs"][str(b)]["beats_qqq_return_and_drawdown"] for b in (25, 50))
        and sum(f["test_from_cash"]["beats_qqq_return_and_drawdown"] for f in folds) >= 2)
    summary["walk_forward"] = wf
    summary["qualified_fixed_candidates"] = [n for n, m in summary["policies"].items() if hypothesis_gate(m)]
    summary["audit"]["new_ledgers"] = {}
    for name in additions:
        result = cache[name]
        result["curve"].to_csv(directory / f"{name}_curve.csv", index=False)
        pd.DataFrame(result["orders"]).to_csv(directory / f"{name}_orders.csv", index=False)
        curve = pd.read_csv(directory / f"{name}_curve.csv")
        orders = pd.read_csv(directory / f"{name}_orders.csv").to_dict("records")
        pd.testing.assert_frame_equal(curve, result["curve"], check_exact=False, atol=1e-7)
        summary["audit"]["new_ledgers"][name] = audit_ledger(panel, curve, orders, 25)
    summary["audit"]["reference_ledgers"] = {}
    for factor, result in features["reference_audit"].items():
        result["curve"].to_csv(directory / f"reference_{factor}_curve.csv", index=False)
        pd.DataFrame(result["orders"]).to_csv(directory / f"reference_{factor}_orders.csv", index=False)
        summary["audit"]["reference_ledgers"][factor] = audit_ledger(panel, result["curve"], result["orders"], 25)
    summary["audit"]["prefix_checks"] = prefix_checks(panel, panels, features, rules, data_dir)
    plot_result({"QQQ": benchmark(panel, *WINDOWS["full"], 25)["curve"],
                 **{n: cache[n]["curve"] for n in
                    ("trend_heavy32", "conditional_ic32", "realized_net63_32")}},
                directory / "equity_drawdown.png")
    summary["elapsed_seconds"] = time.perf_counter() - started
    write_json(directory / "summary.json", summary)
    text = report(summary, directory)
    with (directory / "REPORT.md").open("x", encoding="utf-8") as handle:
        handle.write(text)
    if report_path is not None:
        with Path(report_path).open("x", encoding="utf-8") as handle:
            handle.write(text)
    write_json(directory / "completed.json", {
        "status": "complete", "data_sha256": panel.metadata["input_sha256"],
        "source_files_sha256": protocol["source_files_sha256"],
        "artifact_sha256": {p.name: sha256(p.read_bytes()).hexdigest()
                            for p in sorted(directory.iterdir()) if p.is_file()},
        "fixed_candidates": summary["qualified_fixed_candidates"], "walk_forward_gate": wf["gate_passed"],
    })
    print(json.dumps({"output": str(directory), "qualified": summary["qualified_fixed_candidates"],
                      "walk_forward_gate": wf["gate_passed"], "elapsed_seconds": summary["elapsed_seconds"]}))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA))
    parser.add_argument("--report")
    args = parser.parse_args()
    run(args.output, args.data_dir, report_path=args.report)


if __name__ == "__main__":
    main()
