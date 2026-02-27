"""
动态股票池：从市值、近期增长等维度拉取美股优质标的（默认 S&P 500 池内取 top N）。
并支持从线上拉取纳斯达克100、恒生指数、沪深300 等成分股，便于全量分析。
"""
import time
from typing import List, Optional

import pandas as pd
import yfinance as yf


# ---------- 线上成分股拉取（Wikipedia 等），失败则返回 None，由调用方回退静态列表 ----------

def get_nasdaq100_tickers_from_web() -> Optional[List[str]]:
    """从 Wikipedia 拉取纳斯达克100 成分股，失败返回 None。"""
    try:
        url = "https://en.wikipedia.org/wiki/Nasdaq-100"
        tables = pd.read_html(url)
        for df in tables:
            if df is None or df.empty:
                continue
            cols = [c for c in df.columns if isinstance(c, str)]
            if "Ticker" in cols:
                symbols = df["Ticker"].astype(str).str.strip().str.replace(".", "-", regex=False)
                out = [s for s in symbols.tolist() if s and len(s) <= 6 and s != "Ticker"]
                if len(out) >= 50:
                    return out
            if "Symbol" in cols:
                symbols = df["Symbol"].astype(str).str.strip().str.replace(".", "-", regex=False)
                out = [s for s in symbols.tolist() if s and len(s) <= 6 and s != "Symbol"]
                if len(out) >= 50:
                    return out
    except Exception:
        pass
    return None


def _parse_hk_code(raw: str) -> Optional[str]:
    """从 Wikipedia 表格单元格解析港股代码为 yfinance 格式（如 0700.HK）。"""
    if not raw or raw == "nan":
        return None
    s = "".join(str(raw).strip().split()).replace(",", "")
    # SEHK: 700 或 700 或 0700
    if ":" in s:
        s = s.split(":")[-1].strip()
    if s.isdigit() and 1 <= len(s) <= 4:
        return s.zfill(4) + ".HK"
    if s.endswith(".HK"):
        return s
    return None


def get_hangseng_tickers_from_web() -> Optional[List[str]]:
    """从 Wikipedia 拉取恒生指数成分股（yfinance 格式带 .HK），失败返回 None。"""
    try:
        url = "https://en.wikipedia.org/wiki/Hang_Seng_Index"
        tables = pd.read_html(url)
        for df in tables:
            if df is None or df.empty:
                continue
            for code_col in ["Ticker", "Stock code", "Code", "Symbol", "代號", "Stock Code"]:
                if code_col in df.columns:
                    out = []
                    for c in df[code_col].astype(str).tolist():
                        t = _parse_hk_code(c)
                        if t and t not in out:
                            out.append(t)
                    if len(out) >= 10:
                        return out
    except Exception:
        pass
    return None


def get_hstech_tickers_from_web() -> Optional[List[str]]:
    """从 Wikipedia 拉取恒生科技指数成分股（yfinance 格式带 .HK）。恒科无独立成分表，尝试国企指数中科技类，失败返回 None。"""
    try:
        url = "https://en.wikipedia.org/wiki/Hang_Seng_China_Enterprises_Index"
        tables = pd.read_html(url)
        tech_keywords = ("information technology", "technology", "consumer discretionary", "healthcare")
        for df in tables:
            if df is None or df.empty:
                continue
            code_col = None
            industry_col = None
            for c in df.columns:
                if str(c).lower() in ("ticker", "code", "symbol", "stock code"):
                    code_col = c
                if "industry" in str(c).lower() or "sector" in str(c).lower():
                    industry_col = c
            if code_col is None:
                continue
            out = []
            for _, row in df.iterrows():
                raw = row.get(code_col, "")
                if industry_col and industry_col in row.index:
                    ind = str(row.get(industry_col, "")).lower()
                    if not any(k in ind for k in tech_keywords):
                        continue
                t = _parse_hk_code(str(raw))
                if t and t not in out:
                    out.append(t)
            if len(out) >= 10:
                return out[:30]
            # 无 Industry 列或科技类不足时，取前 30 只
            out = []
            for c in df[code_col].astype(str).tolist():
                t = _parse_hk_code(c)
                if t and t not in out:
                    out.append(t)
            if len(out) >= 10:
                return out[:30]
    except Exception:
        pass
    return None


# ---------- A股选股池：AKShare（可选依赖），失败返回 None ----------

def _akshare_code_to_yfinance(code: str) -> Optional[str]:
    """将 AKShare 的 6 位代码转为 yfinance 格式（.SS / .SZ）。北交所 8 位暂不转换。"""
    if not code or not isinstance(code, str):
        return None
    c = "".join(str(code).strip().split())
    if not c.isdigit() or len(c) != 6:
        return None
    if c.startswith("60") or c.startswith("68"):
        return c + ".SS"
    if c.startswith("00") or c.startswith("30"):
        return c + ".SZ"
    return None


def get_csi300_tickers_akshare() -> Optional[List[str]]:
    """用 AKShare 拉取沪深300 成分股（yfinance 格式）。未装 akshare 或接口变更时返回 None。"""
    try:
        import akshare as ak
        # 中证指数成分：symbol 000300 = 沪深300
        df = ak.index_stock_cons_csindex(symbol="000300")
        if df is None or df.empty:
            return None
        # 列名可能是 "成分券代码" / "品种代码" / "code" 等
        code_col = None
        for c in df.columns:
            if "代码" in str(c) or str(c).lower() in ("code", "symbol"):
                code_col = c
                break
        if code_col is None:
            return None
        out = []
        for _, row in df.iterrows():
            raw = row.get(code_col) or ""
            t = _akshare_code_to_yfinance(str(raw).strip())
            if t:
                out.append(t)
        if len(out) >= 50:
            return out
    except Exception:
        pass
    return None


def get_csi2000_tickers_akshare() -> Optional[List[str]]:
    """用 AKShare 拉取中证2000 成分股（yfinance 格式）。指数代码 932000。未装 akshare 或接口变更时返回 None。"""
    try:
        import akshare as ak
        # 中证2000 指数代码 932000；成份股权重接口含成分券代码
        df = ak.index_stock_cons_weight_csindex(symbol="932000")
        if df is None or df.empty:
            return None
        code_col = None
        for c in df.columns:
            if "代码" in str(c) or str(c).lower() in ("code", "symbol"):
                code_col = c
                break
        if code_col is None:
            return None
        out = []
        for _, row in df.iterrows():
            raw = row.get(code_col) or ""
            t = _akshare_code_to_yfinance(str(raw).strip())
            if t:
                out.append(t)
        if len(out) >= 100:
            return out
    except Exception:
        pass
    return None


def get_cn_spot_tickers_akshare(limit: int = 300, sort_by: str = "总市值") -> Optional[List[str]]:
    """用 AKShare 拉取全 A 实时行情，按 sort_by 排序取前 limit 只（yfinance 格式）。"""
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            return None
        if "代码" not in df.columns:
            return None
        # 只保留沪深 6 位代码（排除北交所 8 位等）
        df = df[df["代码"].astype(str).str.match(r"^\d{6}$", na=False)].copy()
        if sort_by in df.columns:
            df = df.sort_values(sort_by, ascending=False, na_position="last")
        out = []
        for _, row in df.iterrows():
            t = _akshare_code_to_yfinance(str(row.get("代码", "")).strip())
            if t and t not in out:
                out.append(t)
            if len(out) >= limit:
                break
        if len(out) >= 10:
            return out[:limit]
    except Exception:
        pass
    return None


def get_csi300_tickers_from_web() -> Optional[List[str]]:
    """从 Wikipedia 拉取沪深300 成分股（yfinance 格式 .SS/.SZ），失败返回 None。"""
    try:
        # 英文页可能有成分表
        url = "https://en.wikipedia.org/wiki/CSI_300_Index"
        tables = pd.read_html(url)
        for df in tables:
            if df is None or df.empty:
                continue
            cols = [str(c).lower() for c in df.columns]
            # 找代码列
            code_col = None
            for c in df.columns:
                if "code" in str(c).lower() or "symbol" in str(c).lower() or "ticker" in str(c).lower():
                    code_col = c
                    break
            if code_col is None:
                continue
            codes = df[code_col].astype(str).str.strip()
            out = []
            for c in codes.tolist():
                if not c or c == "nan" or len(c) < 6:
                    continue
                c = "".join(c.split())
                if c.isdigit() and len(c) == 6:
                    if c.startswith("60") or c.startswith("68"):
                        c = c + ".SS"
                    elif c.startswith("00") or c.startswith("30"):
                        c = c + ".SZ"
                    else:
                        continue
                elif ".SS" in c or ".SZ" in c:
                    pass
                else:
                    continue
                out.append(c)
            if len(out) >= 50:
                return out
    except Exception:
        pass
    return None


def get_russell2000_tickers_from_web() -> Optional[List[str]]:
    """罗素2000 完整成分股公开源较少，尝试 Wikipedia 或返回 None 用静态列表。"""
    try:
        url = "https://en.wikipedia.org/wiki/Russell_2000_Index"
        tables = pd.read_html(url)
        for df in tables:
            if df is None or df.empty or len(df) < 100:
                continue
            for code_col in ["Symbol", "Ticker", "Company"]:
                if code_col in df.columns:
                    symbols = df[code_col].astype(str).str.strip().str.replace(".", "-", regex=False)
                    out = [s for s in symbols.tolist() if s and 1 <= len(s) <= 6 and s != code_col]
                    if len(out) >= 100:
                        return out
    except Exception:
        pass
    return None

# 内存缓存：避免每次 /report 都拉 Wikipedia + 批量行情（缓存整份排序列表，取前 n 只）
_CACHE: Optional[List[str]] = None
_CACHE_TS: float = 0
_CACHE_TTL_SEC = 86400  # 1 天
# 拉取时一次算出的池大小（按市值+增长排序后缓存）
_POOL_SIZE = 200


def _get_sp500_tickers() -> List[str]:
    """从 Wikipedia 拉取 S&P 500 成分，失败时退回内置列表（多行业覆盖）。"""
    try:
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        tables = pd.read_html(url)
        df = tables[0]
        # 股票代码：Yahoo 用 - 代替 .
        symbols = df["Symbol"].astype(str).str.replace(".", "-", regex=False).tolist()
        return [s for s in symbols if s and len(s) <= 6]
    except Exception:
        pass
    # 退回：多行业常见标的（科技/消费/医药/金融/工业等）
    return [
        "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK-B", "JPM", "V",
        "JNJ", "WMT", "PG", "UNH", "HD", "MA", "DIS", "PYPL", "ADBE", "NFLX",
        "CRM", "INTC", "AMD", "QCOM", "AVGO", "TXN", "ORCL", "CSCO", "IBM", "NOW",
        "ABT", "PEP", "KO", "COST", "MCD", "NKE", "PM", "ABBV", "TMO", "DHR",
        "LLY", "MRK", "PFE", "BMY", "AMGN", "GILD", "VRTX", "REGN", "MRNA",
        "HON", "UPS", "CAT", "DE", "BA", "GE", "MMM", "LMT", "RTX", "NOC",
        "XOM", "CVX", "COP", "SLB", "EOG", "PXD", "MPC", "PSX", "VLO", "OXY",
        "BAC", "WFC", "GS", "MS", "C", "AXP", "BLK", "SCHW", "SPGI", "CME",
        "AMAT", "LRCX", "KLAC", "MU", "ADI", "MRVL", "SNPS", "CDNS", "FTNT",
        "MDT", "SYK", "BSX", "ZBH", "EW", "ISRG", "DXCM", "HUM", "CI", "ELV",
        "LOW", "TGT", "HD", "COST", "SBUX", "NKE", "TJX", "ORLY", "AZO",
        "SPY", "QQQ",
    ]


def _batch_returns(tickers: List[str], period: str = "1mo") -> dict:
    """批量拉取近期涨跌幅（日 K 维度）。"""
    if not tickers:
        return {}
    out = {}
    try:
        data = yf.download(
            tickers, period=period, interval="1d", auto_adjust=True,
            threads=True, progress=False, ignore_tz=True, group_by="ticker"
        )
    except Exception:
        return {}
    if data is None or data.empty:
        return {}
    # 多标的时列为 (Ticker, OHLC)，单标的时列为 Open/High/Low/Close
    if isinstance(data.columns, pd.MultiIndex):
        level0 = data.columns.get_level_values(0)
        for t in tickers:
            try:
                if t not in level0:
                    continue
                sub = data[t] if t in data.columns.get_level_values(0) else None
                if sub is None:
                    continue
                close = sub["Close"] if isinstance(sub, pd.DataFrame) and "Close" in sub.columns else None
                if close is None:
                    continue
                s = close.dropna()
                if len(s) >= 2:
                    out[t] = (float(s.iloc[-1]) - float(s.iloc[0])) / float(s.iloc[0]) * 100
            except Exception:
                continue
    else:
        close = data["Close"] if "Close" in data.columns else data.iloc[:, 0]
        if close is not None and len(close.dropna()) >= 2:
            s = close.dropna()
            out[tickers[0]] = (float(s.iloc[-1]) - float(s.iloc[0])) / float(s.iloc[0]) * 100
    return out


def get_top_by_market_cap_and_growth(
    n: int = 100,
    min_market_cap: Optional[float] = None,
    growth_weight: float = 0.3,
) -> List[str]:
    """
    从 S&P 500 池中按市值与近期增长综合排序，取前 n 只。行业覆盖多领域。
    min_market_cap: 最低市值（美元），可选。
    growth_weight: 近期涨幅权重 0~1，其余为市值权重。
    """
    global _CACHE, _CACHE_TS
    now = time.time()
    if _CACHE is not None and (now - _CACHE_TS) < _CACHE_TTL_SEC and len(_CACHE) >= n:
        return _CACHE[:n]

    all_tickers = _get_sp500_tickers()
    # 限制参与排名的数量以控制首次拉取时间（约 200 只，多行业已覆盖）
    tickers = all_tickers[:250]
    # 批量取 1 个月涨跌幅（日 K）
    returns = _batch_returns(tickers, period="1mo")
    # 逐个取市值（避免单次请求过大）
    rows = []
    for i, t in enumerate(tickers):
        try:
            st = yf.Ticker(t)
            info = st.info or {}
            mcap = info.get("marketCap")
            if mcap is None:
                continue
            mcap = float(mcap)
            if min_market_cap is not None and mcap < min_market_cap:
                continue
            ret = returns.get(t)
            if ret is None:
                ret = 0.0
            rows.append({"ticker": t, "market_cap": mcap, "return_1m": ret})
        except Exception:
            continue
        if (i + 1) % 50 == 0:
            time.sleep(0.2)
    if not rows:
        return tickers[:n]

    df = pd.DataFrame(rows)
    df["mcap_rank"] = df["market_cap"].rank(ascending=False, method="first")
    df["ret_rank"] = df["return_1m"].rank(ascending=False, method="first")
    max_m, max_r = df["mcap_rank"].max(), df["ret_rank"].max()
    df["score"] = (1 - df["mcap_rank"] / (max_m + 1e-10)) * (1 - growth_weight) + (
        1 - df["ret_rank"] / (max_r + 1e-10)
    ) * growth_weight
    df = df.sort_values("score", ascending=False).head(_POOL_SIZE)
    out_full = df["ticker"].astype(str).tolist()
    _CACHE = out_full
    _CACHE_TS = now
    return out_full[:n]
