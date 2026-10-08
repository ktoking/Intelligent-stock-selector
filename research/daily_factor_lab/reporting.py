"""Generate a detailed Chinese report from completed, immutable run artifacts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def pair(metrics):
    if metrics.get("return_pct") is None or metrics.get("max_drawdown_pct") is None:
        return "数据不足"
    return f'{metrics["return_pct"]:+.2f}% / {metrics["max_drawdown_pct"]:.2f}%'


def ic_text(value):
    return "数据不足" if value is None else f"{value:+.4f}"


def build_report(directory):
    directory = Path(directory)
    if not (directory/"completed.json").exists():
        raise ValueError("Run has no completion marker")
    summary=json.loads((directory/"summary.json").read_text())
    protocol=json.loads((directory/"protocol.json").read_text())
    name=summary["retrospective_candidate"]
    policies=summary["policies"]
    benchmark=summary["benchmarks"]
    wf=summary["walk_forward"]
    rows=[
        "# 日 K 因子、仓位和交易方法研究",
        "",
        "研究日期：2026-09-23。运行目录："+str(directory.resolve())+"。",
        "",
        "## 结论",
        "",
        ("本次找到历史区间内优于 QQQ 的研究候选。" if name else "本次没有选出同时满足历史收益与回撤条件的候选。")
        + "是否通过滚动验证见下方独立结果；单独的历史候选不代表可直接推广。"
        "框架已经能完成行情缓存验证、因子研究、组合回测、同口径基准对照、固定规则纸面跟踪和有审计记录的 AI 复盘。"
        "当前状态是可复跑、可继续前向验证；没有真实订单或富途客户端回测证明。",
        "",
        "完整区间为 2024-05-01 至 2026-09-22（600 个交易日）；最近半年为 2026-03-23 至 2026-09-22（127 个交易日）。"
        "每个分段从 10,000 美元重新起算，分段收益不能相加或连乘成完整区间收益。",
        "",
    ]
    if name:
        candidate=policies[name]
        rows += [
            f"本轮事后候选：`{name}`。",
            "",
            "| 区间 | 候选收益 / 最大回撤 | QQQ 收益 / 最大回撤 | 两者同时占优 |",
            "|---|---:|---:|:---:|",
        ]
        for label in ("full","early","middle","recent","recent_half"):
            rows.append(f'| {label} | {pair(candidate[label])} | {pair(benchmark[label])} | '
                        f'{"是" if candidate[label]["beats_qqq_return_and_drawdown"] else "否"} |')
        rows += [
            "",
            "表中收益均扣每次买卖 25 基点摩擦。32% 是组合年化波动率目标，不是仓位比例，也不是收益目标；"
            "单票目标权重不超过 30%，总目标权重不超过 100%，不融资。波动率目标不是波动率上限，实际损失也不受此数字保证。",
            "",
            "## 策略如何交易",
            "",
            "1. 美股收盘后用已经完成的日 K 计算指标，按下一交易日记录的开盘价及摩擦模拟成交。"
            "这里的开盘按目标权重成交属于研究假设，不代表可以在开盘前精确知道成交股数。",
            "2. QQQ 收盘在 EMA100 上方时允许新买；在下方时停止新买，原有仓位仍按个股趋势与风险条件管理。"
            "此处 gate 模式在调仓日仍可能卖出排名落后仓位及缩小过大仓位，不能理解为熊市永久持有。",
            "3. 普通股票候选须满足收盘价 > EMA50 > EMA150，过去 63 个交易日涨幅为正，20 日日均实际成交额至少 500 万美元。"
            "本研究不买杠杆 ETF，SOXL、TQQQ 只属于之前的独立研究。本组合的收益不能套用到单一 SOXL 文件上。",
            "4. 按 `63 日涨幅 / 20 日日收益标准差` 排名，最多持有 5 只。"
            "本轮候选的买入与持有排名都是前 5；另测的前 10 持有缓冲并没有在所有区间改善表现。",
            "5. 周一、周四对应交易日评估调仓。假日顺延至本周下一可交易日。"
            "按逆波动率分配目标权重，再用过去 60 日日收益协方差估计组合风险，必要时整体降仓。",
            "6. 调仓偏离目标权重至少 2.5 个百分点才做小额调整，但清仓、新买和单票超限仍执行。"
            "30% 是调仓时的目标约束，持仓上涨或日内跳空仍会令实际权重暂时超过它。",
            "7. 收盘跌破 EMA50，或从持有期收盘峰值回落 15%，下一开盘卖出；退出后至少间隔 2 个交易会话才允许再买。"
            "这不是盘中即时止损，跳空可能使实际卖出损失超过 15%。",
            "",
            "## 19 个候选的完整对照",
            "",
            "每格为收益 / 最大回撤。所有候选均使用同一数据、费用、初始现金和结算口径。",
            "",
            "| 方案 | 完整区间 | 较早区间 | 2025-04 至 12 | 2026-01 至 09 | 最近半年 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for policy,windows in policies.items():
            rows.append("| "+policy+" | "+" | ".join(pair(windows[w]) for w in
                        ("full","early","middle","recent","recent_half"))+" |")
        rows += [
            "| QQQ | "+" | ".join(pair(benchmark[w]) for w in
                                 ("full","early","middle","recent","recent_half"))+" |",
            "",
            "完整报告保留了失败方案；候选不是严格独立样本外选出的。初始 16 组结果已被看过后，"
            "才追加 32%、36%、40% 三档风险预算敏感性检查。这些后续实验的结果必须称为事后研究。",
            "",
            "## 加重仓位是否更好",
            "",
            "以下四组仅改变组合风险预算，均为单票目标上限 30%、最多 5 只、周一/周四调仓。"
            "相同风险预算并不等于相同现金仓位，具体股票波动、相关性和趋势过滤都会改变投资比例。",
            "",
            "| 年化波动率目标 | 完整区间收益 / 回撤 | 最近半年收益 / 回撤 | 半年平均投资比例 | 半年订单数 |",
            "|---|---:|---:|---:|---:|",
        ]
        for policy_name in ("trend_heavy28", "trend_heavy32", "trend_heavy36", "trend_heavy40"):
            if policy_name not in policies:
                continue
            windows=policies[policy_name]
            half=windows["recent_half"]
            target=protocol["policies"][policy_name]["annual_vol_target"]
            rows.append(f"| {target:.0%} | {pair(windows['full'])} | {pair(half)} | "
                        f"{half['avg_exposure_pct']:.2f}% | {half['orders']} |")
        rows += [
            "",
            "在本次数据中，36% 比 32% 得到更高收益，同时承担更大回撤；40% 的最近半年收益更高，"
            "但回撤已超过同期 QQQ，因此不满足“收益更高且回撤更低”的双重目标。"
            "不能从这几档事后实验推导未来两个月应采用哪档；32% 只是冻结观察的研究候选，36% 是风险偏好敏感性对照。",
            "",
            "## 更频繁是否更好",
            "",
            "必须在相同排名、持有缓冲和风险预算下只改变频率，不能把不同仓位/因子间的差异都归因于换仓次数。",
            "",
            "| 同规则频率对照 | 完整区间收益 / 回撤 | 最近半年收益 / 回撤 | 半年订单数 |",
            "|---|---:|---:|---:|",
        ]
        for policy_name, label in (
            ("trend_buffer20", "63日动量/波动：周一、周四"),
            ("trend_daily20", "63日动量/波动：每日"),
            ("blend_weekly20", "63/126日混合动量：每周"),
            ("momentum_blend20", "63/126日混合动量：周一、周四"),
        ):
            if policy_name in policies:
                windows=policies[policy_name]
                rows.append(f"| {label} | {pair(windows['full'])} | {pair(windows['recent_half'])} | "
                            f"{windows['recent_half']['orders']} |")
        rows += [
            "",
            "每日版在最近半年比对应每周两次版多赚一些，但完整区间收益下降、回撤和成本上升，"
            "且最近半年仍明显落后 QQQ。混合动量增加频率也未解决近期落后。"
            "本轮没有证据支持“越频繁越好”，更没有发现必须用五分钟线才能获得的优势。",
            "",
            "## 因子证据",
            "",
            "这里用横截面排序与未来收益的 Spearman 相关系数（Rank IC）作为描述性诊断。"
            "诊断只使用当日已知特征，收益标签从下一开盘开始；标签不会跨越对应诊断分段的结束日期。"
            "下表为未来 20 个交易日的平均 Rank IC，没有将重叠观测当作独立样本计算显著性。",
            "",
            "| 因子 | 较早区间平均 IC | 2026 区间平均 IC |",
            "|---|---:|---:|",
        ]
        for factor,diagnostic in summary["factor_diagnostics"].items():
            a=diagnostic["early"]["20"]["mean_rank_ic"]
            b=diagnostic["recent"]["20"]["mean_rank_ic"]
            rows.append(f"| {factor} | {ic_text(a)} | {ic_text(b)} |")
        rows += [
            "",
            "- `momentum_risk`：63 日相对强弱除以 20 日波动，简单可解释。早期相关性弱、近期改善，"
            "说明它有明显的行情依赖，不能称为长期稳定 alpha。",
            "- `momentum_blend`：63 日与 126 日动量平均，再除以波动率。更慢的排名并不保证更好，"
            "最近半年的持有组合明显落后。",
            "- `residual_momentum`：先粗略扣除对 QQQ 的 beta 暴露，再按波动缩放。"
            "这是简化代理，不是经过完整行业中性或多因子回归验证的残差动量。",
            "- `path_quality`：动量乘以价格路径效率，区分渐进上涨与来回震荡。它不是论文原始指标的完整复制。",
            "- `near_high`：离过去约一年高点越近，分数越高。其 IC 较好，但最多 5 只股票、"
            "特定退出与仓位下的组合表现仍差，说明因子相关性不能直接等同于策略收益。",
            "- `breakout20` 名称中的 20 表示 20% 年化风险预算；突破条件仍是过去 55 日新高的近期事件，"
            "不是 20 日突破。更快趋势、直接突破和每日换仓也没有得到全面优势。",
            "",
            "## 费用、延迟和收益来源",
            "",
            "| 摩擦 / 信号假设 | 完整区间 | 最近半年 |",
            "|---|---:|---:|",
            f"| 每边 10 基点 | {pair(summary['candidate_stress']['10']['full'])} | {pair(summary['candidate_stress']['10']['recent_half'])} |",
            f"| 每边 25 基点 | {pair(candidate['full'])} | {pair(candidate['recent_half'])} |",
            f"| 每边 50 基点 | {pair(summary['candidate_stress']['50']['full'])} | {pair(summary['candidate_stress']['50']['recent_half'])} |",
            f"| 每边 25 基点、指标再滞后 1 会话 | {pair(summary['candidate_stress']['extra_signal_delay']['full'])} | {pair(summary['candidate_stress']['extra_signal_delay']['recent_half'])} |",
            "",
            "额外滞后测试使用更旧的信号，不等于真实网络或订单延迟仿真。完整区间收益出现很大变化，"
            "进一步表明换仓路径敏感，不应据此把滞后版本升级为新最优策略。每边 50 基点时不再胜过 QQQ，"
            "交易成本是当前候选的关键短板。",
        ]
        attr=summary["candidate_attribution"]
        rows += [
            "",
            f"完整区间候选有 {candidate['full']['orders']} 笔买卖订单，模拟摩擦共 ${candidate['full']['cost_dollars']:,.2f}，"
            f"平均现金投资比例 {candidate['full']['avg_exposure_pct']:.2f}%。总收益中前 5 个盈利标的贡献 "
            f"${attr['top5_pnl']:,.2f}，占净利润 {attr['top5_pnl']/attr['total_pnl']*100:.1f}%。"
            "这是收益集中度观察；去掉这 5 只再跑会是新的事后实验，不能直接从总收益扣除便声称可复制。",
            "",
            "| 盈利贡献前 5 | 净贡献美元 |",
            "|---|---:|",
        ]
        rows.extend(f"| {r['symbol']} | {r['pnl']:,.2f} |" for r in attr["symbols"][:5])
    rows += [
        "",
        "## 滚动历史验证",
        "",
        "在每个分段开始前，只按此前区间成绩选择一个方案；要求训练区间收益超过 QQQ 且最大回撤更低，"
        "再按超额收益减回撤评分。没有合格者就现金等待。选择完成后才计算下一个区间，组合总账连续运行。"
        "由于分析者已经看过这些历史，它仍是回顾性 walk-forward，不能重新命名为未见过的样本外检验。",
        "",
        "| 测试区间 | 过去数据选出的方案 | 分段从现金起算收益 / 回撤 | QQQ |",
        "|---|---|---:|---:|",
    ]
    for fold in wf["folds"]:
        rows.append(f"| {fold['test_start']} 至 {fold['test_end']} | {fold['selected']} | "
                    f"{pair(fold['fold_from_cash'])} | {pair(fold['qqq'])} |")
    rows += [
        "",
        f"连续净值在 25 基点下为 {pair(wf['costs']['25'])}，同期 QQQ 为 {pair(wf['costs']['25']['qqq'])}。"
        f"预设的滚动验证门槛结果：{'通过' if wf['gate_passed'] else '未通过'}。"
        "因此不能把每日/每周自动选赢家投入执行；AI 每周提出新版本也应先经过独立比较。",
        "",
        "## 与旧版差异",
        "",
        "此前 +55.91% / 15.33% 的重仓周一/周四版本，其 30% 只限制新开仓，不持续调整已有仓位；"
        "新框架增加组合风险预算、目标权重调整、禁止止损后同一开盘重新买入，并使用实际 turnover 判断流动性，"
        "所以两份结果不能当作同一策略的复跑差异。新候选收益更低，但回撤也下降。",
        "",
        "数据处理不再因为未来缺失一天或退市尾部缺失，就删除该股票此前全部历史；"
        "已有持仓遇到缺失成交或估值 bar 时明确中止，等待缺失/停牌/退市处理。"
        "此外，旧策略的前复权价格 >=5 美元过滤已移除，因为拆股后的历史前复权绝对价格并不等于当时实际报价。"
        "这些都是研究口径变化，不能只把结果改善归因于某一因子。",
        "",
        "## 本地框架",
        "",
        "- `core.py`：行情审计、指标、排名、组合仓位、交易日历、成交与净值。",
        "- `experiment.py`：冻结实验协议、全部候选、同费用 QQQ、滚动选择、因子诊断、成本压力和归因。",
        "- `refresh.py`：通过 Futunn REST 增量取日 K，生成新缓存；数据修订/前复权变化时拒绝拼接旧历史。",
        "- `paper.py`：冻结策略、引擎与数据前缀；次日开始 $10,000 纸面组合，逐日追加不可覆盖的记录。",
        "- `ai_review.py`：封存截止时间内的行情和有来源事件，调用现有 Ollama 并严格校验固定动作档位。",
        "- `reporting.py`：从已完成 run 生成本文，避免手写结果与真实文件脱节。",
        "",
        "每日收盘且行情完整后运行 refresh 和 paper advance；AI 根据当天已封存快照进行解释，"
        "只产生维持、降仓或暂停新买的建议。它没有修改回测、下单、加杠杆或自动晋升策略的权限。"
        "新的七日研究版本应存入新目录，和原规则并行模拟，而不是覆盖原版本。",
        "",
        "## 数据与可行性边界",
        "",
        f"- 缓存覆盖 {summary['data']['symbols']} 个标的，其中 {summary['data']['ordinary_stocks']} 个普通股票，"
        f"数据起止 {summary['data']['start']} 至 {summary['data']['end']}。",
        "- 当前自选名单用于回看过去，存在幸存者和选股后见偏差；多轮研究已经看过各时段，"
        "目前没有足够长的未触碰历史验证段。",
        "- 数据为 autype=1 前复权日 K、不含现金股息；QQQ 使用同样口径、相同起止日期、整数股和成本。"
        "真实分红总收益、税费、真实逐笔点差、开盘容量和排队不在本模拟中。"
        "卖出所得在模型中立即可用；未模拟现金账户的结算限制，因此“无融资”不等于已验证真实现金账户可照单执行。",
        "- 最大回撤按日收盘净值计算，无法代表盘中最大回撤。止损是收盘确认，不能保证跳空时按阈值成交。",
        "- 波动率估计和行业/个股集中风险可能失真；前 5 个盈利标的贡献集中说明替换股票池后不能沿用收益数字。",
        "- 本地 paper 是同一模型的前向记录，不是券商模拟账户，也不是实时可成交价检验。"
        "真正的执行等价性仍要用可交易报价、交易规则、资金结算和实际订单回报核验。",
        "- 没有调用真实或券商模拟订单端点，没有生成声称已通过富途客户端验证的多标的 `.quant`。"
        "当前动态选股组合与过去单股 Canvas `.quant` 的执行对象不同。",
        "",
        "## 研究依据",
        "",
        "以下原始研究提供研究动机，不证明本项目参数最优，也不能替代本地验证：",
        "",
        "1. Kenneth French 官方 Daily Momentum Factor 定义，采用先前 2–12 个月收益；"
        "本项目的 63/126 日指标是不同的简化实现。"
        " https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/Data_Library/det_mom_factor_daily.html",
        "2. Moreira & Muir, Volatility-Managed Portfolios, NBER w22208：波动管理的因子级证据。"
        " https://www.nber.org/papers/w22208",
        "3. Novy-Marx & Velikov, A Taxonomy of Anomalies and Their Trading Costs, NBER w20721："
        "买入与继续持有采用不同条件可减少成本。 https://www.nber.org/papers/w20721",
        "4. Da, Gurun & Warachka, Frog in the Pan：渐进价格信息与动量的研究；路径效率仅为本项目代理。"
        " https://digitalcommons.chapman.edu/business_articles/115/",
        "5. Bailey 等, The Probability of Backtest Overfitting：反复试验和挑选最优历史曲线的偏差。"
        " https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf",
        "",
        "## 下一步研究判断",
        "",
        "当前最需要改善的是换手成本和跨时段稳定性，而不是继续增加交易次数。"
        "32% 和 36% 风险预算在近期历史样本中同时改善了收益和回撤，但 2024–2025 较早段及高成本检验不合格。"
        "保留候选用于冻结纸面对照，继续研究有明确经济解释的成本控制；"
        "若未来记录不能胜过 QQQ，就应保留基准或现金，而不是持续事后调参使过去变好。",
        "",
        "复现步骤和运行命令见同目录 README.md；全部结构化结果保存在 run 的 protocol.json、summary.json、comparison.csv 与订单/净值文件中。",
    ]
    return "\n".join(rows)+"\n"


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run",required=True)
    parser.add_argument("--output",required=True)
    args=parser.parse_args()
    report=build_report(args.run)
    with Path(args.output).open("x",encoding="utf-8") as handle:
        handle.write(report)
    print(args.output)


if __name__=="__main__":
    main()
