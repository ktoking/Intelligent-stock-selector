#!/usr/bin/env python3
"""Reproducible lag/cost and paired-return diagnostics for all frozen portfolios."""
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
from scripts.equity_factor_lab import metrics, simulate as reference_simulate
from scripts.etf_portfolio_factor_lab import load_panels, targets
from scripts.factor_portfolio_engine import simulate


def run(folder):
    destination = folder/"robustness.json"
    if destination.exists():
        raise FileExistsError("audit is immutable")
    source = folder/"report.json"
    report = json.loads(source.read_text())
    manifest = report["manifest"]
    for name, expected in manifest["source_sha256"].items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest() != expected:
            raise ValueError("input data changed")
    for name, expected in manifest["code_sha256"].items():
        if hashlib.sha256((ROOT/"scripts"/name).read_bytes()).hexdigest() != expected:
            raise ValueError("research code changed")
    (folder/Path(__file__).name).write_bytes(Path(__file__).read_bytes())
    inputs = list(manifest["source_sha256"])
    p, _ = load_panels(Path(inputs[0]), Path(inputs[1]))
    dates = p["close"].index
    first, split = dates.get_loc("2013-04-24"), dates.get_loc("2026-06-08")
    baseline = pd.read_csv(folder/"soxl100_cash_primary_daily.csv")
    prior = pd.read_csv(ROOT/"data/factor_lab/20260905_etf_transfer_v1/SOXL_ema20_100_vol60_primary_daily.csv")
    np.testing.assert_allclose(baseline.equity, prior.equity, rtol=0, atol=1e-7)
    rng = np.random.default_rng(20260905)
    n, count = len(baseline), 2000
    # Same sampled five-session blocks for both portfolios preserve paired observations.
    starts = rng.integers(0, n, size=(count, (n+4)//5))
    positions = ((starts[..., None]+np.arange(5)) % n).reshape(count, -1)[:, :n]
    base_log = np.log1p(baseline["return"].to_numpy())
    base_bootstrap = np.expm1(base_log[positions].mean(axis=1)*252)*100
    rows = []
    for config in manifest["configs"]:
        name = config["name"]
        w = targets(p["close"], config)
        checks = {}
        for lag in (1, 2):
            delayed = w.shift(lag).fillna(0.)
            for cost in (7, 20):
                checks[f"extra_lag{lag}_cost{cost}"] = simulate(p, delayed, first, split,
                    cost_bps=cost, record_orders=False)["metrics"]
        checks["full_cost20"] = simulate(p, w, first, len(dates), cost_bps=20, record_orders=False)["metrics"]
        daily = pd.read_csv(folder/f"{name}_primary_daily.csv")
        full = pd.read_csv(folder/f"{name}_full_daily.csv")
        checks["continuous_recent"] = metrics(full.loc[full.date >= "2026-06-08"].to_dict("records"))
        log_returns = np.log1p(daily["return"].to_numpy())
        boot = np.expm1(log_returns[positions].mean(axis=1)*252)*100
        checks["paired_bootstrap"] = {
            "samples": count, "block_sessions": 5,
            "cagr_interval95_pct": np.percentile(boot, [2.5, 97.5]).tolist(),
            "cagr_difference_vs_soxl95_pp": np.percentile(boot-base_bootstrap, [2.5, 97.5]).tolist(),
            "limitation": "conditional historical block resampling; not multiplicity-adjusted, not probability of future success",
        }
        rolling = pd.Series(log_returns).rolling(252).sum().dropna().pipe(np.expm1)*100
        checks["rolling252"] = {"min_return_pct": float(rolling.min()),
            "median_return_pct": float(rolling.median()), "share_ge40": float((rolling>=40).mean())}
        # Reference implementation parity on the actual multi-asset path, not broker execution validation.
        if name in ("soxl100_cash", "soxl100_gold", "soxl50_TQQQ_gold_bond"):
            independent = pd.DataFrame(reference_simulate(p, w, first, split, rebalance=1, cost_bps=7.)["daily"])
            error = float((independent.equity-daily.equity).abs().max())
            np.testing.assert_allclose(independent.equity, daily.equity, rtol=1e-11, atol=1e-6)
            checks["reference_engine_max_equity_error_usd"] = error
        curve = np.r_[1., np.exp(np.cumsum(log_returns))]
        underwater = curve[1:] < np.maximum.accumulate(curve)[1:]*(1-1e-12)
        longest = current = 0
        for flag in underwater:
            current = current+1 if flag else 0
            longest = max(longest, current)
        checks["longest_underwater_sessions"] = longest
        rows.append({"name": name, **checks})
        print(f"audited {name}", flush=True)
    output = {"created_at": datetime.now(timezone.utc).isoformat(),
        "report_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "audit_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "baseline_reproduced": True, "results": rows, "promotion_passed": False, "execution_enabled": False}
    destination.write_text(json.dumps(output, indent=2, allow_nan=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder", type=Path, required=True)
    run(parser.parse_args().folder)
