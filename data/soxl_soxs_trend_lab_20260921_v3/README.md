# SOXL / SOXS 趋势切换三个月研究

- 研究区间：2026-06-22 ～ 2026-09-18
- 数据：真实 ETF 小时线，QQQ 产生信号，下一根开盘成交
- 成本：单边 10.0 bp；SOXL/SOXS 同时最多持有一个
- 选中参数：`momentum_D1_f0_s0_h2_b200_p2_n1_r95`
- 最近42日 Primary：收益 36.0905%，最大回撤 7.158%，PF None，交易 2 笔
- 前21日 backward validation：收益 9.4159%，最大回撤 26.357%，PF 1.9347，交易 2 笔
- 完整区间：收益 48.9183%，最大回撤 26.357%，PF 5.8562，胜率 75.0%，交易 4 笔
- 最终留出区间：收益 0.0%，最大回撤 0.0%，PF None，交易 0 笔
- 全样本峰值（不可直接采用）：`momentum_D1_f0_s0_h2_b200_p2_n1_r95`，收益 48.9183%，最大回撤 26.357%
- 建议状态：**SHADOW**；`promotion_passed=False`

`.quant` 默认 `execution_enabled=False`。在 moomoo 回测时可手动改为 True；实盘前继续保持 Shadow 验证。
