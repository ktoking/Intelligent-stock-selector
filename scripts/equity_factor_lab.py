#!/usr/bin/env python3
"""Causal, cash-account factor research on cached underlying equity prices.

Research only: no broker, credentials, order endpoint, or runtime config writes.
The old cached universe is selected retrospectively, so even temporal holdouts
are diagnostics rather than evidence of an untouched investable universe.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys

import exchange_calendars as xcals
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data/yfinance_hourly_2y.pkl"
FAMILIES = ("momentum", "risk_adjusted_momentum", "residual_momentum",
            "trend_efficiency", "trend_pullback", "volume_momentum")


@dataclass(frozen=True)
class Config:
    family: str
    top: int = 3
    market_gate: bool = True
    rebalance_sessions: int = 5
    target_volatility: float = 0.25
    max_weight: float = 1 / 3

    @property
    def name(self):
        return f"{self.family}_top{self.top}_gate{int(self.market_gate)}"


def daily_data(path=CACHE):
    raw = pd.read_pickle(path)  # Only the user-owned existing local cache.
    calendar = xcals.get_calendar("XNYS")
    daily, problems = {}, []
    for symbol, source in raw.items():
        f = source.copy().sort_index()
        if f.index.tz is None:
            # Explicit daily-cache contract: naive midnight exchange dates.
            if any(stamp.time() != datetime.min.time() for stamp in f.index):
                raise ValueError("naive timestamps must be daily midnight bars")
            ratio = f["adj close"] / f["close"]
            f.loc[:, ["open", "high", "low", "close"]] = f[["open", "high", "low", "close"]].mul(ratio, axis=0)
            f.index = f.index.strftime("%Y-%m-%d")
            if f.index.has_duplicates:
                raise ValueError(f"duplicate daily dates: {symbol}")
            daily[symbol] = f
            continue
        f.index = pd.to_datetime(f.index, utc=True)
        if f.index.has_duplicates:
            raise ValueError(f"duplicate source timestamps: {symbol}")
        ratio = f["adj close"] / f["close"]
        f.loc[:, ["open", "high", "low", "close"]] = f[["open", "high", "low", "close"]].mul(ratio, axis=0)
        f["session"] = f.index.tz_convert("America/New_York").strftime("%Y-%m-%d")
        rows = []
        for day, group in f.groupby("session"):
            if not calendar.is_session(day):
                continue
            start, end = calendar.session_open(day), calendar.session_close(day)
            expected = pd.date_range(start, end, freq="60min", inclusive="left")
            part = group.loc[(group.index >= start) & (group.index < end)]
            if part.index.tolist() != expected.tolist():
                problems.append({"symbol": symbol, "date": day, "reason": "incomplete_hourly_session"})
                continue
            rows.append({"date": day, "open": part.open.iloc[0], "high": part.high.max(),
                         "low": part.low.min(), "close": part.close.iloc[-1], "volume": part.volume.sum()})
        daily[symbol] = pd.DataFrame(rows).set_index("date")
    required = list(raw)
    days = calendar.sessions_in_range(min(f.index[0] for f in daily.values()),
                                      max(f.index[-1] for f in daily.values())).strftime("%Y-%m-%d")
    panels = {field: pd.DataFrame({s: daily[s][field] for s in required}).reindex(days)
              for field in ("open", "high", "low", "close", "volume")}
    # Missing OHLC is not a zero-return day. Forward fill is used only to repair
    # neither a price nor a session: the run fails until the source is complete.
    missing = panels["close"].isna().stack()
    if missing.any():
        raise ValueError(f"incomplete equity session panel: {missing[missing].index.tolist()[:15]}")
    for field in ("open", "high", "low", "close"):
        if not np.isfinite(panels[field].to_numpy()).all() or (panels[field] <= 0).any().any():
            raise ValueError(f"invalid prices in {field}")
    jumps = panels["close"].pct_change().abs()
    if (jumps > .65).any().any():
        raise ValueError("daily jump >65%; inspect corporate actions before research")
    return panels, {"source": str(path), "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "start": str(days[0]), "end": str(days[-1]), "sessions": len(days),
                    "symbols": required, "missing_sessions": problems,
                    "adjustment": "cached adj-close/close applied to OHLC; dividends/splits not independently verified"}


def factor_scores(p):
    c, v = p["close"], p["volume"]
    r = c.pct_change(fill_method=None)
    vol = r.rolling(20).std()
    m20, m60, m120 = (c.pct_change(n, fill_method=None) for n in (20, 60, 120))
    rank = lambda x: x.rank(axis=1, pct=True)
    beta = pd.DataFrame({s: r[s].rolling(60).cov(r.QQQ) / r.QQQ.rolling(60).var() for s in c})
    residual = m60 - beta.mul(m60.QQQ, axis=0)
    efficiency = (c - c.shift(60)) / c.diff().abs().rolling(60).sum()
    pullback = -c.pct_change(3, fill_method=None) / vol
    return {
        "momentum": .5 * rank(m20) + .3 * rank(m60) + .2 * rank(m120),
        "risk_adjusted_momentum": rank((.5 * m20 + .5 * m60) / vol),
        "residual_momentum": rank(residual),
        "trend_efficiency": rank(efficiency),
        "trend_pullback": rank(pullback),
        "volume_momentum": .7 * rank(m20) + .3 * rank(v.rolling(5).mean() / v.rolling(20).mean()),
    }


def target_weights(p, scores, cfg):
    c = p["close"]
    symbols = [s for s in c if s not in ("QQQ", "SPY")]
    returns = c[symbols].pct_change(fill_method=None)
    eligible = (c[symbols] > c[symbols].rolling(60).mean()) & (c[symbols].pct_change(60) > 0)
    weights = pd.DataFrame(0., index=c.index, columns=c.columns)
    for i in range(120, len(c)):
        if cfg.market_gate and c.QQQ.iloc[i] <= c.QQQ.iloc[i-59:i+1].mean():
            continue
        score = scores[cfg.family].iloc[i][symbols].where(eligible.iloc[i]).dropna()
        chosen = score.sort_values(ascending=False, kind="stable").head(cfg.top).index
        if not len(chosen):
            continue
        w = np.repeat(min(cfg.max_weight, 1 / len(chosen)), len(chosen))
        covariance = returns.iloc[i-59:i+1][chosen].cov().to_numpy() * 252
        expected_vol = float(np.sqrt(max(0, w @ covariance @ w)))
        w *= min(1., cfg.target_volatility / expected_vol) if expected_vol > 0 else 1.
        weights.loc[c.index[i], chosen] = w
    return weights


def simulate(p, targets, start, end, *, cost_bps=7., rebalance=5, initial_equity=100_000.):
    """Close(t-1) signal -> open(t) fills; positions drift between rebalances.

    Cost applies to each actual buy/sell notional. Sells precede buys, buys
    shrink to available cash, no leverage/shorts, and boundary liquidation
    occurs at the predeclared final close with the same cost assumption.
    """
    if start < 1 or end <= start:
        raise ValueError("simulation requires prior signal and nonempty interval")
    fee = cost_bps / 10_000
    cash, shares, previous = initial_equity, np.zeros(len(targets.columns)), initial_equity
    daily, orders = [], []
    symbols = list(targets.columns)
    for i in range(start, end):
        day = str(p["open"].index[i])
        op, cl = p["open"].iloc[i].to_numpy(), p["close"].iloc[i].to_numpy()
        traded = costs = 0.
        if (i - start) % rebalance == 0:
            target = targets.iloc[i-1].to_numpy()
            if np.min(target) < 0 or target.sum() > 1 + 1e-10:
                raise ValueError("invalid long-only cash allocation")
            equity = cash + shares @ op
            wanted = equity * target / op
            sell = np.maximum(shares - wanted, 0)
            cash += (sell * op).sum() * (1 - fee)
            shares -= sell
            buy = np.maximum(wanted - shares, 0)
            needed = (buy * op).sum() * (1 + fee)
            buy *= min(1., max(0., cash) / needed) if needed > 0 else 1.
            cash -= (buy * op).sum() * (1 + fee)
            shares += buy
            for j in range(len(symbols)):
                for side, qty in (("SELL", sell[j]), ("BUY", buy[j])):
                    if qty > 1e-8:
                        notional = float(qty * op[j])
                        orders.append({"date": day, "signal_date": str(targets.index[i-1]),
                                       "symbol": symbols[j], "side": side, "quantity": float(qty),
                                       "price": float(op[j]), "notional": notional,
                                       "cost": notional * fee, "fill": "next_open"})
                        traded += notional
                        costs += notional * fee
        if i == end - 1:
            for j, qty in enumerate(shares):
                if qty > 1e-8:
                    notional = float(qty * cl[j])
                    orders.append({"date": day, "symbol": symbols[j], "side": "SELL",
                                   "quantity": float(qty), "price": float(cl[j]), "notional": notional,
                                   "cost": notional * fee, "fill": "predeclared_final_close"})
                    traded += notional
                    costs += notional * fee
            cash += (shares @ cl) * (1 - fee)
            shares[:] = 0
        equity = float(cash + shares @ cl)
        if cash < -1e-6 or equity <= 0:
            raise ValueError("cash account insolvency")
        daily.append({"date": day, "return": equity / previous - 1, "equity": equity,
                      "cash": float(cash), "gross_weight": float(shares @ cl / equity),
                      "turnover": traded / previous, "cost": costs})
        previous = equity
    return {"daily": daily, "orders": orders, "metrics": metrics(daily)}


def metrics(daily):
    r = np.array([x["return"] for x in daily])
    if not len(r):
        return {"sessions": 0}
    curve = np.r_[1., np.cumprod(1 + r)]
    dd = 1 - curve / np.maximum.accumulate(curve)
    positive, negative = r[r > 0].sum(), -r[r < 0].sum()
    return {"sessions": len(r), "return_pct": float((curve[-1] - 1) * 100),
            "annualized_pct": float((curve[-1] ** (252 / len(r)) - 1) * 100),
            "max_drawdown_close_pct": float(dd.max() * 100),
            "sharpe_zero_rf": float(r.mean() / r.std(ddof=1) * np.sqrt(252)) if len(r) > 1 and r.std() else 0.,
            "daily_profit_factor": float(positive / negative) if negative else None,
            "geometric_daily_pct": float((curve[-1] ** (1 / len(r)) - 1) * 100),
            "days_ge_2pct": int((r >= .02).sum()), "days_ge_2pct_rate": float((r >= .02).mean()),
            "worst_day_pct": float(r.min() * 100), "mean_gross_weight": float(np.mean([x["gross_weight"] for x in daily])),
            "turnover_sum": float(sum(x["turnover"] for x in daily)),
            "cost_usd": float(sum(x["cost"] for x in daily))}


def bootstrap(daily, count=3000):
    r = np.array([x["return"] for x in daily])
    rng = np.random.default_rng(20260905)
    starts = rng.integers(0, len(r), size=(count, (len(r) + 4) // 5))
    ix = ((starts[..., None] + np.arange(5)) % len(r)).reshape(count, -1)[:, :len(r)]
    cagr = (np.prod(1 + r[ix], axis=1) ** (252 / len(r)) - 1) * 100
    return {"method": "circular five-session block bootstrap; conditional on selected strategy, not selection-adjusted",
            "samples": count, "annualized_pct_interval_95": np.percentile(cagr, [2.5, 97.5]).tolist(),
            "share_cagr_ge_40": float((cagr >= 40).mean())}


def choose(candidates):
    qualified = [r for r in candidates if all(r[part]["return_pct"] > 0 and
                 r[part]["max_drawdown_close_pct"] <= 30 and r[part]["sharpe_zero_rf"] > 0
                 for part in ("train", "validation"))]
    return max(qualified, key=lambda r: (min(r["train"]["sharpe_zero_rf"],
                r["validation"]["sharpe_zero_rf"]), r["name"])) if qualified else None


def factor_ic(p, scores, start, end):
    symbols = [s for s in p["open"] if s not in ("QQQ", "SPY")]
    future = p["open"].shift(-6) / p["open"].shift(-1) - 1
    result = {}
    for name, score in scores.items():
        values = []
        # Five-session spacing; labels resolve entirely inside development.
        for i in range(start, end-6, 5):
            pair = pd.DataFrame({"x": score.iloc[i][symbols], "y": future.iloc[i][symbols]}).dropna()
            if len(pair) >= 8 and pair.x.nunique() > 1:
                values.append(float(pair.x.corr(pair.y, method="spearman")))
        result[name] = {"observations": len(values), "mean_rank_ic": float(np.mean(values)),
                        "positive_rate": float(np.mean(np.array(values) > 0)),
                        "use": "development attribution only; not used to choose configs"}
    return result


def framework_check(p):
    """Independent next-open/fee calculation on real QQQ daily OHLC."""
    from backtesting import Backtest, Strategy
    frame = pd.DataFrame({k.title(): p[k].QQQ.iloc[:25].to_numpy() for k in ("open", "high", "low", "close", "volume")},
                         index=pd.to_datetime(p["close"].index[:25]))
    class Tape(Strategy):
        def init(self):
            pass
        def next(self):
            if len(self.data) == 2:
                self.buy(size=1)
            if len(self.data) == 24:
                self.position.close()
    result = Backtest(frame, Tape, cash=100_000, commission=.0007,
                      trade_on_close=False, finalize_trades=False).run()
    entry, exit_ = float(frame.Open.iloc[2]), float(frame.Open.iloc[24])
    expected = 100_000 + exit_ - entry - .0007 * (entry + exit_)
    actual = float(result["Equity Final [$]"])
    trades = result["_trades"]
    passed = abs(expected - actual) < 1e-7 and len(trades) == 1 and int(trades.EntryBar.iloc[0]) == 2 and int(trades.ExitBar.iloc[0]) == 24
    if not passed:
        raise ValueError("independent backtesting.py next-open accounting check failed")
    return {"passed": passed, "version": importlib.metadata.version("backtesting"),
            "scope": "real QQQ one-unit next-open entry/exit and two-sided fees; not full multiasset equivalence",
            "expected_final_equity": expected, "framework_final_equity": actual}


def run(output, source=CACHE):
    if (output / "report.json").exists():
        raise FileExistsError("completed research is immutable; use a new output directory")
    output.mkdir(parents=True, exist_ok=True)
    (output / "strategy_source.py").write_bytes(Path(__file__).read_bytes())
    configs = [Config(family, top, gate) for family in FAMILIES for top in (3, 5) for gate in (False, True)]
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(),
                "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "configs": [asdict(c) for c in configs], "cost_bps_per_side": [7, 12.5, 20],
                "warmup": 121, "train": 126, "validation": 42, "test": 42, "final_diagnostic": 63,
                "selection": "positive train and validation, close drawdown<=30%, maximize worse-segment Sharpe",
                "target": {"annualized_min_pct": 40, "annualized_max_pct": 110, "daily_hit_pct": 2},
                "status": "preregistered within this run, existing cache/universe is retrospective"}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    p, provenance = daily_data(source)
    scores = factor_scores(p)
    weights = {c.name: target_weights(p, scores, c) for c in configs}
    config_by_name = {c.name: c for c in configs}
    warmup, final_start = 121, len(p["close"]) - 63
    if final_start - warmup < 210:
        raise ValueError("insufficient complete sessions for specified folds")
    def selection(end):
        vals = []
        for c in configs:
            train = simulate(p, weights[c.name], end-168, end-42)["metrics"]
            validation = simulate(p, weights[c.name], end-42, end)["metrics"]
            vals.append({"name": c.name, "train": train, "validation": validation})
        return choose(vals), vals
    folds, walk_daily, walk_orders, stress_daily = [], [], [], {12.5: [], 20: []}
    family_daily = {f: [] for f in FAMILIES}
    family_folds = {f: [] for f in FAMILIES}
    for start in range(warmup+168, final_start, 42):
        end = min(start+42, final_start)
        selected, board = selection(start)
        target = weights[selected["name"]] if selected else weights[configs[0].name] * 0
        outcome = simulate(p, target, start, end, initial_equity=walk_daily[-1]["equity"] if walk_daily else 100_000.)
        walk_daily.extend(outcome["daily"])
        walk_orders.extend({**order, "fold": len(folds)} for order in outcome["orders"])
        for cost in stress_daily:
            prior_equity = stress_daily[cost][-1]["equity"] if stress_daily[cost] else 100_000.
            stress_daily[cost].extend(simulate(p, target, start, end, cost_bps=cost, initial_equity=prior_equity)["daily"])
        folds.append({"test_start": str(p["close"].index[start]), "test_end": str(p["close"].index[end-1]),
                      "selected": selected, "leaderboard": board, "test": outcome["metrics"]})
        for family in FAMILIES:
            winner = choose([r for r in board if config_by_name[r["name"]].family == family])
            family_target = weights[winner["name"]] if winner else target * 0
            prior_equity = family_daily[family][-1]["equity"] if family_daily[family] else 100_000.
            test = simulate(p, family_target, start, end, initial_equity=prior_equity)
            family_daily[family].extend(test["daily"])
            family_folds[family].append({"selected": winner["name"] if winner else "cash", "test": test["metrics"]})
        print(f"fold {len(folds)}: {selected['name'] if selected else 'cash'} {outcome['metrics']['annualized_pct']:.2f}% CAGR", flush=True)
    selected, board = selection(final_start)
    (output / "final_selection_before_evaluation.json").write_text(json.dumps({"selected": selected, "leaderboard": board}, indent=2))
    target = weights[selected["name"]] if selected else weights[configs[0].name] * 0
    final = simulate(p, target, final_start, len(p["close"]))
    family_results = []
    for family in FAMILIES:
        winner = choose([r for r in board if config_by_name[r["name"]].family == family])
        family_results.append({"family": family, "selected_from_development": winner,
                               "walkforward": metrics(family_daily[family]), "walkforward_folds": family_folds[family],
                               "final_not_evaluated": True})
    benchmarks = {}
    for name in ("QQQ", "equal_weight_universe"):
        w = pd.DataFrame(0., index=p["close"].index, columns=p["close"].columns)
        names = [name] if name == "QQQ" else [s for s in w if s not in ("QQQ", "SPY")]
        w[names] = 1 / len(names)
        benchmarks[name] = {"final": simulate(p, w, final_start, len(w), rebalance=20)["metrics"],
                            "walk_period": simulate(p, w, warmup+168, final_start, rebalance=20)["metrics"]}
    report = {"manifest": manifest, "data": provenance, "factor_ic_development": factor_ic(p, scores, warmup, final_start),
              "families": family_results, "walkforward": {"folds": folds, "metrics": metrics(walk_daily),
                  "bootstrap": bootstrap(walk_daily), "cost_stress": {str(k): metrics(v) for k,v in stress_daily.items()}},
              "final_diagnostic": {"start": str(p["close"].index[final_start]), "end": str(p["close"].index[-1]),
                  "selected": selected, "metrics": final["metrics"], "bootstrap": bootstrap(final["daily"]),
                  "cost_stress": {str(c): simulate(p, target, final_start, len(p["close"]), cost_bps=c)["metrics"] for c in (12.5, 20)}},
              "benchmarks": benchmarks, "framework_check": framework_check(p),
              "promotion_passed": False, "execution_enabled": False,
              "limitations": ["Retrospective technology-heavy survivor universe; no point-in-time membership.",
                  "Existing history previously inspected in other studies; temporal holdout is not pristine.",
                  "Underlying equities, not OKX perpetuals; no funding, borrow or leverage modeled.",
                  "Costs 7bps/side proxy, fractional shares, no participation limits; close-only drawdown understates intraday risk.",
                  "Cached adjusted OHLC needs independent corporate-action and source verification.",
                  "Annualization of short folds is extrapolation, not promised or verified future returns.",
                  "Each fold liquidates at its boundary and carries equity forward; boundary costs included."]}
    (output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False))
    pd.DataFrame(walk_daily).to_csv(output / "walkforward_daily.csv", index=False)
    pd.DataFrame(walk_orders).to_csv(output / "walkforward_orders.csv", index=False)
    pd.DataFrame(final["daily"]).to_csv(output / "final_daily.csv", index=False)
    pd.DataFrame(final["orders"]).to_csv(output / "final_orders.csv", index=False)
    print(json.dumps({"output": str(output), "walkforward": report["walkforward"]["metrics"],
                      "final": report["final_diagnostic"]["metrics"], "promotion_passed": False}, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=CACHE)
    args = parser.parse_args()
    run(args.output, args.source)
