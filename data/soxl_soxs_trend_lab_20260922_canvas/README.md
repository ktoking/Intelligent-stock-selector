# SOXL / SOXS QQQ Trend Switch V2 Canvas

此文件基于用户已验证可导入的
`SOXL_ADAPTIVE_RECOVERY_V5_BETA_OPTIMIZED.quant` 原生 Canvas 容器生成。

## 导入文件

- `SOXL_SOXS_QQQ_TREND_SWITCH_V2_CANVAS.quant`
- SHA-256: `42dc00e4bc48a4c64d8c61373121eebb55e3c59b21d9f66d58987a36fe3dda6c`

## 富途运行参数

- `trading_symbol`: `US.SOXL`
- `ref_symbol`: `US.QQQ`
- `EXECUTION_ENABLED`: 默认 `0`，仅产生信号与提醒，不下单
- 回测时需要验证下单逻辑，可手动改为 `1`

策略以 QQQ 已完成日线的两日动量为信号：大于 `+2%` 选择 SOXL，小于
`-2%` 选择 SOXS，中性区间退出到现金。同一时间只允许持有其中一个。

## 结构校验

- `StrategyType_Canvas = 1`
- `startCardId = 1`
- 卡片：开始卡 + 自编码动作卡
- 连线：开始卡 -> 自编码动作卡
- 与参考文件相同的 Canvas version、261 项 function version、4 项指标元数据
- Protobuf `decode_raw`: PASS
- 自编码动作 Python 包装编译：PASS
- 项目测试：3 passed

尚未获得富途 UI 的实际导入成功回执；本机富途主窗口无法被辅助功能自动化绑定。
