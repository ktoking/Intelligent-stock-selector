"""
已退市标的（yfinance 无行情，拉取会 404）：过滤后不再参与报告。
"""
DELISTED_TICKERS = frozenset(["WBA"])  # Walgreens 2025-08 私有化退市
