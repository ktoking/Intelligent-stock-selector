#!/usr/bin/env python3
"""Predeclared sector-factor satellites around equity/leveraged ETF cores.

Uses real ETF price paths; it never scales another instrument's realized P&L.
All output is research, with known history reuse and fund-selection limitations.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.equity_factor_lab import bootstrap, daily_data, metrics, simulate

SECTORS = ("XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY")
CORE_REFERENCE = {"SPY": "SPY", "UPRO": "SPY", "QQQ": "QQQ", "TQQQ": "QQQ"}


@dataclass(frozen=True)
class Config:
    core: str
    core_fraction: float
    satellite: str
    target_vol: float

    @property
    def name(self):
        return f"{self.core}_core{self.core_fraction:g}_{self.satellite}_vol{self.target_vol:g}"


def feature_cache(p):
    c = p["close"]
    returns = c.pct_change(fill_method=None)
    m20, m63, m126 = (c.pct_change(n, fill_method=None) for n in (20, 63, 126))
    sector_momentum = (.5 * m20[list(SECTORS)].rank(axis=1, pct=True)
                       + .3 * m63[list(SECTORS)].rank(axis=1, pct=True)
                       + .2 * m126[list(SECTORS)].rank(axis=1, pct=True))
    residual = {}
    for ref in ("SPY", "QQQ"):
        beta = pd.DataFrame({s: returns[s].rolling(63).cov(returns[ref]) /
                             returns[ref].rolling(63).var() for s in SECTORS})
        residual[ref] = (m63[list(SECTORS)] - beta.mul(m63[ref], axis=0)).rank(axis=1, pct=True)
    covariance = returns.rolling(63).cov().to_numpy().reshape(len(c), len(c.columns), len(c.columns)) * 252
    return {"momentum": sector_momentum, "residual": residual,
            "eligible": (c[list(SECTORS)] > c[list(SECTORS)].rolling(63).mean()) & (m63[list(SECTORS)] > 0),
            "risk_on": (c > c.rolling(200).mean()) & (m63 > 0),
            "defensive_momentum": m63[["GLD", "IEF"]], "covariance": covariance}


def weights(p, features, cfg):
    c = p["close"]
    names = list(c.columns)
    loc = {s: names.index(s) for s in names}
    ref = CORE_REFERENCE[cfg.core]
    scores = (features["momentum"] if cfg.satellite == "momentum" else features["residual"][ref]).to_numpy()
    eligibility = features["eligible"].to_numpy()
    risk_on = features["risk_on"][ref].to_numpy()
    defensive = features["defensive_momentum"].to_numpy()
    result = np.zeros(c.shape)
    for i in range(200, len(c)):
        w = result[i]
        if risk_on[i]:
            w[loc[cfg.core]] = cfg.core_fraction
            available = np.where(eligibility[i] & np.isfinite(scores[i]))[0]
            chosen = available[np.argsort(-scores[i, available], kind="stable")[:3]]
            if len(chosen):
                for s in chosen:
                    w[loc[SECTORS[s]]] = (1 - cfg.core_fraction) / len(chosen)
        else:
            best = int(np.nanargmax(defensive[i]))
            asset = ("GLD", "IEF")[best] if defensive[i, best] > 0 else "SHY"
            w[loc[asset]] = 1.
        variance = float(w @ features["covariance"][i] @ w)
        if not np.isfinite(variance):
            raise ValueError("missing covariance for an actionable date")
        vol = np.sqrt(max(0, variance))
        if vol > cfg.target_vol:
            w *= cfg.target_vol / vol
    return pd.DataFrame(result, index=c.index, columns=c.columns)


def select(board):
    qualified = [r for r in board if all(r[part]["return_pct"] > 0 and
                  r[part]["max_drawdown_close_pct"] <= 35 for part in ("train", "validation"))]
    def score(r):
        return min(r[part]["annualized_pct"] / max(5., r[part]["max_drawdown_close_pct"])
                   for part in ("train", "validation"))
    return max(qualified, key=lambda r: (score(r), r["name"])) if qualified else None


def attribution(daily, benchmark_daily):
    r = np.array([x["return"] for x in daily])
    b = np.array([x["return"] for x in benchmark_daily])
    beta = float(np.cov(r, b, ddof=1)[0, 1] / b.var(ddof=1)) if b.var() else 0.
    return {"beta": beta, "arithmetic_alpha_zero_rf_pct_pa": float((r.mean()-beta*b.mean())*25200),
            "correlation": float(np.corrcoef(r,b)[0,1]) if r.std() and b.std() else None,
            "note": "descriptive regression, not causal alpha or significance proof"}


def yearly(daily):
    return {year: metrics([r for r in daily if r["date"].startswith(year)])
            for year in sorted({r["date"][:4] for r in daily})}


def run(source, output):
    if (output / "report.json").exists():
        raise FileExistsError("completed research is immutable")
    output.mkdir(parents=True, exist_ok=True)
    cfgs = [Config(core, fraction, family, vol) for core in CORE_REFERENCE
            for fraction in (.5, .75) for family in ("momentum", "residual") for vol in (.25, .4)]
    provenance = {}
    for path in (Path(__file__), ROOT/"scripts/equity_factor_lab.py"):
        payload = path.read_bytes()
        (output / path.name).write_bytes(payload)
        provenance[path.name] = hashlib.sha256(payload).hexdigest()
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "source_code": provenance,
                "configs": [asdict(c) for c in cfgs], "sector_universe": SECTORS,
                "warmup": 201, "train": 252, "validation": 126, "test": 126, "final_diagnostic": 63,
                "selection": "positive train/validation, drawdown<=35%, maximize worse-segment Calmar with 5% denominator floor",
                "costs_bps_per_side": [7, 12.5, 20], "rebalance_sessions": 5,
                "defense": "core reference <=200d MA or nonpositive 63d momentum: best positive 63d GLD/IEF else SHY",
                "annual_goal_pct": [40,110], "daily_goal_pct": 2,
                "history_reuse": "2026-06-08 through 2026-09-04 already opened; all final results diagnostic"}
    (output/"manifest.json").write_text(json.dumps(manifest, indent=2))
    p, data = daily_data(source)
    features = feature_cache(p)
    targets = {cfg.name: weights(p, features, cfg) for cfg in cfgs}
    cfgmap = {cfg.name: cfg for cfg in cfgs}
    n = len(p["close"])
    first, last = 201+252+126, n-63
    if last <= first:
        raise ValueError("not enough data for specified folds")
    def selection(end):
        board = [{"name": cfg.name,
                  "train": simulate(p, targets[cfg.name], end-378, end-126)["metrics"],
                  "validation": simulate(p, targets[cfg.name], end-126, end)["metrics"]} for cfg in cfgs]
        return select(board), board
    def append_sim(series, target, start, end, cost=7.):
        result = simulate(p, target, start, end, cost_bps=cost,
                          initial_equity=series[-1]["equity"] if series else 100_000.)
        series.extend(result["daily"])
        return result
    daily, orders, folds = [], [], []
    stress = {12.5: [], 20.: []}
    groups = {name: [] for name in ("SPY", "QQQ", "UPRO", "TQQQ")}
    group_choices = {name: [] for name in groups}
    cash = next(iter(targets.values())) * 0
    for start in range(first, last, 126):
        end = min(start+126,last)
        chosen, board = selection(start)
        target = targets[chosen["name"]] if chosen else cash
        result = append_sim(daily, target, start, end)
        orders.extend({**order, "fold": len(folds)} for order in result["orders"])
        for cost in stress:
            append_sim(stress[cost], target, start, end, cost)
        for core in groups:
            pick = select([r for r in board if cfgmap[r["name"]].core == core])
            group_choices[core].append(pick["name"] if pick else "cash")
            append_sim(groups[core], targets[pick["name"]] if pick else cash, start, end)
        folds.append({"start": str(p["close"].index[start]), "end": str(p["close"].index[end-1]),
                      "chosen": chosen, "leaderboard": board, "test": result["metrics"]})
        print(f"fold {len(folds)} {folds[-1]['start']}: {chosen['name'] if chosen else 'cash'} test_return={result['metrics']['return_pct']:.2f}%", flush=True)
    pick, board = selection(last)
    (output/"final_selection_before_evaluation.json").write_text(json.dumps({"selected":pick,"board":board},indent=2))
    target = targets[pick["name"]] if pick else cash
    final = simulate(p, target, last, n)
    benchmarks, benchmark_daily = {}, {}
    for asset in CORE_REFERENCE:
        target_b = cash.copy()
        target_b[asset] = 1.
        result = simulate(p, target_b, first, last, rebalance=10_000)
        benchmarks[asset] = {"walk":result["metrics"],"final":simulate(p,target_b,last,n,rebalance=10_000)["metrics"]}
        benchmark_daily[asset] = result["daily"]
        pd.DataFrame(result["daily"]).to_csv(output/f"benchmark_{asset}_daily.csv",index=False)
    report = {"manifest":manifest,"data":data,"walkforward":{"metrics":metrics(daily),"folds":folds,
                 "bootstrap":bootstrap(daily),"cost_stress":{str(k):metrics(v) for k,v in stress.items()},
                 "by_year":yearly(daily),"attribution":{k:attribution(daily,v) for k,v in benchmark_daily.items() if k in ("SPY","QQQ")}},
              "core_groups":{k:{"metrics":metrics(v),"choices":group_choices[k],"by_year":yearly(v)} for k,v in groups.items()},
              "final_diagnostic":{"start":str(p["close"].index[last]),"end":str(p["close"].index[-1]),
                 "selected":pick,"metrics":final["metrics"],"bootstrap":bootstrap(final["daily"]),
                 "cost_stress":{str(c):simulate(p,target,last,n,cost_bps=c)["metrics"] for c in stress}},
              "benchmarks":benchmarks,"promotion_passed":False,"execution_enabled":False,
              "limitations":["ETF selection is retrospective; sector portfolios rebalance within historical ETF prices, no current single-stock membership backfill.",
                  "Actual TQQQ/UPRO price paths include daily-reset effects; not 3x-scaled QQQ/SPY returns. Account has no borrowed cash but embeds ETF leverage.",
                  "Adjusted OHLC total-return approximation needs independent corporate-action verification.",
                  "Fractional shares and proportional spread/fees approximation; no auctions/volume capacity/intraday drawdown modeled.",
                  "Strategies were designed after prior studies; temporal folds are historical diagnostics, not pristine forward evidence.",
                  "Final 63 days previously seen, not eligible for untouched holdout claims."]}
    (output/"report.json").write_text(json.dumps(report,indent=2,allow_nan=False))
    for name,rows in (("walkforward_daily",daily),("walkforward_orders",orders),("final_daily",final["daily"]),("final_orders",final["orders"])):
        pd.DataFrame(rows).to_csv(output/f"{name}.csv",index=False)
    for core,rows in groups.items():
        pd.DataFrame(rows).to_csv(output/f"core_{core}_daily.csv",index=False)
    print(json.dumps({"output":str(output),"walk":report["walkforward"]["metrics"],
                      "final":report["final_diagnostic"]["metrics"]},indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.source,args.output)
