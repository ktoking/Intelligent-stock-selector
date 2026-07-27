# OKX 日内模拟盘 Agent

当前系统只连接 OKX Demo 账户。系统由一个 `OKX Runtime` 统一守护，不依赖 Hermes、crontab，也不需要为 10 分钟、5 分钟、1 分钟分别建立定时任务。

## 执行链

1. `okx_intraday_agent.py` 在每小时 `00/10/20/30/40/50` 分扫描全池，按量比、ATR、突破距离、EMA、VWAP、事件风险和点差分别生成多头/空头排名，保留前 10 名，候选有效期 20 分钟。
   公开行情可用不代表 Demo 私有交易可用；扫描器会调用 `max-size` 缓存验证，不支持的美股代币不会进入执行候选。
2. `okx_candidate_ws.py` 常驻读取候选。OKX 美股代币公共 WebSocket 提供 ticker 与五档盘口，但这些合约没有 candle 频道，因此执行器在每个收盘分钟通过 REST 读取 5m/1m 已完成 K 线。
3. 5m 必须保持原方向的 EMA、VWAP 和量比，否则剔除。
4. 1m 必须收在原突破位外侧，并通过点差、五档深度、预估滑点和账户风控，才提交 Demo 市价单。
5. 市价入场单附带交易所 mark price 止损/止盈；止损距离为 `max(1.2 ATR, 0.2%)`，目标为 1.8R。
6. SeaTalk 只在实际成交后发送；无成交不播报。日终复盘由独立任务发送。

## 风控边界

- 凭证只从 `~/.okx/config.toml` 的 `demo` Profile 读取，私有请求始终带 `x-simulated-trading: 1`。
- 默认 3 倍杠杆，硬上限 10 倍。
- 每笔风险预算为 `min(25 USDT, 账户权益 × 0.25%)`；名义金额默认最多 250 USDT，且不超过权益的 10%。
- 默认最多同时 2 个仓位；同一标的禁止加仓和同时持有多空；同标的成交后冷却 30 分钟。
- 默认最大点差 25 bps、最大预估滑点 35 bps、日亏损 50 USDT 时停止新开仓。
- Demo 默认 `OKX_INTRADAY_SESSION=24x7`，允许美股代币在盘前盘后继续测试；设置为 `us_cash` 可恢复仅 09:30–16:00（纽约时间）入场。
- 本地面板的 kill switch 可立即停止新开仓，不会自动平掉已有仓位。
- 本地面板的监听开关会停止/恢复扫描器与行情执行器；暂停期间不会产生新候选或新订单。已有仓位仍由 OKX 上已经生效的止损止盈保护。
- LLM 只用于复盘和解释，不参与下单批准或覆盖风控。

主要环境变量：

```dotenv
OKX_INTRADAY_PROFILE=demo
OKX_INTRADAY_LEVERAGE=3
OKX_INTRADAY_RISK_USDT=25
OKX_INTRADAY_RISK_FRACTION=0.0025
OKX_INTRADAY_MAX_NOTIONAL_USDT=250
OKX_INTRADAY_MAX_SPREAD_BPS=25
OKX_INTRADAY_MAX_SLIPPAGE_BPS=35
OKX_INTRADAY_MAX_OPEN_POSITIONS=2
OKX_INTRADAY_EXECUTE_DEMO=1
```

## 运行与调度

- 唯一系统入口是 `com.stock-agent.okx-runtime` LaunchAgent。
- Runtime 内部守护扫描器：每 10 分钟全池发现。
- Runtime 内部守护执行器：常驻 5m/1m 复核、实时盘口和 Demo 执行。
- Runtime 每 10 分钟检查一次美股收盘复盘；复盘脚本按纽约交易日保证每天只发送一次。
- 任一子进程异常退出时 Runtime 会单独重启，不影响另一个子进程。
- Hermes 仅保留非交易类的每日市场方向简报，不参与 OKX 行情、风控或订单调度。
- 面板：`http://127.0.0.1:8000/okx/dashboard`。

执行状态写入 `data/okx_execution_state.json`，行情快照写入 `data/okx_candidate_ws.json`。这两个文件是运行态数据，不存储凭证。
