# WorkBuddy 读取本项目的报告 - 配置说明

本文说明如何让本地安装的 **WorkBuddy**（腾讯云桌面 AI 智能体）读取本仓库 `report` 接口生成的 HTML 报告，包括：项目路径、报告输出位置、如何生成报告、以及给 WorkBuddy 的提示词示例。

---

## 一、项目地址（工作文件夹）

在 WorkBuddy 里需要**授权**以下目录之一，才能读取报告文件：

| 用途 | 路径 |
|------|------|
| **仅读报告**（推荐） | ` /Users/kaiyi.wang/PycharmProjects/stock-agent/report/output ` |
| **整个项目**（若需读代码/配置） | ` /Users/kaiyi.wang/PycharmProjects/stock-agent ` |

- WorkBuddy 只能访问你添加的「工作文件夹」，请至少把 **`report/output`** 加入。
- 路径中的 `kaiyi.wang` 请按你本机用户名替换（若不同）。

---

## 二、报告输出文件位置与命名

- **目录**：` report/output/ `
- **文件名规则**：` report-{月日}-{时分}.html `，例如：
  - ` report-0309-1430.html ` → 3 月 9 日 14:30 生成
  - ` report-0308-0805.html ` → 3 月 8 日 08:05 生成（如定时任务跑的）

**最新报告** = 该目录下按文件名排序后的**最后一个**（因为时间戳在文件名里，越晚生成文件名越靠后）。若 WorkBuddy 支持「按修改时间」排序，则选**修改时间最新**的 ` report-*.html ` 即可。

---

## 三、报告在哪里生成（执行方式）

WorkBuddy **不负责生成报告**，只负责**读取已经生成好的 HTML 文件**。报告需要在本项目中先跑出来，有两种常见方式：

### 方式 A：本地先启动服务，再触发报告

1. **启动服务**（在项目根目录执行）：
   ```bash
   cd /Users/kaiyi.wang/PycharmProjects/stock-agent
   python server.py
   ```
   或：` uvicorn server:app --host 0.0.0.0 --port 8000 `

2. **触发报告生成**（任选其一）：
   - **浏览器**：打开 ` http://localhost:8000/report/page `，选市场/数量后点「生成报告」  
     （注意：该页面默认 `save_output=0`，**不会**写入 `report/output/`；若要落盘，用下面接口。）
   - **直接调接口**（会保存到 `report/output/`）：
     ```text
     GET http://localhost:8000/report?limit=5&market=us&save_output=1
     ```
     例如在浏览器地址栏输入，或：
     ```bash
     curl -o /dev/null "http://localhost:8000/report?limit=5&market=us&save_output=1"
     ```

3. 生成完成后，新文件会出现在 ` report/output/report-MMDD-HHMM.html `。

### 方式 B：定时任务（如每日 8 点）

若已配置 `scripts/daily_report.py` 或 crontab/Launchd，报告会自动生成并写入 ` report/output/ `。你只需在 WorkBuddy 里读该目录下**最新**的 ` report-*.html ` 即可。

---

## 四、给 WorkBuddy 的提示词示例

下面这些提示词可以在 WorkBuddy 里直接使用（把路径换成你本机实际路径）。

### 1. 读取最新一份报告

- **提示词（推荐）**：
  ```text
  请打开文件夹 /Users/kaiyi.wang/PycharmProjects/stock-agent/report/output/，
  找到其中**修改时间最新**的、文件名形如 report-月日-时分.html 的报告文件，
  读取其内容（若是 HTML，请提取正文文字），并给我一段简要总结：包含报告标题、生成时间、覆盖了哪些股票、以及本期最值得关注的 1～3 只标的和理由。
  ```

- **若 WorkBuddy 只能选「单个文件」**：
  ```text
  请读取这个文件并总结报告要点（覆盖了哪些股票、评分与建议、本期亮点）：
  /Users/kaiyi.wang/PycharmProjects/stock-agent/report/output/report-0309-1430.html
  ```
  （把 `report-0309-1430.html` 换成你当前最新报告的文件名。）

### 2. 对比最近两份报告

- **提示词**：
  ```text
  在 /Users/kaiyi.wang/PycharmProjects/stock-agent/report/output/ 下，
  按修改时间取**最新两份** report-*.html，
  分别读取并对比：报告日期、标的列表、评分变化、以及「本期推荐」或「报告总览」的异同，用几条 bullet 总结。
  ```

### 3. 只问某只股票在本期报告中的结论

- **提示词**：
  ```text
  请读取 /Users/kaiyi.wang/PycharmProjects/stock-agent/report/output/ 下**最新**的 report-*.html，
  找到关于 [股票代码或名称，如 AAPL 或 苹果] 的部分，
  总结：评分、交易建议（买入/观察/减仓）、核心结论和主要理由。
  ```

### 4. 固定用「最新报告」的通用话术（可长期用）

- **提示词**：
  ```text
  我本地有一个股票分析项目的报告，输出目录是：
  /Users/kaiyi.wang/PycharmProjects/stock-agent/report/output/
  报告文件名格式是 report-月日-时分.html，例如 report-0309-1430.html。
  请每次当我问「最新报告」或「今天的报告」时，都去这个目录下找**修改时间最新**的 report-*.html，读取内容后按我的问题回答（总结/某只股票/对比等）。
  ```

---

## 五、参数速查（生成报告时）

| 参数 | 说明 | 示例 |
|------|------|------|
| **limit** | 报告里分析的股票数量 | 5 / 100 |
| **market** | 市场：us / cn / hk | us |
| **pool** | 选股池：nasdaq100 / csi300 / russell2000 等 | 不传=默认池 |
| **deep** | 0=快速（技术+消息+财报+期权）；1=含深度分析 | 0 |
| **save_output** | 1=保存到 report/output/；0=不保存 | 1（要落盘必须为 1） |

保存到本地必须带 **`save_output=1`**，例如：
` http://localhost:8000/report?limit=10&market=us&save_output=1 `

---

## 六、小结

| 项目 | 说明 |
|------|------|
| **项目地址** | `/Users/kaiyi.wang/PycharmProjects/stock-agent` |
| **报告输出目录** | `/Users/kaiyi.wang/PycharmProjects/stock-agent/report/output/` |
| **输出文件名** | `report-MMDD-HHMM.html`（如 report-0309-1430.html） |
| **报告在哪里执行** | 本机：`python server.py` 后访问 `GET /report?…&save_output=1`，或定时任务 `scripts/daily_report.py` |
| **WorkBuddy 做什么** | 只读已生成的 HTML 文件；在 WorkBuddy 中把 `report/output`（或整个项目）加入工作文件夹，用上面提示词即可。 |

若 WorkBuddy 对 HTML 解析不友好，可以改为：用浏览器打开该 HTML 后「另存为 PDF」或复制正文到 TXT，再让 WorkBuddy 读 PDF/TXT。
