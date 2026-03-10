# 股票分析工作流 - 外部数据 JSON 模板

## 概述

本模板定义了股票分析工作流所需的所有外部数据格式。stock-agent 可从 yfinance 拉取数据并转换为该格式，供下游项目直接消费，无需再调用外部 API。

**报告中的「源数据」区块**：每张股票卡片的「查看源数据 (JSON)」展开后，展示的即为本模板格式（stock_code、stock_data、historical_data、financial_data、news_data、options_data）。

## 获取方式

### 1. HTTP API

```bash
# 单只标的
curl "http://localhost:8000/external-data?ticker=AAPL"

# 可选参数
curl "http://localhost:8000/external-data?ticker=AAPL&period=6mo&interval=1d&max_news=10"
```

| 参数 | 默认 | 说明 |
|------|------|------|
| ticker | 必填 | 股票代码，如 AAPL、0700.HK、600519.SS |
| period | 6mo | 历史数据周期（至少 60 个交易日用于技术指标） |
| interval | 1d | K 线周期：1d=日K |
| max_news | 10 | 新闻条数 |

### 2. Python 调用

```python
from agents.external_data_fetcher import fetch_external_data_json
import json

data = fetch_external_data_json("AAPL")
# 传给下游
with open("aapl_data.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
```

## 返回结构

```json
{
  "stock_code": "AAPL",
  "market_type": "us",
  "stock_data": { ... },
  "historical_data": { "period": "6mo", "data": [ ... ] },
  "financial_data": { ... },
  "news_data": { "total_count": 10, "articles": [ ... ] },
  "options_data": { "current_price": 178.5, "calls": [ ... ], "puts": [ ... ] }
}
```

完整字段说明见用户提供的「股票分析工作流 - 外部数据JSON模板」文档。

## 数据来源

- **stock_data**：yfinance `Ticker.info`
- **historical_data**：yfinance `Ticker.history(period, interval)`
- **financial_data**：yfinance `financials`、`cashflow`、`balance_sheet`、`quarterly_financials`
- **news_data**：yfinance `Ticker.news`
- **options_data**：yfinance `Ticker.option_chain(最近到期日)`

## 注意事项

1. **historical_data**：至少需 60 个交易日用于计算 MA、MACD、KDJ 等；按日期升序。
2. **news_data**：yfinance 新闻通常无 `summary` 字段，本模块用空字符串或 publisher 填充。
3. **options_data**：港股/A 股多数无期权数据，`calls`/`puts` 可能为空。
4. **financial_data**：部分标的 yfinance 财报字段名可能不同，会尽量匹配常见行名（如 Total Revenue、Net Income）。
