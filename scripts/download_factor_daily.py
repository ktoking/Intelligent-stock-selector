#!/usr/bin/env python3
"""Download public, unadjusted plus adjusted-close daily research inputs."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pandas as pd
import yfinance as yf

SYMBOLS = ("NVDA", "AMD", "MU", "INTC", "TSM", "AAPL", "MSFT", "AMZN", "META",
           "GOOGL", "TSLA", "MSTR", "HOOD", "SPY", "QQQ")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True, help="exclusive exchange date")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--symbols", nargs="+", default=list(SYMBOLS))
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("use a new output path to preserve prior research inputs")
    data = {}
    symbols = tuple(dict.fromkeys(args.symbols))
    for symbol in symbols:
        frame = yf.download(symbol, start=args.start, end=args.end, interval="1d",
                            auto_adjust=False, progress=False, threads=False, timeout=15)
        if frame.empty:
            raise RuntimeError(f"empty data for {symbol}; no partial universe will be saved")
        if isinstance(frame.columns, pd.MultiIndex):
            frame.columns = frame.columns.get_level_values(0)
        frame.columns = [c.lower() for c in frame.columns]
        data[symbol] = frame
        print(symbol, len(frame), str(frame.index[-1]), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(".pkl.tmp")
    pd.to_pickle(data, temporary)
    temporary.replace(args.output)
    args.output.with_suffix(".json").write_text(json.dumps({
        "fetched_at": datetime.now(timezone.utc).isoformat(), "provider": "Yahoo Finance via yfinance daily",
        "start": args.start, "end_exclusive": args.end, "auto_adjust": False,
        "symbols": symbols, "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
        "yfinance_version": yf.__version__,
    }, indent=2))


if __name__ == "__main__":
    main()
