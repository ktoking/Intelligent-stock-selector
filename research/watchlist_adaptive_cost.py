"""Retrospective cost-control and causal adaptive-factor extension.

Uses the unchanged daily_factor_lab execution engine. No network or orders.
All hypotheses are written before the run; this is a follow-up after prior
historical results were seen, not an untouched holdout.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import time

import numpy as np
import pandas as pd

from research.daily_factor_lab.core import (
    DEFAULT_DATA, Panel, Policy, benchmark, catalog, load_panel, simulate,
)
from research.daily_factor_lab.experiment import (
    FOLDS, WINDOWS, attribution, comparison, plot_result, rolling_comparison, write_json,
)


FACTORS = ("momentum_risk", "residual_momentum", "near_high")
LABEL_HORIZON = 20
IC_LOOKBACK = 63
IC_MIN_PERIODS = 20
LEARNED_SHARE = .5


def adaptive_scores(panel, *, horizon=LABEL_HORIZON, lookback=IC_LOOKBACK,
                    min_periods=IC_MIN_PERIODS, learned_share=LEARNED_SHARE,
                    min_stocks=30):
    """Weights at close t use only labels whose terminal close <= t.

    A label for signal s is close[s+horizon]/open[s+1]-1; it is unavailable
    until close s+horizon. The initial universe, missing terminal observations,
    and overlapping labels remain research limitations, not significance proof.
    """
    if horizon < 1 or min_periods < 1 or lookback < min_periods or not 0 <= learned_share <= 1:
        raise ValueError("Invalid causal score parameters")
    n_days, n_symbols = panel.fields["close"].shape
    n_factors = len(FACTORS)
    raw = np.stack([panel.fields[name] for name in FACTORS], axis=-1)
    eligible = (panel.stock_mask[None, :] & (panel.fields["liquid20"] >= 5e6)
                & np.isfinite(raw).all(axis=-1))
    ranks = np.full_like(raw, np.nan)
    for k in range(n_factors):
        frame = pd.DataFrame(np.where(eligible, raw[:, :, k], np.nan))
        ranks[:, :, k] = frame.rank(axis=1, pct=True).to_numpy()
    matured_ic = np.full((n_days, n_factors), np.nan)
    for terminal in range(horizon, n_days):
        signal = terminal - horizon
        labels = panel.fields["close"][terminal] / panel.fields["open"][signal + 1] - 1
        valid = eligible[signal] & np.isfinite(labels)
        if valid.sum() < min_stocks:
            continue
        target_ranks = pd.Series(labels[valid]).rank().to_numpy()
        for k in range(n_factors):
            values = ranks[signal, valid, k]
            if values.std() > 0 and target_ranks.std() > 0:
                matured_ic[terminal, k] = np.corrcoef(values, target_ranks)[0, 1]
    trailing = pd.DataFrame(matured_ic).rolling(lookback, min_periods=min_periods).mean().to_numpy()
    positive = np.maximum(np.nan_to_num(trailing, nan=0.), 0.)
    denominator = positive.sum(axis=1, keepdims=True)
    learned = np.divide(positive, denominator,
                        out=np.full_like(positive, 1 / n_factors), where=denominator > 0)
    weights = (1 - learned_share) / n_factors + learned_share * learned
    adaptive = np.sum(ranks * weights[:, None, :], axis=-1)
    static = np.mean(ranks, axis=-1)
    return {"static": static, "adaptive": adaptive, "weights": weights,
            "matured_ic": matured_ic, "trailing_ic": trailing, "eligible": eligible}


def score_panels(panel, scores):
    result = {"original": panel}
    for name in ("static", "adaptive"):
        result[name] = Panel(
            panel.dates, panel.symbols, panel.stock_mask,
            {**panel.fields, "momentum_risk": scores[name]},
            {**panel.metadata, "scoring_override": name,
             "factor_names": list(FACTORS), "rank_field": "momentum_risk"},
        )
    return result


def registry():
    """Keep every old hypothesis in walk-forward selection; add nine proposals."""
    rules = {name: {"policy": policy, "score": "original"} for name, policy in catalog().items()}
    for risk in (.32, .36, .40):
        name = f"trend_heavy{round(risk * 100)}"
        rules[name] = {"policy": Policy(name, annual_vol_target=risk, rank_buffer=5), "score": "original"}
    proposals = (
        ("cost_weekly32", "original", "weekly", 5, .025, .32),
        ("cost_buffer32", "original", "twice", 10, .05, .32),
        ("cost_weekly_buffer32", "original", "weekly", 10, .05, .32),
        ("static_blend32", "static", "twice", 10, .05, .32),
        ("adaptive_blend32", "adaptive", "twice", 10, .05, .32),
        ("adaptive_weekly32", "adaptive", "weekly", 10, .05, .32),
        ("adaptive_top5_32", "adaptive", "twice", 5, .05, .32),
        ("static_blend36", "static", "twice", 10, .05, .36),
        ("adaptive_blend36", "adaptive", "twice", 10, .05, .36),
    )
    for name, score, schedule, buffer, band, risk in proposals:
        rules[name] = {
            "policy": Policy(name, schedule=schedule, rank_buffer=buffer,
                             turnover_band=band, annual_vol_target=risk),
            "score": score,
        }
    return rules, [row[0] for row in proposals]


def panel_for_policy_changes(panels, rules, changes):
    """Per-day score field follows the policy active at the NEXT open.

    For an open at i with a new policy, that policy must rank using its score at
    i-1. Other features remain unchanged. This only switches the ranking field.
    """
    base = panels["original"]
    field = base.fields["momentum_risk"].copy()
    policy = None
    for i, day in enumerate(base.dates):
        date = str(day.date())
        if date in changes:
            policy = changes[date]
        if policy is not None and policy.name != "cash" and i > 0:
            mode = rules[policy.name]["score"]
            field[i - 1] = panels[mode].fields["momentum_risk"][i - 1]
    return Panel(base.dates, base.symbols, base.stock_mask,
                 {**base.fields, "momentum_risk": field}, base.metadata)


def hypothesis_gate(windows):
    """Fixed-candidate screen distinct from the old walk-forward release gate."""
    return bool(
        all(windows[str(b)][w]["beats_qqq_return_and_drawdown"]
            for b in (25, 50) for w in ("full", "recent_half"))
        and sum(all(windows[str(b)][w]["beats_qqq_return_and_drawdown"] for b in (25, 50))
                for w in ("early", "middle", "recent")) >= 2
    )


def run(output, data_dir=DEFAULT_DATA):
    directory = Path(output)
    directory.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    panel = load_panel(data_dir)
    rules, additions = registry()
    protocol = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "research_stage": "posthoc extension after 19 previous policies and all windows were inspected",
        "data": panel.metadata, "windows": WINDOWS, "folds": FOLDS,
        "cost_bps_each_side": [25, 50], "new_hypotheses": additions,
        "rules": {name: {"policy": asdict(rule["policy"]), "score": rule["score"]}
                  for name, rule in rules.items()},
        "factors": list(FACTORS), "label_horizon": LABEL_HORIZON,
        "ic_lookback": IC_LOOKBACK, "ic_min_periods": IC_MIN_PERIODS,
        "learned_share": LEARNED_SHARE,
        "adaptive_rule": "50% equal weights plus 50% normalized positive trailing matured Rank IC; equal fallback",
        "candidate_gate": "beat QQQ return and DD at 25/50bps in full/recent_half and >=2/3 early/middle/recent windows",
        "walk_forward_selection": "same prior rule: qualified training excess-return minus drawdown; cash if none",
        "walk_forward_gate": "pooled return and DD beat QQQ at 25/50bps, >=2/3 folds beat both at 25bps",
        "independent_oos": False, "execution_authority": "none",
    }
    sources = [Path(__file__), Path(__file__).parent / "daily_factor_lab/core.py",
               Path(__file__).parent / "daily_factor_lab/experiment.py"]
    protocol["source_files_sha256"] = {str(p): sha256(p.read_bytes()).hexdigest() for p in sources}
    write_json(directory / "protocol.json", protocol)
    scores = adaptive_scores(panel)
    panels = score_panels(panel, scores)
    for kind in ("weights", "matured_ic", "trailing_ic"):
        pd.DataFrame(scores[kind], index=panel.dates, columns=FACTORS).to_csv(directory / f"{kind}.csv")
    summary = {"data": panel.metadata, "policies": {}, "benchmarks": {}, "new_hypotheses": additions}
    cache = {}
    for bps in (25, 50):
        summary["benchmarks"][str(bps)] = {}
        for window, (start, end) in WINDOWS.items():
            reference = benchmark(panel, start, end, bps)
            summary["benchmarks"][str(bps)][window] = reference["metrics"]
            for name, rule in rules.items():
                result = simulate(panels[rule["score"]], rule["policy"], start, end, bps)
                stats = comparison(result["metrics"], reference["metrics"])
                stats["rolling42"] = rolling_comparison(result["curve"], reference["curve"], 42)
                summary["policies"].setdefault(name, {}).setdefault(str(bps), {})[window] = stats
                if bps == 25 and window == "full":
                    cache[name] = result
            print(f"completed {bps}bps {window}: {len(rules)} policies", flush=True)
    changes, folds = {}, []
    for train_end, start, end in FOLDS:
        reference = benchmark(panel, WINDOWS["full"][0], train_end, 25)
        training = {}
        for name, rule in rules.items():
            result = simulate(panels[rule["score"]], rule["policy"], WINDOWS["full"][0], train_end, 25)
            training[name] = comparison(result["metrics"], reference["metrics"])
        qualified = [name for name, value in training.items() if value["beats_qqq_return_and_drawdown"]]
        selected = (max(qualified, key=lambda name: training[name]["excess_return_points"]
                        - training[name]["max_drawdown_pct"]) if qualified else "cash")
        rule = rules.get(selected, {"policy": Policy("cash", positions=0), "score": "original"})
        first = next(day for day in panel.dates if day >= pd.Timestamp(start))
        changes[str(first.date())] = rule["policy"]
        test = simulate(panels[rule["score"]], rule["policy"], start, end, 25)
        test_qqq = benchmark(panel, start, end, 25)
        folds.append({"train_end": train_end, "test_start": start, "test_end": end,
                      "selected": selected, "training": training,
                      "test_from_cash": comparison(test["metrics"], test_qqq["metrics"]),
                      "qqq": test_qqq["metrics"]})
    wf_panel = panel_for_policy_changes(panels, rules, changes)
    walk_forward = {"folds": folds, "costs": {}}
    for bps in (25, 50):
        result = simulate(wf_panel, next(iter(changes.values())), FOLDS[0][1], FOLDS[-1][2], bps, changes)
        reference = benchmark(panel, FOLDS[0][1], FOLDS[-1][2], bps)
        stats = comparison(result["metrics"], reference["metrics"])
        stats["qqq"] = reference["metrics"]
        walk_forward["costs"][str(bps)] = stats
        result["curve"].to_csv(directory / f"walk_forward_{bps}bps_curve.csv", index=False)
        pd.DataFrame(result["orders"]).to_csv(directory / f"walk_forward_{bps}bps_orders.csv", index=False)
    walk_forward["gate_passed"] = bool(
        all(walk_forward["costs"][str(b)]["beats_qqq_return_and_drawdown"] for b in (25, 50))
        and sum(f["test_from_cash"]["beats_qqq_return_and_drawdown"] for f in folds) >= 2)
    summary["walk_forward"] = walk_forward
    summary["qualified_fixed_candidates"] = [
        name for name, windows in summary["policies"].items() if hypothesis_gate(windows)]
    summary["elapsed_seconds"] = time.perf_counter() - started
    # Persist every new full-run tape, not only the best-looking version.
    for name in ["trend_heavy32", *additions]:
        result = cache[name]
        result["curve"].to_csv(directory / f"{name}_full_curve.csv", index=False)
        pd.DataFrame(result["orders"]).to_csv(directory / f"{name}_full_orders.csv", index=False)
    rows = []
    for name, costs in summary["policies"].items():
        for bps, windows in costs.items():
            for window, stats in windows.items():
                rows.append({"policy": name, "bps": bps, "window": window,
                             **{k: v for k, v in stats.items() if k != "rolling42"}})
    pd.DataFrame(rows).to_csv(directory / "comparison.csv", index=False)
    # Display prespecified baseline and the two central new hypotheses, not a winner chart.
    plot_result({"QQQ": benchmark(panel, *WINDOWS["full"], 25)["curve"],
                 **{name: cache[name]["curve"] for name in
                    ("trend_heavy32", "cost_buffer32", "adaptive_blend32")}},
                directory / "equity_drawdown.png")
    summary["attribution"] = {name: attribution(panel, cache[name])
                              for name in summary["qualified_fixed_candidates"]}
    write_json(directory / "summary.json", summary)
    write_json(directory / "completed.json", {
        "status": "complete", "data_sha256": panel.metadata["input_sha256"],
        "source_files_sha256": protocol["source_files_sha256"],
        "walk_forward_gate_passed": walk_forward["gate_passed"],
    })
    print(json.dumps({"output": str(directory), "qualified": summary["qualified_fixed_candidates"],
                      "walk_forward_gate": walk_forward["gate_passed"],
                      "elapsed_seconds": summary["elapsed_seconds"]}, ensure_ascii=False))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA))
    args = parser.parse_args()
    run(args.output, args.data_dir)


if __name__ == "__main__":
    main()
