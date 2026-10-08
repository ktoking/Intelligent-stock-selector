# Daily Factor Lab

本地美股日 K 因子、组合回测、固定规则纸面账本与 AI 复盘框架。
研究使用当前自选池的历史价格；不会调用任何下单接口，不依赖 OpenD。
它不是券商模拟账户，也不是已通过富途客户端验证的多标的 `.quant`。

## 当前状态

- 19 个方案、5 个固定历史区间、同费用 QQQ、10/25/50 基点压力和逐段只用过去信息选策略。
- `trend_heavy32` 在完整区间和最近半年同时优于 QQQ 收益与回撤，但较早区间、高成本和自动换版本的滚动验证未通过。
- `paper.py` 可以冻结规则、数据和引擎，日后追加模拟净值，拒绝改写过去记录。
- `ai_review.py` 保存时间截止明确的来源、原始模型输出、结构校验和引用；AI 没有改变冻结规则的权限。
- 详细数字见本目录 `RESEARCH_20260923.md`。所有历史研究都存在当前自选池后见偏差。
- 本次交付运行：`outputs/daily_factor_lab/20260923_delivery/`；运行证据与未完成项见 `VERIFICATION_20260923.md`。
- 后续成本/动态因子研究：`COST_ADAPTIVE_20260923.md`。新增9个固定假设，保留全部19个旧方案；
  每周低换手版本改善完整区间但近期落后，动态因子未通过门槛，未替换冻结候选。
- 条件化因子研究：`CONDITIONAL_FACTOR_20260923.md`。再加5个固定方案，总计33组。
  新方案仍未通过门槛；修复了新排名层的浮点并列排序问题，严格历史前缀检查保留。

## 环境

所有命令从仓库根目录 `/Users/kaiyi.wang/PycharmProjects/stock-agent` 执行。

```bash
.venv/bin/python -m pip install -r research/daily_factor_lab/requirements.txt
.venv/bin/python -m pytest -q \
  tests/test_daily_factor_lab_core.py \
  tests/test_daily_factor_ai_review.py \
  tests/test_daily_factor_paper.py \
  tests/test_daily_factor_refresh.py \
  tests/test_daily_factor_reporting.py
```

历史缓存为 `outputs/watchlist_daily_trend_20260923/`，包含本机富途 REST 读回的日 K。
缓存和运行产物不加入 Git，防止大量历史结果再次撑大差异；源码、策略配置和报告保留。
行情刷新额外复用项目已有的 `cryptography` 与 `research.tqqq_intraday.signed_get`。

## 复跑研究

输出目录必须尚未存在，避免覆盖实验记录。

```bash
.venv/bin/python -m research.daily_factor_lab run \
  --risk-sensitivity \
  --output outputs/daily_factor_lab/my_new_run

.venv/bin/python -m research.daily_factor_lab.reporting \
  --run outputs/daily_factor_lab/my_new_run \
  --output outputs/daily_factor_lab/my_new_run/RESEARCH.md
```

研究运行前保存 `protocol.json`，其中有全部规则、分段、费用和源代码/数据哈希；
只有曲线、表格和结构化结果全部完成才生成 `completed.json`。
19 组中后 3 组是看到初始结果后追加的风险预算敏感性研究，协议明确记录这一点。

关键产物：

| 文件 | 内容 |
|---|---|
| `protocol.json` | 冻结实验假设、源代码和数据哈希 |
| `summary.json` | 全部绩效、基准、滚动检验、压力测试、因子诊断和归因 |
| `comparison.csv` | 所有方案的全部区间，便于筛选核对 |
| `candidate_policy.json` | 事后研究候选，不表示通过发布门槛 |
| `candidate_full_orders.csv` | 完整区间模拟订单、原因、单笔摩擦 |
| `walk_forward_*` | 连续滚动选择的订单和净值 |
| `equity_drawdown.png` | 2025-04-01 起的滚动选择、固定候选与 QQQ 净值/回撤；不是 2024 起完整区间图 |

本地审计不仅复算摘要，还从 937 笔历史订单独立重建每一天现金、股数和净值：

```bash
.venv/bin/python -m scripts.verify_daily_factor_run \
  --run outputs/daily_factor_lab/20260923_delivery \
  --paper-demo outputs/daily_factor_lab/paper_replay_demo_20260923 \
  --ai-review outputs/daily_factor_lab/20260923_v2/ai_review_second_2026-09-22.json \
  --output outputs/daily_factor_lab/verification_new.json
```

源码或行情改变时审计会拒绝把它当作原实验的复跑。此审计通过不代表策略推广门槛通过。
依赖的实测版本保存在 `outputs/daily_factor_lab/verification_20260923.json`。

## 每日数据更新

使用现有 Futunn AppKey，密钥只从仓库外文件读取。需在当前 shell 设置
`FUTUNN_APP_KEY` 和 `FUTUNN_PRIVATE_KEY_FILE`，不把私钥写入代码或结果。

```bash
.venv/bin/python -m research.daily_factor_lab.refresh \
  --source outputs/watchlist_daily_trend_20260923 \
  --output outputs/daily_factor_lab/cache_20260923
```

默认只取最后一个已经完成的纽约交易日。每个请求检查 `ret_code=0` 和末日完整性；
按原股票池生成新缓存，保留旧缓存。以后把 `--source` 指向上次成功生成的完整缓存。
若有历史价格修订、复权变化、缺失或分页风险，本次刷新明确失败，不能用它推进纸面账本。
`--symbols US.QQQ US.AAPL` 仅用于两只标的的接口诊断，不能替代整个组合的完整缓存。

## 未来纸面跟踪

从最近已收盘日开始冻结策略，初始模拟资金 10,000 美元。执行模型与回测相同，
使用记录的下一开盘价加摩擦；这仍不是开盘前已知报价或真实可成交量证明。

```bash
.venv/bin/python -m research.daily_factor_lab.paper init \
  --directory outputs/daily_factor_lab/paper_my_version \
  --policy-file research/daily_factor_lab/policies/trend_heavy32.json \
  --asof 2026-09-22
```

本次已创建 `outputs/daily_factor_lab/paper_candidate_20260923/`，
起始会话为 2026-09-23，当前是 `awaiting_first_session`，不存在虚构成交。
新交易日收盘且完整行情刷新后执行：

```bash
.venv/bin/python -m research.daily_factor_lab.paper advance \
  --directory outputs/daily_factor_lab/paper_candidate_20260923 \
  --data-dir outputs/daily_factor_lab/cache_20260923 \
  --asof 2026-09-23
```

如果该日尚未收盘、没有数据或旧数据发生修订，命令会拒绝封存。
每次会核对以前所有已封存订单和净值前缀；同一天不能覆盖重写。
策略和引擎变化应新建版本。当前股票池冻结，后续新增自选股不会悄悄加入旧版本。
历史实例 `paper_replay_demo_20260923` 只演示分段推进功能，不算真实前向跟踪。

## 每日 AI 复盘

可以直接使用已封存的 paper `session_日期.json` 作快照。
也可生成一个清楚标记为历史重放的候选快照：

```bash
.venv/bin/python -m research.daily_factor_lab snapshot \
  --asof 2026-09-22 \
  --policy-file research/daily_factor_lab/policies/trend_heavy32.json \
  --research-summary outputs/daily_factor_lab/20260923_delivery/summary.json \
  --output outputs/daily_factor_lab/snapshot_new.json
```

事件文件是 JSON 数组。每条事件包含 `source_url`、带时区的 `published_at`，
以及 `title` 或 `text`；有历史可用时间时再提供 `available_at`。
未来事件和无来源事件会被剔除并记录原因；当前没有接入新闻抓取或历史事件档案。
空事件数组代表只分析已有行情和策略，不生成“已分析突发新闻”的说法。

```bash
.venv/bin/python -m research.daily_factor_lab.ai_review \
  --snapshot outputs/daily_factor_lab/snapshot_new.json \
  --events research/daily_factor_lab/events.empty.json \
  --asof 2026-09-23T00:00:00+00:00 \
  --output outputs/daily_factor_lab/review_new.json \
  --call-llm
```

`--call-llm` 调用本机已配置的 Ollama，默认 `gemma4:31b-cloud`，
实际推理可能在 Ollama 云端。省略此参数只生成待复盘包，不假装得到 AI 建议。
模型只允许 `maintain`、`reduce`、`pause_new`，比例只能为 1、0.75、0.5、0。
非默认动作必须引用包内证据，任何输出解析或结构失败都会记录为无效，不自动采纳。
`validated_llm_advice` 仅表示结构、固定档位和引用 ID 校验通过，不代表理由正确。
本次实际模型返回“维持”，但理由没有充分讨论已经失败的滚动验证和成本压力；记录保留，
未采纳为交易动作。第一次调用的格式失败记录也保留，没有只保存成功回复。

AI 建议没有被追溯性地塞进历史收益。要评价 AI，必须从现在开始保存它实际生成的建议、
人工采纳情况及之后的模拟结果，并保留未采用 AI 的同步基线。

## 每 7 天的复盘

检查净值相对 QQQ、最大回撤、交易成本、盈利集中度、漏掉的行情和 AI 建议是否有用。
`paper` 在跨过每 5 个交易会话时标记一次周复盘参考；它不是后台定时器。
新规则放入新 policy 文件和新的 run 目录；重新回测、独立纸面观察后再考虑替代旧规则。
当前没有创建后台定时任务，没有启用下单。

## 成本与动态因子研究扩展

```bash
.venv/bin/python -m research.watchlist_adaptive_cost \
  --output outputs/daily_factor_lab/adaptive_cost_new_run

.venv/bin/python -m scripts.review_watchlist_adaptive_cost \
  --run outputs/daily_factor_lab/adaptive_cost_new_run \
  --audit outputs/daily_factor_lab/adaptive_cost_new_audit.json \
  --report outputs/daily_factor_lab/adaptive_cost_new_report.md

.venv/bin/python -m pytest -q tests/test_watchlist_adaptive_cost.py
```

扩展研究复用未修改的成交引擎，保存已兑现因子IC和每一天的因子权重。
审计会核对原19组结果、100组新窗口指标、10份订单账本，以及截断真实历史后的因果不变性。
`adaptive_*` 和 `static_blend*` 的新排名层目前仅在研究适配器内运行，
不能只复制其中的 `Policy` 到旧 `paper.py`，否则会丢失排名层而变成另一套策略。
当前没有把这些未通过门槛的方法接入每日纸面实例。

条件化研究把评价样本限制在可买趋势股，或用实际扣费参考组合的已实现净收益评价因子：

```bash
.venv/bin/python -m research.watchlist_conditional_factor \
  --output outputs/daily_factor_lab/conditional_new_run
.venv/bin/python -m pytest -q tests/test_watchlist_conditional_factor.py
```

成功运行包含旧28组全部指标的一致性核对、新5组及3个参考组合的账本核对、
6组真实数据截断检查和文件哈希。原始 `conditional_factor_20260923_v1` 因浮点并列排序
导致前缀检查失败，不是有效交付；修复后的运行是 `conditional_factor_20260923_v2`。
所有动态排名变体仍只用于研究，不可绕过适配器直接放入旧 paper。

## 已知边界

当前自选名单回看历史会产生选择偏差；数据不含现金股息，整数前复权股数不等于历史实际拆股前股数。
最大回撤为收盘口径，无法代表盘中最坏损失。缺失持仓行情必须先解决，不能静默按旧价持有。
历史模拟按开盘价反推股数，生产执行还需要独立的价格、结算、部分成交和订单回报对账。
卖出所得在当前模型中立即可用于买入；未实现现金账户结算资金限制。
单股富途 Canvas 文件不能等价表示这个多标的动态选股组合，本框架不会伪装成已验证的 `.quant`。
