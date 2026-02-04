"""
多市场选股：美股（S&P 500 / 罗素2000）、A股（龙头 / 中证2000）、港股。支持按市场+池取静态/动态池。
手动传入 tickers 时，A股 6 位代码会自动补交易所后缀（上海 .SS、深圳 .SZ），港股 4 位可补 .HK。
"""
from typing import List

from data.universe import get_top_by_market_cap_and_growth


def normalize_ticker(t: str) -> str:
    """
    将手动输入的股票代码规范为 yfinance 可识别的格式。
    - A股 6 位数字：60xxxx/68xxxx → xxx.SS（上海），00xxxx/002xxx/300xxx → xxx.SZ（深圳）。
    - 港股 4 位数字：补 .HK，如 0700 → 0700.HK。
    - 已有 .SS/.SZ/.HK 或美股代码（字母）则原样返回（仅统一大小写）。
    """
    s = (t or "").strip().upper()
    if not s:
        return s
    if ".SS" in s or ".SZ" in s or ".HK" in s:
        return s
    if s.isdigit():
        if len(s) == 6:
            if s.startswith("60") or s.startswith("68"):
                return s + ".SS"
            if s.startswith("00") or s.startswith("30"):
                return s + ".SZ"
        if len(s) == 4:
            return s + ".HK"
    return s

# 报告默认分析数量
DEFAULT_REPORT_TOP_N = 100

# 市场枚举：us=美股，cn=A股，hk=港股
MARKET_US = "us"
MARKET_CN = "cn"
MARKET_HK = "hk"
MARKETS = [MARKET_US, MARKET_CN, MARKET_HK]

# 选股池枚举：大盘=默认（S&P500/沪深龙头），小盘/潜力=罗素2000/中证2000，纳斯达克100
POOL_LARGE = "sp500"       # 美股默认；A股为沪深龙头
POOL_NASDAQ100 = "nasdaq100"    # 美股纳斯达克100（科技/成长大盘）
POOL_SMALL_US = "russell2000"   # 美股小盘（罗素2000 风格）
POOL_SMALL_CN = "csi2000"       # A股小盘/潜力（中证2000 风格）

# 美股纳斯达克100：科技/成长大盘（yfinance 代码），常见成分股静态列表
NASDAQ_100_TICKERS_FALLBACK = [
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "NVDA", "META", "TSLA", "AVGO", "COST",
    "PEP", "ADBE", "NFLX", "CSCO", "AMD", "INTC", "QCOM", "TXN", "INTU", "AMGN",
    "AMAT", "SBUX", "MDLZ", "ISRG", "GILD", "VRTX", "REGN", "PANW", "ADP", "BKNG",
    "LRCX", "KLAC", "SNPS", "CDNS", "ASML", "CRWD", "MRVL", "ABNB", "WDAY", "DXCM",
    "MNST", "ORLY", "PCAR", "CTAS", "PAYX", "KDP", "KHC", "MAR", "MELI", "AEP",
    "CHTR", "CPRT", "FTNT", "ODFL", "FAST", "EXC", "XEL", "FANG", "CCEP", "IDXX",
    "AZN", "WBD", "EA", "CTSH", "VRSK", "DDOG", "ZS", "TEAM", "ANSS", "MCHP",
    "BKR", "GEHC", "CDW", "CPT", "ROST", "CSGP", "WBA", "DLTR", "TTD", "NXPI",
    "MDB", "APP", "SMCI", "ARM", "CEG", "LULU", "HON", "VOD", "GFS",
]

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

# 美股小盘/潜力股：罗素2000 风格（yfinance 代码），多行业覆盖
RUSSELL_2000_TICKERS_FALLBACK = [
    "SMCI", "ENPH", "SEDG", "FIVE", "DECK", "POOL", "MSTR", "ARM", "RIVN", "LCID",
    "SOFI", "UPST", "AFRM", "COIN", "HOOD", "PLTR", "SNOW", "DDOG", "NET", "ZS",
    "CRWD", "OKTA", "TWLO", "DOCU", "MDB", "CFLT", "GTLB", "ESTC", "PATH", "BILL",
    "APP", "CVNA", "CARG", "RBLX", "U", "DKNG", "PENN", "LNW", "CHWY", "WAYFA",
    "W", "ETSY", "CHGG", "FVRR", "IAC", "MTCH", "BMBL", "YETI", "SHAK", "WING",
    "CAVA", "DPZ", "PZZA", "DRI", "BLMN", "EAT", "DIN", "TXRH", "BJRI", "PLAY",
    "FROG", "DUOL", "VEEV", "HIMS", "TDOC", "HOLX", "DXCM", "PODD", "ALGN", "IDXX",
    "ZBRA", "FTNT", "PANW", "CRWD", "CYBR", "OKTA", "ZS", "S", "TWLO", "VEEV",
    "EXEL", "SRPT", "BIIB", "ALNY", "INCY", "BMRN", "NBIX", "SGEN", "RARE", "NTRA",
    "AXSM", "ACAD", "SRPT", "FOLD", "RGNX", "BLUE", "BEAM", "CRSP", "EDIT", "NTLA",
    "SMCI", "AVGO", "MRVL", "LSCC", "RMBS", "SWKS", "QRVO", "MCHP", "MXIM", "SYNA",
    "JBLU", "ALK", "SAVE", "LUV", "DAL", "UAL", "AAL", "HA", "SNCY", "ULCC",
]

# A股小盘/潜力股：中证2000 风格（中小盘、创业板、科创板等，yfinance 格式 .SS/.SZ）
CN_CSI2000_TICKERS_FALLBACK = [
    "300124.SZ", "300274.SZ", "300496.SZ", "300033.SZ", "300059.SZ", "300015.SZ",
    "300347.SZ", "300316.SZ", "300661.SZ", "300628.SZ", "300676.SZ", "300763.SZ",
    "300759.SZ", "300782.SZ", "300760.SZ", "300122.SZ", "300142.SZ", "300253.SZ",
    "002475.SZ", "002415.SZ", "002230.SZ", "002352.SZ", "002714.SZ", "002304.SZ",
    "002271.SZ", "002241.SZ", "002008.SZ", "002049.SZ", "002466.SZ", "002594.SZ",
    "002812.SZ", "002916.SZ", "002938.SZ", "002555.SZ", "002456.SZ",
    "688981.SS", "688036.SS", "688012.SS", "688111.SS", "688008.SS", "688116.SS",
    "688126.SS", "688169.SS", "688223.SS", "688301.SS", "688390.SS", "688599.SS",
    "600588.SS", "600570.SS", "600436.SS", "600309.SS", "600267.SS",
    "000063.SZ", "000538.SZ", "000568.SZ", "000858.SZ", "000333.SZ", "000651.SZ",
    "001979.SZ", "002027.SZ", "002044.SZ", "002065.SZ", "002078.SZ", "002129.SZ",
]

# A股/港股 ticker → 中文名称（报告展示用），可自行扩充
TICKER_ZH_NAMES = {
    "0700.HK": "腾讯控股", "9988.HK": "阿里巴巴", "3690.HK": "美团", "0941.HK": "中国移动",
    "1299.HK": "友邦保险", "2318.HK": "中国平安", "2382.HK": "舜宇光学", "2628.HK": "中国人寿",
    "0939.HK": "建设银行", "1398.HK": "工商银行", "3988.HK": "中国银行", "1093.HK": "石药集团",
    "600519.SS": "贵州茅台", "000858.SZ": "五粮液", "600036.SS": "招商银行", "000333.SZ": "美的集团",
    "601318.SS": "中国平安", "000651.SZ": "格力电器", "600276.SS": "恒瑞医药", "300750.SZ": "宁德时代",
    "601012.SS": "隆基绿能", "000568.SZ": "泸州老窖", "600030.SS": "中信证券", "601166.SS": "兴业银行",
    "000725.SZ": "京东方A", "002714.SZ": "牧原股份", "600887.SS": "伊利股份", "300059.SZ": "东方财富",
    "002415.SZ": "海康威视", "600436.SS": "片仔癀", "000063.SZ": "中兴通讯", "601888.SS": "中国中免",
    "300760.SZ": "迈瑞医疗", "600104.SS": "上汽集团", "000001.SZ": "平安银行", "600000.SS": "浦发银行",
}


def get_report_tickers(
    limit: int = DEFAULT_REPORT_TOP_N,
    market: str = MARKET_US,
    pool: str = None,
) -> List[str]:
    """
    按市场与选股池取前 limit 只。
    market: us=美股，cn=A股，hk=港股。
    pool: 不传或 sp500=大盘（美股 S&P500 / A股沪深龙头）；nasdaq100=美股纳斯达克100；russell2000=美股小盘（罗素2000）；csi2000=A股小盘/潜力（中证2000）。
    limit<=10 时美股大盘用静态列表；美股大盘 limit>10 拉 S&P 500 动态排序；小盘池与 A股/港股均用对应静态池前 limit 只。
    """
    market = (market or MARKET_US).strip().lower()
    pool = (pool or "").strip().lower()
    n = max(1, min(limit, 200))

    # 美股纳斯达克100
    if market == MARKET_US and pool == POOL_NASDAQ100:
        return NASDAQ_100_TICKERS_FALLBACK[:n]
    # 美股小盘：罗素2000 风格
    if market == MARKET_US and pool == POOL_SMALL_US:
        return RUSSELL_2000_TICKERS_FALLBACK[:n]
    # A股小盘/潜力：中证2000 风格
    if market == MARKET_CN and pool == POOL_SMALL_CN:
        return CN_CSI2000_TICKERS_FALLBACK[:n]

    if market == MARKET_CN:
        return CN_QUALITY_TICKERS_FALLBACK[:n]
    if market == MARKET_HK:
        return HK_QUALITY_TICKERS_FALLBACK[:n]
    if market == MARKET_US:
        if limit <= 10:
            return US_QUALITY_TICKERS_FALLBACK[:n]
        return get_top_by_market_cap_and_growth(n=n)
    return US_QUALITY_TICKERS_FALLBACK[:n]
