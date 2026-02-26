# 富途量化策略（独立脚本）

本目录为**独立于主项目的富途量化脚本**，不依赖 stock-agent 的代码、配置或数据文件。  
买卖规则**参考**主项目技术文档（MA/MACD/KDJ/RSI/ATR、入场离场逻辑），在策略内**自实现**，便于在富途牛牛 / 富途 OpenAPI 策略编辑器中直接使用。

---

## 文件说明

| 文件 | 说明 |
|------|------|
| **strategy_technical.py** | 技术面买卖策略：**所有信号由富途多类 API 计算**（不只 ma）。MA：ma(), is_ma_bullish_alignment, is_ma_bearish_alignment, atr()；KDJ：is_kdj_golden_cross, is_kdj_death_cross, is_kdj_top_divergence, is_kdj_bottom_divergence, kdj_k/kdj_d/kdj_j。买入：MA 多头/站上 MA20 或 KDJ 金叉/底背离/超卖；卖出：跌破 MA20/MA60、MA 空头、ATR 止损或 KDJ 死叉/顶背离/超买。无需填写任何全局变量。 |
| **strategy_report_signal.py** | 精简版：仅「当前价 ≤ 减仓价 → 卖出」「当前价 ≤ 买入参考价 → 买入」两档提醒，适合只盯固定价位时使用。 |
| **技术规则参考.md** | 指标公式与买卖条件摘要（与主项目 `docs/技术指标说明与智能体提示词.md` 一致），供在富途里扩展 K 线/自编指标时对照。 |
| **README.md** | 本说明。 |

---

## 使用步骤（以 strategy_technical.py 为例）

1. 在富途策略编辑器中新建「证券」类型策略，将 `strategy_technical.py` 内容粘贴进去。  
2. **驱动标的**：填要监控的股票代码（如 AAPL.US、00700.HK，具体格式以富途为准）。  
3. **无需填写全局变量**：减仓/买入参考价 = MA20，止损参考价 = MA20，ATR = atr(14)，均由 **ma() / atr()** 在运行时计算。  
4. **K 线周期**：脚本内默认 `BarType.D1`（日线）；若平台枚举不同或需用 1 小时线，将 `self.K线周期 = BarType.D1` 改为 `BarType.H1` 等。  
5. 若平台 **atr()** 不存在或参数不同，请按实际接口修改 `_get_atr()`，或 ATR 止损分支将自动跳过。  
6. 运行策略后，满足条件会推送对应提醒。

---

## 与主项目的关系

- **无依赖**：不 import 主项目、不读 recommendations.jsonl 或任何报告输出。  
- **规则参考**：买卖条件与主项目技术文档一致（跌破 MA20/MA60、ATR 止损、固定减仓价；站上参考价入场），便于你「按同一套技术逻辑」在富途里做提醒或后续下单。  
- 若主项目日后更新技术规则，可同步更新本目录下的策略逻辑与 `技术规则参考.md`。

---

## 注意事项

- 若平台仅支持 `GlobalType.INT`：价格类参数可用「元×100」存（如 185.50 → 18550），比较时除以 100.0。  
- `current_price(..., price_type=THType.ALL)` 以富途文档为准，必要时改为最新价/现价等枚举。  
- 本策略仅做**提醒**，不含下单；若需自动下单，需在富途侧查阅交易 API 并自行在策略中调用。
