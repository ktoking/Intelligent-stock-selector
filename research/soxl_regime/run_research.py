from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from .backtest import StrategyConfig, buy_hold, simulate
from .data_loader import download_soxl
from .features import add_features
from .regime import RegimeConfig, classify


def _slice(frame: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    dates = frame.index.strftime("%Y-%m-%d")
    return frame[(dates >= start) & (dates <= end)]


def run(output: Path, refresh: bool = False) -> dict:
    cache = output / "cache"
    frames, audits = download_soxl(cache, refresh=refresh)
    hourly = add_features(frames["hourly"])
    daily = add_features(frames["daily"], slope_bars=3, boll_std=1.5)

    regime_grid = [
        RegimeConfig(enter_spread=spread, range_width=width, persistence=persistence)
        for spread in (0.005, 0.0075, 0.010)
        for width in (0.10, 0.14, 0.18)
        for persistence in (1, 2)
    ]
    strategy_grid = [
        StrategyConfig(trend_position=trend, range_position=range_pos, panic_position=panic,
                       range_rsi=rsi, range_tp=tp, panic_drop=drop)
        for trend in (0.25, 0.35, 0.40, 0.50)
        for range_pos in (0.20, 0.25)
        for panic in (0.15, 0.20)
        for rsi in (35, 40, 45)
        for tp in (0.015, 0.025)
        for drop in (0.04, 0.06)
    ]
    rows: list[dict] = []
    regime_cache = [(cfg, classify(hourly, cfg)) for cfg in regime_grid]
    # First rank regime definitions with a stable, central strategy to avoid a joint-grid lottery.
    for rcfg, states in regime_cache:
        _, trades, result = simulate(hourly, states, daily, StrategyConfig(), cost_bps=10)
        counts = states.value_counts(normalize=True).to_dict()
        transition_share = counts.get("TRANSITION", 0) + counts.get("HIGH_VOL", 0)
        score = result["return_pct"] - 1.25 * result["max_drawdown_pct"] - 8 * max(0, transition_share - 0.55)
        rows.append({"kind": "regime", "score": score, "regime": asdict(rcfg), "strategy": asdict(StrategyConfig()), **result, "regime_share": counts})
    best_regime_row = max(rows, key=lambda row: row["score"])
    best_regime = RegimeConfig(**best_regime_row["regime"])
    states = classify(hourly, best_regime)

    strategy_rows: list[dict] = []
    for cfg in strategy_grid:
        _, trades, result = simulate(hourly, states, daily, cfg, cost_bps=10)
        pf = result["profit_factor"] or 0
        concentration_penalty = max(0, (result["top3_contribution_pct"] or 0) - 80) / 10
        score = result["return_pct"] - 1.1 * result["max_drawdown_pct"] + min(pf, 3) - concentration_penalty
        strategy_rows.append({"kind": "strategy", "score": score, "regime": asdict(best_regime), "strategy": asdict(cfg), **result})
    eligible = [row for row in strategy_rows if row["trades"] >= 30 and row["max_drawdown_pct"] <= 15 and (row["profit_factor"] or 0) >= 1.3]
    selected_row = max(eligible or strategy_rows, key=lambda row: row["score"])
    selected = StrategyConfig(**selected_row["strategy"])
    equity, trades, full = simulate(hourly, states, daily, selected, cost_bps=10)

    segments = [
        ("S1", "2024-09-25", "2025-03-24"), ("S2", "2025-03-25", "2025-09-24"),
        ("S3", "2025-09-25", "2026-03-24"), ("S4", "2026-03-25", "2026-09-18"),
    ]
    segment_rows = []
    for name, start, end in segments:
        part = _slice(hourly, start, end)
        _, _, result = simulate(part, states.reindex(part.index), daily, selected, cost_bps=10)
        segment_rows.append({"segment": name, "start": start, "end": end, **result})

    walk_windows = [
        ("WF1", "2024-09-25", "2025-06-24", "2025-06-25", "2025-09-24"),
        ("WF2", "2024-12-25", "2025-09-24", "2025-09-25", "2025-12-24"),
        ("WF3", "2025-03-25", "2025-12-24", "2025-12-25", "2026-03-24"),
        ("WF4", "2025-06-25", "2026-03-24", "2026-03-25", "2026-06-24"),
        ("WF5", "2025-09-25", "2026-06-24", "2026-06-25", "2026-09-18"),
    ]
    wf_regimes = [
        RegimeConfig(enter_spread=spread, range_width=width, persistence=persistence)
        for spread in (0.005, 0.010) for width in (0.10, 0.18) for persistence in (1, 2)
    ]
    wf_strategies = [
        StrategyConfig(trend_position=trend, range_position=0.25, panic_position=panic, range_rsi=rsi, range_tp=0.015, panic_drop=0.06)
        for trend in (0.25, 0.35) for panic in (0.0, 0.15) for rsi in (35, 45)
    ]
    wf_rows = []
    for name, train_start, train_end, valid_start, valid_end in walk_windows:
        train = _slice(hourly, train_start, train_end)
        validation = _slice(hourly, valid_start, valid_end)
        choices = []
        for rcfg in wf_regimes:
            candidate_states = classify(hourly, rcfg)
            for scfg in wf_strategies:
                _, _, train_result = simulate(train, candidate_states.reindex(train.index), daily, scfg, cost_bps=10)
                train_score = train_result["return_pct"] - 1.25 * train_result["max_drawdown_pct"] + min(train_result["profit_factor"] or 0, 3)
                choices.append((train_score, rcfg, scfg, train_result))
        _, chosen_regime, chosen_strategy, train_result = max(choices, key=lambda item: item[0])
        validation_states = classify(hourly, chosen_regime).reindex(validation.index)
        _, _, validation_result = simulate(validation, validation_states, daily, chosen_strategy, cost_bps=10)
        wf_rows.append({
            "window": name, "train_start": train_start, "train_end": train_end,
            "start": valid_start, "end": valid_end,
            "selected_regime": asdict(chosen_regime), "selected_strategy": asdict(chosen_strategy),
            "train_return_pct": train_result["return_pct"], "train_max_drawdown_pct": train_result["max_drawdown_pct"],
            **validation_result,
        })

    costs = {}
    for side_bps in (5, 10, 20):
        _, _, result = simulate(hourly, states, daily, selected, cost_bps=side_bps)
        costs[str(side_bps)] = result

    # The importable artifact is deliberately more conservative than the return-maximizing proxy.
    # DOWN_PANIC is disabled because its full-window contribution is negative.
    candidate_config = StrategyConfig(
        trend_position=0.25, range_position=0.25, panic_position=0.0,
        range_rsi=45, range_tp=0.015, panic_drop=0.06,
    )
    _, _, candidate = simulate(hourly, states, daily, candidate_config, cost_bps=10)
    candidate_costs = {}
    for side_bps in (5, 10, 20):
        _, _, candidate_result = simulate(hourly, states, daily, candidate_config, cost_bps=side_bps)
        candidate_costs[str(side_bps)] = candidate_result

    # Recent exact 5-minute execution diagnostic using a 30-minute regime built only from completed 5m bars.
    five = add_features(frames["five_minute_recent"])
    thirty_raw = frames["five_minute_recent"].resample("30min", origin="start_day", offset="30min", label="right", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    thirty = add_features(thirty_raw)
    thirty_states = classify(thirty, best_regime)
    mapped = thirty_states.reindex(five.index, method="ffill").fillna("TRANSITION")
    recent_equity, recent_trades, recent = simulate(five, mapped, daily, selected, cost_bps=10)

    output.mkdir(parents=True, exist_ok=True)
    equity.to_csv(output / "soxl_regime_backtest.csv")
    trades.to_csv(output / "soxl_regime_trades.csv", index=False)
    pd.DataFrame(segment_rows).to_csv(output / "soxl_regime_segments.csv", index=False)
    pd.DataFrame(rows + strategy_rows).to_csv(output / "soxl_regime_parameters.csv", index=False)
    pd.DataFrame(wf_rows).to_csv(output / "soxl_regime_walk_forward.csv", index=False)
    recent_equity.to_csv(output / "soxl_regime_recent_5m_equity.csv")
    recent_trades.to_csv(output / "soxl_regime_recent_5m_trades.csv", index=False)

    current_regime = str(states.iloc[-1])
    profitable_segments = sum(float(row["return_pct"]) > 0 for row in segment_rows)
    wf_profitable = sum(float(row["return_pct"]) > 0 for row in wf_rows)
    passed = (
        full["return_pct"] > 0 and full["max_drawdown_pct"] < 15 and (full["profit_factor"] or 0) >= 1.3
        and full["trades"] >= 30 and profitable_segments >= 3 and wf_profitable >= 3 and costs["20"]["return_pct"] > 0
    )
    result = {
        "authoritative_data_range": audits,
        "limitations": [
            "Alpaca credentials/cached bars were unavailable.",
            "Alpha Vantage historical monthly 5-minute endpoint rejected the configured non-premium key.",
            "Two-year execution results therefore use Yahoo 60-minute proxy bars from 2024-09-25, not the requested two-year 5-minute Alpaca/IEX bars.",
            "Yahoo recent 5-minute validation covers only 2026-07-27 through 2026-09-18.",
        ],
        "regime_config": asdict(best_regime), "strategy_config": asdict(selected),
        "current_regime_at_2026_09_18": current_regime,
        "regime_share": states.value_counts(normalize=True).round(6).to_dict(),
        "regime_switches": int(states.ne(states.shift()).sum() - 1),
        "full_proxy": full, "recent_5m": recent, "segments": segment_rows,
        "walk_forward": wf_rows, "cost_stress_bps_per_side": costs,
        "generated_candidate": candidate,
        "generated_candidate_cost_stress_bps_per_side": candidate_costs,
        "benchmark_buy_hold": buy_hold(hourly, 10),
        "provided_futu_v4_baseline": {"return_pct": 10.41, "max_drawdown_pct": 6.9, "sharpe": 3.707, "sortino": 6.361, "orders": 93, "range": "approximately 2026-06-20 to 2026-09-19"},
        "formal_proxy_acceptance_passed": passed,
        "final_status": "CONDITIONAL PASS" if passed else "FAIL",
        "reason": "The proxy acceptance gate passed, but the mandatory two-year 5-minute Alpaca/Futu confirmation remains unproven." if passed else "The proxy itself did not pass the requested acceptance gate.",
        "rejected_strategies": ["5m momentum breakout (prior exploration: unstable)", "5m UP pullback (prior exploration: no stable edge)", "TRANSITION trading (disabled)"],
    }
    (output / "research_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("outputs/soxl_regime_switch_v1"))
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    result = run(args.output, refresh=args.refresh)
    print(json.dumps({key: result[key] for key in ["current_regime_at_2026_09_18", "regime_config", "strategy_config", "full_proxy", "recent_5m", "final_status"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
