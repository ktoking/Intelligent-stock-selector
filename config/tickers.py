"""
多市场选股：美股（S&P 500 / 罗素2000）、A股（龙头 / 中证2000）、港股。支持按市场+池取静态/动态池。
手动传入 tickers 时，A股 6 位代码会自动补交易所后缀（上海 .SS、深圳 .SZ），港股 4 位可补 .HK。
"""
from typing import List

from data.universe import (
    get_top_by_market_cap_and_growth,
    get_nasdaq100_tickers_from_web,
    get_hangseng_tickers_from_web,
    get_hstech_tickers_from_web,
    get_csi300_tickers_from_web,
    get_russell2000_tickers_from_web,
    get_csi300_tickers_akshare,
    get_csi2000_tickers_akshare,
    get_cn_spot_tickers_akshare,
)


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

# 选股池枚举：大盘=默认（S&P500/沪深龙头），小盘/潜力=罗素2000/中证2000，纳斯达克100；港股恒指/恒科
POOL_LARGE = "sp500"       # 美股默认；A股为沪深龙头
POOL_NASDAQ100 = "nasdaq100"    # 美股纳斯达克100（科技/成长大盘）
POOL_SMALL_US = "russell2000"   # 美股小盘（罗素2000 风格）
POOL_SMALL_CN = "csi2000"       # A股小盘/潜力（中证2000 风格）
POOL_HK_HSI = "hsi"             # 港股恒生指数（恒指）
POOL_HK_HSTECH = "hstech"       # 港股恒生科技指数（恒科）

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

# 港股：恒生指数 88 只成分股（yfinance 格式：.HK），线上拉取失败时回退，来源 Wikipedia/etnet 2026
HK_QUALITY_TICKERS_FALLBACK = [
    "0005.HK", "0388.HK", "0939.HK", "1299.HK", "1398.HK", "2318.HK", "2388.HK", "2628.HK", "3968.HK", "3988.HK",
    "0002.HK", "0003.HK", "0006.HK", "0836.HK", "1038.HK", "2688.HK",
    "0012.HK", "0016.HK", "0017.HK", "0083.HK", "0101.HK", "0688.HK", "0823.HK", "0960.HK", "1109.HK", "1113.HK", "1209.HK", "1997.HK",
    "0001.HK", "0027.HK", "0066.HK", "0175.HK", "0241.HK", "0267.HK", "0285.HK", "0288.HK", "0291.HK", "0300.HK",
    "0316.HK", "0322.HK", "0386.HK", "0669.HK", "0700.HK", "0728.HK", "0762.HK", "0857.HK", "0868.HK", "0881.HK",
    "0883.HK", "0941.HK", "0968.HK", "0981.HK", "0992.HK", "1024.HK", "1044.HK", "1088.HK", "1093.HK", "1099.HK",
    "1177.HK", "1211.HK", "1378.HK", "1810.HK", "1801.HK", "1876.HK", "1928.HK", "1929.HK", "2015.HK", "2020.HK",
    "2057.HK", "2269.HK", "2313.HK", "2319.HK", "2331.HK", "2359.HK", "2382.HK",
    "2618.HK", "3690.HK", "3692.HK", "6618.HK", "6690.HK", "6862.HK", "9633.HK", "9888.HK", "9901.HK", "9961.HK",
    "9988.HK", "9992.HK", "9999.HK",
]

# 港股：恒生科技指数（恒科）成分股静态列表，线上拉取失败时回退。官方 30 只 + 科技/消费/医药扩展至 ~100 只
HK_HSTECH_TICKERS_FALLBACK = [
    "0700.HK", "9988.HK", "3690.HK", "1810.HK", "9618.HK", "9961.HK", "9999.HK",
    "9888.HK", "1024.HK", "2382.HK", "2269.HK", "2196.HK", "6690.HK", "241.HK",
    "2020.HK", "1177.HK", "1211.HK", "992.HK", "981.HK", "3692.HK", "6618.HK",
    "2313.HK", "9626.HK", "2015.HK", "9868.HK", "2359.HK", "1801.HK", "9990.HK",
    "0941.HK", "0762.HK", "0728.HK", "0788.HK", "0968.HK", "0868.HK", "9633.HK",
    "06690.HK", "06862.HK", "09992.HK", "1698.HK", "9660.HK", "9698.HK", "2899.HK",
    "0772.HK", "1797.HK", "6060.HK", "09886.HK", "09901.HK", "6098.HK",
    "0388.HK", "0669.HK", "1044.HK", "1088.HK", "1093.HK", "1099.HK", "1109.HK", "1113.HK",
    "1171.HK", "1378.HK", "0175.HK", "1876.HK", "1928.HK", "1929.HK", "2318.HK", "2319.HK",
    "2388.HK", "2628.HK", "0288.HK", "0291.HK", "0300.HK", "0316.HK", "0322.HK",
    "0386.HK", "0688.HK", "0823.HK", "0836.HK", "0857.HK", "0881.HK", "0883.HK", "0960.HK",
    "0267.HK", "0285.HK", "0384.HK", "6186.HK", "3888.HK", "3968.HK", "3998.HK", "9923.HK",
    "9996.HK", "9997.HK", "2333.HK", "2607.HK", "2688.HK", "2007.HK", "1992.HK", "1766.HK",
    "1890.HK", "6608.HK", "6188.HK", "0966.HK", "2029.HK", "3990.HK",
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
    按市场与选股池取前 limit 只。优先从线上拉取成分股（Wikipedia 等），失败则回退到静态列表，便于全量分析。
    market: us=美股，cn=A股，hk=港股。
    pool: 不传或 sp500=大盘（美股 S&P500 / A股沪深龙头）；nasdaq100=美股纳斯达克100；russell2000=美股小盘（罗素2000）；csi2000=A股小盘/潜力（中证2000）。
    """
    market = (market or MARKET_US).strip().lower()
    pool = (pool or "").strip().lower()
    n = max(1, min(limit, 500))

    # 美股纳斯达克100：优先线上
    if market == MARKET_US and pool == POOL_NASDAQ100:
        tickers = get_nasdaq100_tickers_from_web()
        return (tickers[:n] if tickers else NASDAQ_100_TICKERS_FALLBACK[:n])
    # 美股小盘：罗素2000，优先线上
    if market == MARKET_US and pool == POOL_SMALL_US:
        tickers = get_russell2000_tickers_from_web()
        return (tickers[:n] if tickers else RUSSELL_2000_TICKERS_FALLBACK[:n])
    # A股小盘/潜力：中证2000，优先 AKShare，失败用静态
    if market == MARKET_CN and pool == POOL_SMALL_CN:
        tickers = get_csi2000_tickers_akshare()
        return (tickers[:n] if tickers else CN_CSI2000_TICKERS_FALLBACK[:n])

    # A股：优先 AKShare（沪深300 或 全A按市值），再 Wikipedia，再静态
    if market == MARKET_CN:
        tickers = get_csi300_tickers_akshare()
        if tickers:
            return tickers[:n]
        tickers = get_cn_spot_tickers_akshare(limit=n, sort_by="总市值")
        if tickers:
            return tickers[:n]
        tickers = get_csi300_tickers_from_web()
        return (tickers[:n] if tickers else CN_QUALITY_TICKERS_FALLBACK[:n])
    # 港股：按 pool 选择恒指或恒科
    if market == MARKET_HK:
        if pool == POOL_HK_HSTECH:
            tickers = get_hstech_tickers_from_web()
            return (tickers[:n] if tickers else HK_HSTECH_TICKERS_FALLBACK[:n])
        # 恒指：pool=hsi / hangseng / sp500 / 空
        tickers = get_hangseng_tickers_from_web()
        return (tickers[:n] if tickers else HK_QUALITY_TICKERS_FALLBACK[:n])
    # 美股大盘：limit<=10 用静态快；limit>10 拉 S&P 500 线上再按市值+增长排序
    if market == MARKET_US:
        if limit <= 10:
            return US_QUALITY_TICKERS_FALLBACK[:n]
        return get_top_by_market_cap_and_growth(n=n)
    return US_QUALITY_TICKERS_FALLBACK[:n]
