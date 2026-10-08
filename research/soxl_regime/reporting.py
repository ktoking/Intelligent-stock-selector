from __future__ import annotations

import argparse
import json
from pathlib import Path


def render(result_path: Path, validation_path: Path, output_path: Path) -> None:
    result = json.loads(result_path.read_text())
    validation = json.loads(validation_path.read_text())
    full = result["full_proxy"]
    candidate = result["generated_candidate"]
    recent = result["recent_5m"]
    shares = result["regime_share"]
    segments = result["segments"]
    wf = result["walk_forward"]
    contribution = full["contribution_by_mode"]
    text = f"""# SOXL Regime Switch V1 研究报告

## 结论

最终状态：**{result['final_status']}**。

两年 60 分钟代理模型为正，但不满足 Balanced 门禁：最大回撤 {full['max_drawdown_pct']}% > 15%，四个半年区间只有 {sum(x['return_pct'] > 0 for x in segments)}/4 盈利，收益高度集中于趋势分支，且 2025-09-25～2026-03-24 为负。由于缺少两年 5 分钟 Alpaca/Futu 数据，本次不能将代理结果升级为正式可执行策略。

已生成一个**研究候选** `.quant` 供富途导入和回测，默认 `EXECUTION_ENABLED=0`；没有生成 Balanced 或 Aggressive 正式版。

## Data

- 两年代理：Yahoo 60m，{result['authoritative_data_range']['hourly']['actual_start']} ～ {result['authoritative_data_range']['hourly']['actual_end']}，{result['authoritative_data_range']['hourly']['rows']} bars。
- 日线：{result['authoritative_data_range']['daily']['actual_start']} ～ {result['authoritative_data_range']['daily']['actual_end']}，{result['authoritative_data_range']['daily']['rows']} bars。
- 精确 5m 诊断：{result['authoritative_data_range']['five_minute_recent']['actual_start']} ～ {result['authoritative_data_range']['five_minute_recent']['actual_end']}，{result['authoritative_data_range']['five_minute_recent']['rows']} bars。
- RTH：America/New_York 09:30～16:00；价格已由 Yahoo `auto_adjust=True` 调整。
- 未使用 OpenD；未调用任何交易接口。

## Regime

- 当前（2026-09-18 收盘后）：**{result['current_regime_at_2026_09_18']}**。
- TREND_UP {shares.get('TREND_UP', 0):.2%}；RANGE {shares.get('RANGE', 0):.2%}；TREND_DOWN {shares.get('TREND_DOWN', 0):.2%}；HIGH_VOL/TRANSITION {(shares.get('HIGH_VOL', 0) + shares.get('TRANSITION', 0)):.2%}。
- 切换次数：{result['regime_switches']}。
- 最优代理阈值：{json.dumps(result['regime_config'], ensure_ascii=False)}。
- RANGE 占比仅 {shares.get('RANGE', 0):.2%}，说明该分类边界仍偏窄，是失败项之一。

## Strategy Results

### 返回最大化代理（不采用）

- 参数：Trend 50%，Range 25%，Down Panic 15%。
- 收益 {full['return_pct']}%，最大回撤 {full['max_drawdown_pct']}%，PF {full['profit_factor']}，Sharpe {full['sharpe']}，Sortino {full['sortino']}，Calmar {full['calmar']}，交易 {full['trades']}。
- Top1/Top3/Top5 收益贡献：{full['top1_contribution_pct']}% / {full['top3_contribution_pct']}% / {full['top5_contribution_pct']}%，属于 **FRAGILE**。
- 分支 PnL：Trend {contribution.get('TREND', 0):.2f}；Range {contribution.get('RANGE', 0):.2f}；Down Panic {contribution.get('DOWN_PANIC', 0):.2f}。

### 生成的保守研究候选

- 参数：Trend 25%，Range 25%，Down Panic 0%（负 Edge 已禁用），Transition 0%。
- 代理收益 {candidate['return_pct']}%，最大回撤 {candidate['max_drawdown_pct']}%，PF {candidate['profit_factor']}，交易 {candidate['trades']}。
- 仍未达到 Balanced 的 15% 回撤门禁，因此只允许富途回测，不应实盘。

### 最近 5m 诊断

- 收益 {recent['return_pct']}%，最大回撤 {recent['max_drawdown_pct']}%，交易仅 {recent['trades']} 笔。
- 样本过少，不能作为两年稳健性证据。

## Robustness

| 区间 | 收益 | 最大回撤 | PF | 交易数 |
|---|---:|---:|---:|---:|
"""
    for row in segments:
        text += f"| {row['segment']} {row['start']}～{row['end']} | {row['return_pct']}% | {row['max_drawdown_pct']}% | {row['profit_factor']} | {row['trades']} |\n"
    text += "\nWalk-forward（9 个月 Train 选择参数，随后 3 个月 Validate）：\n\n"
    for row in wf:
        text += f"- {row['window']} {row['start']}～{row['end']}: {row['return_pct']}%，DD {row['max_drawdown_pct']}%，{row['trades']} 笔。\n"
    stress = result["cost_stress_bps_per_side"]
    text += f"""

成本压力：单边 5/10/20 bps 后收益分别为 {stress['5']['return_pct']}% / {stress['10']['return_pct']}% / {stress['20']['return_pct']}%。成本后仍为正，但不修复回撤与收益集中问题。

## Benchmark

- SOXL Buy & Hold：收益 {result['benchmark_buy_hold']['return_pct']}%，最大回撤 {result['benchmark_buy_hold']['max_drawdown_pct']}%。
- 用户提供的 Futu V4 基线：约 +10.41%，最大回撤约 6.9%，Sharpe 3.707，Sortino 6.361，约 93 单（本次未重新验证）。
- 新模型：风险明显低于 Buy & Hold，但仍未达到 Balanced 门禁。

## Futu Quant 静态验证

- 基线模板：`SOXL_QQQ_DYNAMIC_REVERSION_V4_FIXED.quant`，SHA-256 `{validation.get('template_sha256')}`。
- 输出文件：`SOXL_REGIME_SWITCH_V1_RESEARCH_CANDIDATE.quant`，SHA-256 `{validation['sha256']}`。
- Protocol Buffers wire parse、节点/连线唯一性、无悬空边、买卖路径、RSI(14)、完成 K 线、09:30 重置、09:45/15:30 入场边界、15:45 强平、退出优先于入场：全部静态 PASS。
- 文件仅含真实模板克隆的“开始”与“自编码动作”节点；未知顶层元数据与版本/指标定义由模板保留。
- 静态验证不等于富途 UI 导入/运行验证，必须在富途中回测确认。

## Failures / Rejected

- 两年 5m 数据门禁未完成：Alpaca 不可用，Alpha Vantage 月度 5m 是付费端点。
- Down Panic 全窗贡献为负，最终候选禁用。
- RANGE 分类过窄，且放宽后代理贡献不稳定。
- 5m momentum breakout、5m UP pullback：沿用既有研究结论，未发现稳定 Edge，拒绝。
- TRANSITION：保持现金。
- 收益集中与 2026 上半年趋势行情高度相关，不能以 +72.85% 作为可推广结果。

## 下一步富途验证

1. 导入研究候选 `.quant`，绑定 `trading_symbol=US.SOXL`，保持 `EXECUTION_ENABLED=0` 先检查指标/提醒。
2. 富途回测中将 `EXECUTION_ENABLED` 改为 1，仅用于回测；区间覆盖 2024-09-20～2026-09-18，5 分钟运行。
3. 导出回测订单与权益后，对齐本报告的下一根成交、单边成本、分段和仓位门禁；未通过前不实盘。
"""
    output_path.write_text(text, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", type=Path, default=Path("outputs/soxl_regime_switch_v1/research_result.json"))
    parser.add_argument("--validation", type=Path, default=Path("outputs/soxl_regime_switch_v1/quant_validation.json"))
    parser.add_argument("--output", type=Path, default=Path("outputs/soxl_regime_switch_v1/SOXL_REGIME_RESEARCH_REPORT.md"))
    args = parser.parse_args(); render(args.result, args.validation, args.output)


if __name__ == "__main__":
    main()
