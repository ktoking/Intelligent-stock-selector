---
name: stock-agent-report
description: >-
  When the user asks to run the local stock-agent report (e.g. 帮我跑纳指100报告), first ensure
  http://127.0.0.1:8000 is up (start python server.py from the project root if /health fails), then
  call GET /report with query parameters taken from the user's wording; prefer today's HTML under
  report/output/ before long reruns; map 深度分析单股 to deep=1&tickers=.
---

# stock-agent 本地 Report（OpenClaw）

项目根目录：`/Users/kaiyi.wang/PycharmProjects/stock-agent`  
服务地址：`http://127.0.0.1:8000` · 健康检查：`GET /health` · 报告接口：`GET /report`（实现见仓库 `server.py`）

---

## 固定执行顺序（例如用户说「帮我跑纳指100报告」）

按下面顺序做，**不要跳过「服务是否已启动」**。

### A. 今日是否已有报告（长任务前优先）

保存目录：`report/output/`，文件名：`report-MMDD-HHMM.html`。

```bash
d=/Users/kaiyi.wang/PycharmProjects/stock-agent/report/output
find "$d" -maxdepth 1 -name "report-$(date +%m%d)-*.html" 2>/dev/null | sort -r | head -10
```
（避免 zsh 下「无匹配」直接报错；也可用 `bash -c 'ls -t ...'`。）

- 有输出：把**最新一条**的绝对路径告诉用户；若用户**没说要重新跑**，先别调 `/report`，直接打开或复述路径（`open "路径"`）。
- 用户明确要**重新生成**，或今天**没有**文件：继续 B。

### B. 本地服务必须先就绪（未启动则启动）

1. 检查：

```bash
curl -sf http://127.0.0.1:8000/health >/dev/null && echo OK || echo FAIL
```

2. 若为 FAIL：在项目根后台启动，并**轮询直到** `/health` 成功（最多约 30～60 秒）：

```bash
cd /Users/kaiyi.wang/PycharmProjects/stock-agent
test -f .venv/bin/activate && . .venv/bin/activate
nohup python server.py >> /tmp/stock-agent-server.log 2>&1 &
for i in $(seq 1 45); do curl -sf http://127.0.0.1:8000/health >/dev/null && break; sleep 1; done
curl -sf http://127.0.0.1:8000/health >/dev/null || echo "仍无法连通，请查看 /tmp/stock-agent-server.log"
```

（若本机不用 `nohup`，可用等价的后台方式；核心是**同一端口 8000**、**确认 /health 成功后再调报告**。）

3. 确保 LLM/Ollama 等依赖可用（见项目 `.env.local`、`README`）。

### C. 调 `/report`：**参数以用户本次说的为准**

用浏览器或 `curl` 请求 **`GET /report`**，查询串从用户输入映射，**未提到的项用下表默认**。

| 用户意图 | 典型参数 |
|----------|----------|
| **纳指 100 / NASDAQ100 批量（常规）** | `market=us` `pool=nasdaq100` `limit=`（用户说几只就几只，没说则 **100**） `deep=0` `interval=1d` `save_output=1` |
| **用户说了「深度」且指定股票** | `tickers=`（逗号分隔） `deep=1` `market=us`（或对应市场） `interval=1d` `save_output=1` |
| **用户指定了 limit / deep / interval / tickers / prepost** | 严格按用户说的拼进 URL |

示例（用户只说「跑纳指100报告」、未提数量时）：

`http://127.0.0.1:8000/report?market=us&pool=nasdaq100&limit=100&deep=0&interval=1d&save_output=1`

示例（用户说「深度分析 NVDA」）：

`http://127.0.0.1:8000/report?tickers=NVDA&deep=1&market=us&interval=1d&save_output=1`

生成过程中可轮询：`GET http://127.0.0.1:8000/report/progress`  
也可用浏览器：`GET /report/page` 在页面里点「生成报告」。

---

## 参数说明（与 README / `server.py` 一致）

| 参数 | 含义 |
|------|------|
| `tickers` | 逗号分隔；若传了，以代码为准，不再依赖 pool 选股 |
| `market` | `us` / `cn` / `hk`（未传 tickers 时与 pool 一起用） |
| `pool` | 如 `nasdaq100`、`sp500`、`russell2000`、`csi300` 等 |
| `limit` | 未传 tickers 时从池子里取前 N 只 |
| `deep` | `0` 常规；`1` 深度（①②③④⑤ + 与上次对比），**很慢**，纳指全量一般用 `0` |
| `interval` | 默认 `1d`；分 K 如 `15m` 等见文档 |
| `prepost` | `1` 含盘前盘后 |
| `save_output` | `1` 写入 `report/output/report-MMDD-HHMM.html` |

---

## 生成结束后

- 新 HTML 路径在 `report/output/`，把**最新**文件路径给用户，必要时 `open`。
- 详见：`docs/Report流程说明.md`、`README.md`。
