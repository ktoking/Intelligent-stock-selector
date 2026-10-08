"""Read-only reproducibility and independent order-ledger audit of a lab run.

Writes a new audit artifact only when explicitly given --output. No network,
broker, OpenD, or LLM calls. Passing this audit is not a profitability gate.
Run from the repository root with: python -m scripts.verify_daily_factor_run.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

from research.daily_factor_lab.ai_review import validate_review
from research.daily_factor_lab.core import DEFAULT_DATA, Policy, benchmark, load_panel, simulate
from research.daily_factor_lab.experiment import write_json
from research.daily_factor_lab.paper import content_hash, data_prefix_hash, engine_hash


def read_json(path):
    return json.loads(Path(path).read_text())


def close(left, right, label):
    if left is None or right is None:
        if left != right:
            raise AssertionError(label)
    elif not np.isclose(left, right, atol=1e-7, rtol=1e-10):
        raise AssertionError(f"{label}: {left} != {right}")


def compare_metrics(actual, expected):
    for key, value in actual.items():
        close(value, expected[key], key)


def audit_ledger(panel, curve, orders, bps):
    """Reconstruct all daily cash/shares/NAV without using simulator accounting."""
    cash, shares, cost = 10000., np.zeros(len(panel.symbols), dtype=int), 0.
    symbols = {symbol: j for j, symbol in enumerate(panel.symbols)}
    daily = {}
    for order in orders:
        daily.setdefault(order["date"], []).append(order)
    for row in curve.to_dict("records"):
        i = panel.dates.get_loc(pd.Timestamp(row["date"]))
        for order in daily.pop(row["date"], []):
            j = symbols[order["symbol"]]
            side = order["side"]
            if side not in ("BUY", "SELL"):
                raise AssertionError("Invalid order side")
            sign = 1 if side == "BUY" else -1
            qty = order["qty"]
            if qty <= 0 or int(qty) != qty:
                raise AssertionError("Nonpositive or noninteger share quantity")
            raw = panel.fields["open"][i, j]
            friction = qty * raw * bps / 10000.
            close(raw, order["raw_price"], "raw price")
            close(friction, order["cost"], "order friction")
            close(raw * (1 + sign * bps / 10000.), order["price"], "fill price")
            cash -= sign * qty * raw + friction
            shares[j] += sign * int(qty)
            cost += friction
            if cash < -1e-7 or (shares < 0).any():
                raise AssertionError("Borrowing or negative share balance")
        held = shares > 0
        marks = panel.fields["close"][i, held]
        if not np.isfinite(marks).all():
            raise AssertionError("Missing held-asset mark")
        nav = cash + shares[held] @ marks
        close(nav, row["equity"], "daily NAV")
        close(cash, row["cash"], "daily cash")
        if "holdings" in row:
            close(held.sum(), row["holdings"], "holding count")
    if daily:
        raise AssertionError("Order outside the curve interval")
    values = np.r_[10000., curve.equity.to_numpy()]
    return {
        "orders": len(orders), "sessions": len(curve), "cost_dollars": cost,
        "final_equity": float(values[-1]), "final_cash": cash,
        "return_pct": float((values[-1] / 10000. - 1) * 100),
        "max_drawdown_pct": float((1 - values / np.maximum.accumulate(values)).max() * 100),
    }


def verify(run_dir, data_dir=DEFAULT_DATA):
    directory = Path(run_dir)
    protocol = read_json(directory / "protocol.json")
    completed = read_json(directory / "completed.json")
    summary = read_json(directory / "summary.json")
    if completed["status"] != "complete":
        raise AssertionError("Incomplete run")
    package = Path(__file__).resolve().parents[1] / "research" / "daily_factor_lab"
    digest = sha256()
    source_files = {}
    for file in sorted(package.glob("*.py")):
        payload = file.read_bytes()
        digest.update(file.name.encode())
        digest.update(payload)
        source_files[file.name] = sha256(payload).hexdigest()
    if digest.hexdigest() != protocol["source_sha256"] or digest.hexdigest() != completed["source_sha256"]:
        raise AssertionError("Source differs from frozen run")
    panel = load_panel(data_dir)
    for expected in (protocol["data"]["input_sha256"], summary["data"]["input_sha256"],
                     completed["data_sha256"]):
        if panel.metadata["input_sha256"] != expected:
            raise AssertionError("Data differs from frozen run")
    candidate = summary["retrospective_candidate"]
    saved_policy = read_json(directory / "candidate_policy.json")
    if saved_policy != protocol["policies"][candidate]:
        raise AssertionError("Saved candidate differs from experiment policy")
    policy = Policy(**saved_policy)
    window_checks = []
    for name, (start, end) in protocol["windows"].items():
        result = simulate(panel, policy, start, end, 25)
        compare_metrics(result["metrics"], summary["policies"][candidate][name])
        compare_metrics(benchmark(panel, start, end, 25)["metrics"], summary["benchmarks"][name])
        window_checks.append(name)
        if name == "full":
            saved_curve = pd.read_csv(directory / "candidate_full_curve.csv")
            saved_orders = pd.read_csv(directory / "candidate_full_orders.csv").to_dict("records")
            pd.testing.assert_frame_equal(saved_curve, result["curve"], check_exact=False, atol=1e-7)
            if len(saved_orders) != len(result["orders"]):
                raise AssertionError("Candidate order count changed")
            for saved, actual in zip(saved_orders, result["orders"]):
                for key, value in actual.items():
                    if isinstance(value, str):
                        if saved[key] != value:
                            raise AssertionError(f"Order {key} changed")
                    else:
                        close(saved[key], value, f"Order {key}")
            ledger = audit_ledger(panel, saved_curve, saved_orders, 25)
            for key in ("return_pct", "max_drawdown_pct", "cost_dollars"):
                close(ledger[key], result["metrics"][key], f"Ledger {key}")
    stress_checks = []
    for label, results in summary["candidate_stress"].items():
        bps, lag = (25, 2) if label == "extra_signal_delay" else (int(label), 1)
        for window, expected in results.items():
            result = simulate(panel, policy, *protocol["windows"][window], bps, signal_lag=lag)
            compare_metrics(result["metrics"], expected)
            stress_checks.append(f"{label}/{window}")
    changes = {}
    folds = summary["walk_forward"]["folds"]
    for fold in folds:
        if fold["train_end"] >= fold["test_start"]:
            raise AssertionError("Training overlaps validation interval")
        selected = fold["selected"]
        chosen = (Policy("cash", positions=0) if selected == "cash"
                  else Policy(**protocol["policies"][selected]))
        first_day = next(day for day in panel.dates if str(day.date()) >= fold["test_start"])
        changes[str(first_day.date())] = chosen
    for bps in (10, 25, 50):
        result = simulate(panel, next(iter(changes.values())), folds[0]["test_start"],
                          folds[-1]["test_end"], bps, changes)
        compare_metrics(result["metrics"], summary["walk_forward"]["costs"][str(bps)])
        saved = pd.read_csv(directory / f"walk_forward_{bps}bps_curve.csv")
        pd.testing.assert_frame_equal(saved, result["curve"], check_exact=False, atol=1e-7)
    dependencies = {name: version(name) for name in
                    ("numpy", "pandas", "exchange_calendars", "requests", "matplotlib", "cryptography")}
    return {
        "status": "reproducibility_and_accounting_passed",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "run_directory": str(directory.resolve()), "candidate": candidate,
        "source_sha256": digest.hexdigest(), "source_files_sha256": source_files,
        "data_sha256": panel.metadata["input_sha256"], "python": sys.version,
        "dependencies": dependencies, "candidate_windows_verified": window_checks,
        "candidate_stress_verified": stress_checks, "walk_forward_costs_verified": [10, 25, 50],
        "candidate_independent_ledger": ledger,
        "artifact_sha256": {p.name: sha256(p.read_bytes()).hexdigest()
                            for p in sorted(directory.iterdir()) if p.is_file()},
        "robustness_gate_passed": summary["walk_forward"]["gate_passed"],
        "execution_authority": "none",
        "limitation": "Reproducibility is not independent market data, an untouched holdout, or broker-fill validation.",
    }


def verify_paper(directory, data_dir):
    directory = Path(directory)
    manifest = read_json(directory / "manifest.json")
    unsigned = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    if content_hash(unsigned) != manifest["manifest_sha256"] or engine_hash() != manifest["engine_sha256"]:
        raise AssertionError("Paper manifest or engine changed")
    from research.daily_factor_lab.paper import _load_complete
    previous = None
    sessions = []
    for path in sorted(directory.glob("session_*.json")):
        artifact = read_json(path)
        unsigned = {k: v for k, v in artifact.items() if k != "artifact_sha256"}
        if content_hash(unsigned) != artifact["artifact_sha256"]:
            raise AssertionError("Paper artifact modified")
        panel = _load_complete(data_dir, artifact["asof"], manifest["symbols"])
        if data_prefix_hash(panel, artifact["asof"]) != artifact["data_prefix_sha256"]:
            raise AssertionError("Paper data revised")
        if previous:
            for field in ("curve", "orders"):
                prefix = [row for row in artifact[field] if row["date"] <= previous["asof"]]
                if prefix != previous[field]:
                    raise AssertionError(f"Paper {field} prefix changed")
        ledger = audit_ledger(panel, pd.DataFrame(artifact["curve"]), artifact["orders"],
                              manifest["cost_bps_each_side"])
        close(ledger["final_equity"], artifact["equity"], "Paper final equity")
        sessions.append({"asof": artifact["asof"], "ledger": ledger})
        previous = artifact
    return {"mode": manifest["mode"], "sessions_verified": sessions,
            "start_session": manifest["start_session"], "frozen_symbols": len(manifest["symbols"]),
            "historical_demo_not_forward_evidence": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA))
    parser.add_argument("--output", required=True)
    parser.add_argument("--paper-demo")
    parser.add_argument("--ai-review")
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists():
        raise FileExistsError(output)
    result = verify(args.run, args.data_dir)
    if args.paper_demo:
        result["paper_demo"] = verify_paper(args.paper_demo, args.data_dir)
    if args.ai_review:
        artifact = read_json(args.ai_review)
        raw = artifact["result"]
        if sha256(raw["raw_response"].encode()).hexdigest() != raw["response_sha256"]:
            raise AssertionError("LLM raw response changed")
        review = json.loads(json.loads(raw["raw_response"])["message"]["content"])
        if validate_review(review, artifact["packet"]) != raw["review"]:
            raise AssertionError("LLM accepted review differs from raw response")
        result["ai_review"] = {
            "artifact": str(Path(args.ai_review).resolve()), "status": raw["status"],
            "action": raw["review"]["action"], "evidence_count": len(artifact["packet"]["evidence"]),
            "review_quality_verified": False, "adopted": False,
            "warning": "Schema and hashes pass, but rationale under-discusses failed walk-forward and cost sensitivity.",
        }
    write_json(output, result)
    print(json.dumps({"output": str(output), "status": result["status"],
                      "robustness_gate_passed": result["robustness_gate_passed"]}))


if __name__ == "__main__":
    main()
