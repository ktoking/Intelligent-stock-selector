#!/usr/bin/env python3
"""
基于 recommendations.jsonl 的简化向量化回测示例（需安装 vectorbt）。

安装：pip install -r requirements-optional.txt

说明：
- 将历史「买入」推荐视为入场信号，持有固定 bar 数或直到下一信号前平仓（示例用最简逻辑）。
- 非完整交易引擎，仅作路线图 P1「真回测」脚手架；生产请扩展滑点、手续费、仓位。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REC_PATH = ROOT / "data" / "memory" / "recommendations.jsonl"


def main() -> None:
    try:
        import vectorbt as vbt
        import yfinance as yf
        import pandas as pd
    except ImportError:
        print("请先安装: pip install -r requirements-optional.txt", file=sys.stderr)
        sys.exit(1)

    if not REC_PATH.exists():
        print(f"未找到推荐文件: {REC_PATH}", file=sys.stderr)
        sys.exit(1)

    rows = []
    with open(REC_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    buys = [r for r in rows if (r.get("action") or "") == "买入" and r.get("ticker")]
    if not buys:
        print("无买入记录，退出")
        return

    tickers = sorted({b["ticker"].upper() for b in buys})
    print(f"标的数: {len(tickers)}，示例拉取日线…")

    price = yf.download(
        tickers,
        period="2y",
        interval="1d",
        group_by="ticker",
        threads=True,
        progress=False,
    )
    if price.empty:
        print("价格数据为空")
        return

    closes = price["Close"] if isinstance(price.columns, pd.MultiIndex) else price
    if isinstance(closes, pd.Series):
        closes = closes.to_frame(name=tickers[0])

    # 极简：全样本等权买入并持有最后 60 个交易日（演示 portfolio 接口）
    subset = closes.iloc[-60:].dropna(axis=1, how="any")
    if subset.shape[1] == 0:
        print("对齐后无完整列")
        return

    n = subset.shape[1]
    size = 1.0 / n
    entries = pd.DataFrame(True, index=subset.index, columns=subset.columns)
    exits = pd.DataFrame(False, index=subset.index, columns=subset.columns)
    exits.iloc[-1] = True

    pf = vbt.Portfolio.from_signals(
        subset,
        entries,
        exits,
        size=size,
        size_type="percent",
        init_cash=100_000,
        fees=0.0005,
        freq="1d",
    )
    print(pf.stats())


if __name__ == "__main__":
    main()
