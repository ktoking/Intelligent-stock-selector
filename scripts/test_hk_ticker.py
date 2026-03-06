#!/usr/bin/env python3
"""
测试港股代码规范化并拉取数据，直到成功或明确失败。
用法：
  python scripts/test_hk_ticker.py 00100
  python scripts/test_hk_ticker.py 01810 00100 0700
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _normalize_ticker(t: str) -> str:
    """与 config.tickers 一致：港股 5 位只去第一位，4 位原样，再补 .HK。"""
    s = (t or "").strip().upper()
    if not s or ".SS" in s or ".SZ" in s or ".HK" in s:
        return s
    if s.isdigit() and (len(s) == 4 or len(s) == 5):
        return (s[1:] if len(s) == 5 else s) + ".HK"
    return s


def main():
    raw_list = sys.argv[1:] if len(sys.argv) > 1 else ["00100", "01810"]
    for raw in raw_list:
        raw = (raw or "").strip()
        if not raw:
            continue
        normalized = _normalize_ticker(raw)
        print(f"输入: {raw!r} → 规范化: {normalized!r}")

        # 港股规则：5 位只去第一位，4 位原样
        if raw.isdigit() and len(raw) == 5:
            print(f"  说明: 5 位码只去第一位 → {raw[1:]!r} + '.HK' = {normalized!r}")
            if raw == "00100":
                print(f"        （Minimax 00100 → 0100.HK）")
        elif raw.isdigit() and len(raw) == 4:
            print(f"  说明: 4 位码原样 + '.HK' = {normalized!r}")

        # 用 yfinance 拉取数据验证
        try:
            import logging
            logging.getLogger("yfinance").setLevel(logging.ERROR)
            import yfinance as yf
        except ImportError:
            print("  跳过拉取: 未安装 yfinance")
            print()
            continue

        ticker = yf.Ticker(normalized)
        hist = ticker.history(period="5d")
        info = ticker.info
        name = info.get("shortName") or info.get("longName") or "—"
        price = info.get("regularMarketPrice") or (float(hist["Close"].iloc[-1]) if hist is not None and not hist.empty else None)

        if hist is not None and not hist.empty and price is not None:
            print(f"  拉取成功: {name} 最近收: {price:.4f}  共 {len(hist)} 条 K 线")
        elif price is not None:
            print(f"  拉取部分成功: {name} 当前价: {price:.4f}  (无 5d K 线)")
        else:
            print("  拉取失败: 无行情数据 (可能退市或代码不对)")
        print()


if __name__ == "__main__":
    main()
