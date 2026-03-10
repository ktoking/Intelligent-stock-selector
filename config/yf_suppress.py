"""
yfinance 噪音抑制：在导入 yfinance 之前调用，减少 404、possibly delisted、timezone 等报错输出。
供 server.py、CLI 脚本等入口在最早时机执行。
"""
import logging
import warnings

def suppress_yf_noise():
    """抑制 yfinance 及底层库的噪音日志与警告。"""
    warnings.filterwarnings("ignore", module="urllib3")
    warnings.filterwarnings("ignore", message=".*404.*")
    warnings.filterwarnings("ignore", message=".*possibly delisted.*")
    warnings.filterwarnings("ignore", message=".*timezone.*")
    warnings.filterwarnings("ignore", message=".*Quote not found.*")
    warnings.filterwarnings("ignore", message=".*Not Found.*")
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    logging.getLogger("urllib3").setLevel(logging.CRITICAL)
