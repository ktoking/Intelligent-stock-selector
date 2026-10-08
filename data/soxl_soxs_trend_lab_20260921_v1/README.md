# SOXL / SOXS 趋势切换三个月研究

- 研究区间：2026-06-22 ～ 2026-09-18
- 数据：真实 ETF 小时线，QQQ 产生信号，下一根开盘成交
- 成本：单边 10.0 bp；SOXL/SOXS 同时最多持有一个
- 选中参数：`ema_f10_s12_h0_b50_p1`
- 完整区间：收益 0.0%，最大回撤 0.0%，PF None，胜率 None%，交易 0 笔
- 最终留出区间：收益 0.0%，最大回撤 0.0%，PF None，交易 0 笔
- 全样本峰值（不可直接采用）：`ema_f20_s24_h0_b30_p1`，收益 11.0538%，最大回撤 58.6458%
- 建议状态：**SHADOW**；`promotion_passed=False`

`.quant` 默认 `execution_enabled=False`。在 moomoo 回测时可手动改为 True；实盘前继续保持 Shadow 验证。
