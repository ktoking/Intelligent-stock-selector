"""
多市场选股：美股（S&P 500 池）、A股、港股。支持按市场取静态/动态池。
"""
from typing import List

from data.universe import get_top_by_market_cap_and_growth

# 报告默认分析数量
DEFAULT_REPORT_TOP_N = 100

# 市场枚举：us=美股，cn=A股，hk=港股
MARKET_US = "us"
MARKET_CN = "cn"
MARKET_HK = "hk"
MARKETS = [MARKET_US, MARKET_CN, MARKET_HK]

# 美股：静态列表（多行业，yfinance 代码如 AAPL）
US_QUALITY_TICKERS_FALLBACK = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK-B", "JPM", "V",
    "JNJ", "WMT", "PG", "UNH", "HD", "MA", "DIS", "PYPL", "ADBE", "NFLX",
    "CRM", "INTC", "AMD", "QCOM", "AVGO", "TXN", "ORCL", "CSCO", "IBM", "NOW",
    "ABT", "PEP", "KO", "COST", "MCD", "NKE", "PM", "ABBV", "TMO", "DHR",
    "LLY", "MRK", "PFE", "BMY", "AMGN", "GILD", "VRTX", "REGN", "MRNA",
    "HON", "UPS", "CAT", "DE", "BA", "GE", "MMM", "LMT", "RTX", "XOM", "CVX",
]

# A股：沪深龙头（yfinance 格式：深圳 .SZ，上海 .SS）
CN_QUALITY_TICKERS_FALLBACK = [
    "600519.SS", "000858.SZ", "600036.SS", "000333.SZ", "601318.SS", "000651.SZ",
    "600276.SS", "002475.SZ", "300750.SZ", "601012.SS", "000568.SZ", "600030.SS",
    "601166.SS", "000725.SZ", "002714.SZ", "600887.SS", "300059.SZ", "002415.SZ",
    "600436.SS", "000063.SZ", "601888.SS", "300760.SZ", "002352.SZ", "600309.SS",
    "000538.SZ", "600585.SS", "002304.SZ", "601899.SS", "600900.SS", "000002.SZ",
    "601398.SS", "601288.SS", "600000.SS", "601328.SS", "000001.SZ", "600016.SS",
    "601988.SS", "601818.SS", "600104.SS", "300496.SZ", "002230.SZ",
]

# 港股：恒生/龙头（yfinance 格式：.HK）
HK_QUALITY_TICKERS_FALLBACK = [
    "0700.HK", "9988.HK", "3690.HK", "0941.HK", "1299.HK", "2318.HK",
    "2382.HK", "2628.HK", "0939.HK", "1398.HK", "3988.HK", "1093.HK",
    "2269.HK", "1810.HK", "9618.HK", "9961.HK", "9999.HK", "2899.HK",
    "1177.HK", "2020.HK", "1171.HK", "1972.HK", "6690.HK", "2196.HK",
    "0836.HK", "0016.HK", "0011.HK", "0066.HK", "0083.HK", "0027.HK",
]


def get_report_tickers(limit: int = DEFAULT_REPORT_TOP_N, market: str = MARKET_US) -> List[str]:
    """
    按市场取选股池前 limit 只。
    market: us=美股，cn=A股，hk=港股。
    limit<=10 用静态列表；美股 limit>10 拉 S&P 500 动态排序；A股/港股 limit>10 用静态池前 limit 只。
    """
    market = (market or MARKET_US).strip().lower()
    n = max(1, min(limit, 200))

    if market == MARKET_CN:
        return CN_QUALITY_TICKERS_FALLBACK[:n]
    if market == MARKET_HK:
        return HK_QUALITY_TICKERS_FALLBACK[:n]
    if limit <= 10:
        return US_QUALITY_TICKERS_FALLBACK[:n]
    return get_top_by_market_cap_and_growth(n=n)
