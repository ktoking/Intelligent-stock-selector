# SOXL / SOXS 趋势切换三个月研究

- 研究区间：2026-06-22 ～ 2026-09-18
- 数据：真实 ETF 小时线，QQQ 产生信号，下一根开盘成交
- 成本：单边 10.0 bp；SOXL/SOXS 同时最多持有一个
- 选中参数：`momentum_D1_f0_s0_h2_b200_p1_n1_r95`
- 最近42日 Primary：收益 33.3035%，最大回撤 20.9052%，PF 2.2458，交易 3 笔
- 前21日 backward validation：收益 3.1058%，最大回撤 18.0273%，PF 1.161，交易 6 笔
- 完整区间：收益 37.444%，最大回撤 20.9001%，PF 1.7993，胜率 55.56%，交易 9 笔
- 最近21日诊断（已包含在 Primary，不是 OOS）：收益 0.0%，最大回撤 0.0%，PF None，交易 0 笔
- 全样本峰值（不可直接采用）：`momentum_D1_f0_s0_h2_b200_p2_n1_r95`，收益 48.9183%，最大回撤 26.357%
- 建议状态：**SHADOW**；`promotion_passed=False`

`.quant` 默认 `execution_enabled=False`。在 moomoo 回测时可手动改为 True；实盘前继续保持 Shadow 验证。

导入文件为富途原生 Protobuf 二进制代码策略容器；可审阅源码保存在同目录的
`SOXL_SOXS_QQQ_TREND_SWITCH_V1.py`。不要把 `.py` 改扩展名后导入。
