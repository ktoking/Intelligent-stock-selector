# 本地框架交付验证

日期：2026-09-23。结论：**本地计算与审计通过；策略稳健性门槛未通过。**
这两个结论不相互替代。没有 OpenD、富途 GUI、真实或券商模拟订单调用。

## 实际执行及读回证据

| 检查 | 状态 | 证据 |
|---|---|---|
| 19 组策略、5 个区间重跑 | PASS | `outputs/daily_factor_lab/20260923_delivery/completed.json`；计算约 11.48 秒，不含下载行情 |
| 本模块测试 | PASS | 5 个测试文件，`36 passed in 0.93s` |
| 语法编译 | PASS | `compileall` 返回 0，含研究模块及独立审计脚本 |
| 源码与数据一致性 | PASS | 当前源码、行情 SHA256 与交付 run 冻结记录匹配 |
| 候选五区间及六组压力结果 | PASS | `outputs/daily_factor_lab/verification_20260923.json` |
| 连续滚动组合三档成本复跑 | PASS | 同上；验证的是复算一致，不是胜过基准 |
| 独立订单账本 | PASS | 937 笔买卖，逐日现金/股数/净值核对；末值 $20,323.2062，现金 $508.5462 |
| 两阶段历史 paper 追加 | PASS | 先封存至 2026-06-30，再到 2026-09-22；前缀订单和净值完全一致 |
| Futunn REST 历史接口 | PARTIAL | AAPL、QQQ 两只增量读取成功，`ret_code=0`；本轮未重新拉取完整 220 只 |
| 实际 AI 调用 | PARTIAL | 第二次返回通过结构校验；建议质量不足，未采纳 |
| 未见数据的未来收益 | NOT YET | 2026-09-23 纸面实例只建档，尚无完成交易会话 |
| 稳健性推广门槛 | FAIL | 完整 walk-forward 收益低于 QQQ、回撤更高；高成本候选也失败 |
| 富途客户端导入/运行 | NOT TESTED | 本次交付是本地多标的框架，不是已验证 `.quant` |

## 复现命令

从仓库根目录执行；新输出文件/目录不得与已有记录重名。

```bash
.venv/bin/python -m pytest -q \
  tests/test_daily_factor_lab_core.py \
  tests/test_daily_factor_ai_review.py \
  tests/test_daily_factor_paper.py \
  tests/test_daily_factor_refresh.py \
  tests/test_daily_factor_reporting.py

.venv/bin/python -m compileall -q \
  research/daily_factor_lab scripts/verify_daily_factor_run.py

.venv/bin/python -m research.daily_factor_lab run \
  --risk-sensitivity --output outputs/daily_factor_lab/my_reproduction

.venv/bin/python -m scripts.verify_daily_factor_run \
  --run outputs/daily_factor_lab/20260923_delivery \
  --paper-demo outputs/daily_factor_lab/paper_replay_demo_20260923 \
  --ai-review outputs/daily_factor_lab/20260923_v2/ai_review_second_2026-09-22.json \
  --output outputs/daily_factor_lab/my_verification.json
```

## 纸面账本到底证明了什么

历史演示从 2026-03-20 冻结当时已经有行情的 207 个标的，2026-03-23 起算。
后出现的 13 个标的不会自动进入冻结实例。先推进到 6 月末，再追加到 9 月 22 日，
最终为 $12,864.7292、166 笔订单；之前的 69 个会话记录没有改写。
它证明追加和账本逻辑可运行，**不证明这些决策曾在过去实时产生**。

另有 `outputs/daily_factor_lab/paper_candidate_20260923/manifest.json`：
以 2026-09-22 收盘数据冻结 220 个标的，下一会话为 2026-09-23，
状态 `awaiting_first_session`，没有虚构成交。该实例目前只代表规则基线；
没有把 AI 建议自动接入仓位，也没有启动后台定时器。

## 实际 AI 复盘

模型：`gemma4:31b-cloud`，通过现有本机 Ollama 调用，实际推理在云端。
输入是一份历史重放组合快照及研究风险证据，事件数组为空，没有新闻和突发事件输入。

- 首次调用：返回格式不符合严格结构，保留无效记录。
- 第二次调用：`maintain`、乘数 1.0、引用 `market_snapshot`，结构校验通过。
- 内容缺陷：它依据 QQQ 高于均线和当前权重分布声称运行稳定，却没有充分处理已提供的
  walk-forward 失败和高摩擦失效证据。不能把“JSON 合法”当作“投资判断可靠”。
- 没有改动策略，没有采纳为交易动作，没有生成“AI 提高历史收益”的数字。

原始输出、实际请求、输入时间截止、引用 ID 和 SHA256 全部保留在
`outputs/daily_factor_lab/20260923_v2/ai_review_second_2026-09-22.json`。
未来评估 AI 需要同步保留未采用 AI 的规则基线及每条实际建议/人工决定，不能用今天的模型回写过去。

## 仍未解决的风险

当前自选池回看历史有幸存者/选股后见偏差，且历史已经被反复查看。
数据是富途前复权价格序列，不是包含现金分红的总收益。
模型允许卖出现金立即再用，未验证真实账户结算和开盘成交；
复权单位整数股也不等同于拆股前的实际历史股数。
没有点时新闻归档、未触碰的独立市场样本或券商成交回报，因此不能称为实盘验证。

源码、测试、配置和报告可纳入版本管理；行情缓存及运行产物位于忽略目录。
未提交或改动用户已有其他模块的工作。
全仓库 `git diff --check` 发现已有 `soxl_regime` 与视频文档的空行/尾随空格；
这些文件不在本次改动范围内，未替用户清理，不能报告为全仓库检查通过。
