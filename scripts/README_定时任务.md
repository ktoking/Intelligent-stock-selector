# 每日定时报告配置说明

每日 9 点自动跑三份报告（美股 SP500、A股中证2000、港股恒指），需满足：

1. **服务已启动**：`daily_report.py` 通过 HTTP 请求 `/report` 接口，必须先启动 `server.py`
2. **定时任务已配置**：crontab 或 macOS LaunchAgent

---

## 方式一：macOS LaunchAgent（推荐）

项目已提供 plist 模板，需先**修改路径**（若项目不在默认位置）：

```bash
# 1. 复制到 LaunchAgents（路径按需修改）
PROJECT=/Users/kaiyi.wang/PycharmProjects/stock-agent
cp "$PROJECT/scripts/com.stock-agent.daily.plist" ~/Library/LaunchAgents/
cp "$PROJECT/scripts/com.stock-agent.server.plist" ~/Library/LaunchAgents/

# 2. 若项目路径不同，编辑 plist 中的路径
#    ProgramArguments、WorkingDirectory、StandardOutPath、StandardErrorPath

# 3. 加载并启动
launchctl load ~/Library/LaunchAgents/com.stock-agent.server.plist   # 常驻服务
launchctl load ~/Library/LaunchAgents/com.stock-agent.daily.plist    # 每日 9 点

# 查看状态
launchctl list | grep stock-agent

# 停止/卸载
launchctl unload ~/Library/LaunchAgents/com.stock-agent.daily.plist
launchctl unload ~/Library/LaunchAgents/com.stock-agent.server.plist
```

- **server**：登录后自动启动并保持运行，崩溃会自动重启
- **daily**：每天 9:00 执行一次（周末/节假日由脚本内部跳过）

日志：`report/output/daily_report.log`、`daily_report.err`

---

## 方式二：Crontab

```bash
# 编辑 crontab
crontab -e

# 添加（路径按需修改）：
0 9 * * * cd /Users/kaiyi.wang/PycharmProjects/stock-agent && /opt/homebrew/bin/python3 scripts/daily_report.py >> report/output/daily_report.log 2>&1
```

**注意**：cron 执行时 `server.py` 必须已在运行，可用 `screen`/`tmux` 或 LaunchAgent 常驻。

---

## 手动测试

```bash
# 测试每日脚本（会跳过周末/节假日，可用 --force 强制）
python scripts/daily_report.py --force

# 快速测试（每份只跑 2 只）
python scripts/daily_report.py --test --force
```
