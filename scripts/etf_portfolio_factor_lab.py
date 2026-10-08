#!/usr/bin/env python3
"""Versioned, fixed-hypothesis portfolio diagnostics on previously inspected history."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.equity_factor_lab import daily_data, metrics
from scripts.etf_fast_factor_lab import signals
from scripts.factor_portfolio_engine import simulate


def specifications():
    configs = []
    def add(name, sleeves, defense="cash", factor="ema20_100", vol=.6):
        configs.append(dict(name=name, sleeves=sleeves, defense=defense, factor=factor, vol=vol))
    for fraction in (.5, .75, 1.):
        for defense in ("cash", "gold", "gold_bond"):
            add(f"soxl{int(fraction*100)}_{defense}", {"SOXL": fraction}, defense)
    for core in ("TQQQ", "UPRO", "TECL"):
        for fraction in (.5, .75):
            for defense in ("cash", "gold_bond"):
                add(f"soxl{int(fraction*100)}_{core}_{defense}",
                    {"SOXL": fraction, core: 1-fraction}, defense)
    for factor in ("slow_fast_mean", "slow_momentum_mean"):
        for defense in ("cash", "gold_bond"):
            add(f"soxl_{factor}_{defense}", {"SOXL": 1.}, defense, factor)
    for vol in (.4, .5):
        for defense in ("cash", "gold_bond"):
            add(f"soxl_vol{int(vol*100)}_{defense}", {"SOXL": 1.}, defense, vol=vol)
    add("soxl100_shy", {"SOXL": 1.}, "shy")
    return configs


def targets(close, config):
    refs = {"SOXL": "SMH", "TECL": "XLK", "TQQQ": "QQQ", "UPRO": "SPY"}
    weights = pd.DataFrame(0., index=close.index, columns=close.columns)
    for core, budget in config["sleeves"].items():
        sig = signals(close[refs[core]])
        if config["factor"] == "slow_fast_mean":
            active = (sig["ema20_100"] + sig["ema10_50"])/2
        elif config["factor"] == "slow_momentum_mean":
            active = (sig["ema20_100"] + sig["momentum20"])/2
        else:
            active = sig[config["factor"]]
        vol = close[core].pct_change(fill_method=None).rolling(20).std()*np.sqrt(252)
        weights[core] = budget*active*(config["vol"]/vol).clip(upper=1).fillna(0)
    reserve = (1-weights.sum(axis=1)).clip(lower=0)
    defense = config["defense"]
    if defense == "shy":
        weights["SHY"] = reserve
    elif defense in ("gold", "gold_bond"):
        assets = ["GLD"] if defense == "gold" else ["GLD", "IEF"]
        for asset in assets:
            # Equal reserve budgets; an inactive sleeve remains cash.
            weights[asset] = reserve/len(assets)*signals(close[asset])["ema20_100"]
    elif defense != "cash":
        raise ValueError("unknown defense")
    weights.iloc[:253] = 0.
    return weights


def load_panels(transfer, broad):
    p, a = daily_data(transfer)
    q, b = daily_data(broad)
    dates = p["close"].index
    if not dates.isin(q["close"].index).all():
        raise ValueError("broad source missing transfer sessions")
    extra = ["TQQQ", "QQQ", "UPRO", "SPY", "GLD", "IEF", "SHY"]
    merged = {field: pd.concat([p[field], q[field].loc[dates, extra]], axis=1) for field in p}
    discrepancy = float((p["close"]["XLK"]/q["close"].loc[dates, "XLK"]-1).abs().max())
    if discrepancy > .001:
        raise ValueError("overlapping XLK adjusted prices disagree by >10bp")
    return merged, {"sources": [a, b], "overlap_xlk_max_relative_difference": discrepancy}


def run(transfer, broad, output):
    if output.exists():
        raise FileExistsError("use a new output directory; studies are immutable")
    output.mkdir(parents=True)
    configs = specifications()
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(), "configs": configs,
        "primary": ["2013-04-24", "2026-06-05"], "recent": ["2026-06-08", "2026-09-04"],
        "rule": "20/100 EMA signal,20-session sample volatility sizing; reference-close -> next actual ETF open",
        "defense": "allocate residual weight to gold or 50/50 gold/IEF, each gated by own EMA20>EMA100; inactive half remains cash",
        "cost_bps_each_side": [7, 20], "parameter_search": False,
        "status": "fixed hypotheses saved before this run; all dates already observed, not a new holdout",
        "source_sha256": {str(f): hashlib.sha256(f.read_bytes()).hexdigest() for f in (transfer, broad)},
        "code_sha256": {},
    }
    for filename in (Path(__file__), ROOT/"scripts/equity_factor_lab.py",
                     ROOT/"scripts/etf_fast_factor_lab.py", ROOT/"scripts/factor_portfolio_engine.py"):
        payload = filename.read_bytes()
        (output/filename.name).write_bytes(payload)
        manifest["code_sha256"][filename.name] = hashlib.sha256(payload).hexdigest()
    (output/"manifest.json").write_text(json.dumps(manifest, indent=2))
    p, provenance = load_panels(transfer, broad)
    dates = p["close"].index
    first, split = dates.get_loc("2013-04-24"), dates.get_loc("2026-06-08")
    rows, returns = [], {}
    for config in configs:
        name = config["name"]
        w = targets(p["close"], config)
        primary = simulate(p, w, first, split)
        recent = simulate(p, w, split, len(dates), record_orders=False)
        stress = simulate(p, w, first, split, cost_bps=20, record_orders=False)
        full = simulate(p, w, first, len(dates), record_orders=False)
        df = pd.DataFrame(primary["daily"])
        returns[name] = df.set_index("date")["return"]
        # Continuous-path slices avoid artificially liquidating at each era boundary.
        era_metrics = {}
        for start, end in (("2013-04-24", "2020-01-01"), ("2020-01-01", "2024-01-01"),
                           ("2024-01-01", "2026-06-08")):
            records = [x for x in primary["daily"] if start <= x["date"] < end]
            era_metrics[f"{start}/{end}"] = metrics(records)
        annual = {year: metrics(part.to_dict("records")) for year, part in df.groupby(df.date.str[:4])}
        row = {"name": name, "config": config, "primary": primary["metrics"], "recent": recent["metrics"],
               "full": full["metrics"], "cost20": stress["metrics"], "eras": era_metrics, "annual": annual}
        rows.append(row)
        df.to_csv(output/f"{name}_primary_daily.csv", index=False)
        pd.DataFrame(primary["orders"]).to_csv(output/f"{name}_primary_orders.csv", index=False)
        pd.DataFrame(recent["daily"]).to_csv(output/f"{name}_recent_daily.csv", index=False)
        pd.DataFrame(full["daily"]).to_csv(output/f"{name}_full_daily.csv", index=False)
        print(f"{name}: CAGR={row['primary']['annualized_pct']:.2f} DD={row['primary']['max_drawdown_close_pct']:.2f} recent={row['recent']['return_pct']:.2f}", flush=True)
    correlations = pd.DataFrame(returns).corr()
    correlations.to_csv(output/"strategy_return_correlations.csv")
    underlying = p["close"][["SOXL", "TECL", "TQQQ", "UPRO", "GLD", "IEF"]].pct_change(fill_method=None).iloc[first:split]
    underlying.corr().to_csv(output/"asset_return_correlations.csv")
    baseline = next(r for r in rows if r["name"] == "soxl100_cash")
    for row in rows:
        row["delta_cagr_pp"] = row["primary"]["annualized_pct"]-baseline["primary"]["annualized_pct"]
        row["delta_drawdown_pp"] = row["primary"]["max_drawdown_close_pct"]-baseline["primary"]["max_drawdown_close_pct"]
    frontier = [r["name"] for r in rows if not any(
        o["primary"]["annualized_pct"] >= r["primary"]["annualized_pct"] and
        o["primary"]["max_drawdown_close_pct"] <= r["primary"]["max_drawdown_close_pct"] and
        (o["primary"]["annualized_pct"] > r["primary"]["annualized_pct"] or
         o["primary"]["max_drawdown_close_pct"] < r["primary"]["max_drawdown_close_pct"])
        for o in rows)]
    report = {"manifest": manifest, "data": provenance, "results": rows, "diagnostic_frontier": frontier,
              "promotion_passed": False, "execution_enabled": False,
              "limitations": ["Previously inspected history, no new out-of-sample evidence or forward observations.",
                              "Portfolio hypotheses inspired by selected SOXL history; multiplicity remains.",
                              "Fractional fills, constant proportional costs; no independent prices/open-depth verification.",
                              "Close drawdown is not intraday drawdown; cash earns zero except explicitly named SHY variant."]}
    (output/"report.json").write_text(json.dumps(report, indent=2, allow_nan=False))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--transfer", type=Path, default=ROOT/"data/yfinance_etf_transfer_20260905.pkl")
    parser.add_argument("--broad", type=Path, default=ROOT/"data/yfinance_etf_factor_20260905.pkl")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.transfer, args.broad, args.output)
