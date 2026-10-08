"""Frozen, append-only paper replay. Does not connect to any brokerage account."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

import exchange_calendars as xc
import numpy as np
import pandas as pd

from .core import DEFAULT_DATA, Panel, Policy, load_panel, simulate
from .experiment import write_json


def content_hash(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode()
    return sha256(encoded).hexdigest()


def engine_hash():
    return sha256(Path(__file__).with_name("core.py").read_bytes()).hexdigest()


def data_prefix_hash(panel: Panel, end):
    included = panel.dates <= pd.Timestamp(end)
    digest = sha256()
    digest.update(json.dumps(panel.symbols).encode())
    digest.update(panel.stock_mask.astype(np.uint8).tobytes())
    digest.update(panel.dates[included].asi8.tobytes())
    for field in ("open", "high", "low", "close", "volume", "turnover"):
        values = np.asarray(panel.fields[field][included], dtype="<f8").copy()
        values[np.isnan(values)] = np.nan
        digest.update(field.encode())
        digest.update(values.tobytes())
    return digest.hexdigest()


def _next_session(day):
    calendar = xc.get_calendar("XNYS")
    days = calendar.sessions_in_range(pd.Timestamp(day) + pd.Timedelta(days=1),
                                     pd.Timestamp(day) + pd.Timedelta(days=10))
    return str(days[0].date())


def _load_complete(directory, asof, frozen_symbols=None):
    panel = load_panel(directory, asof=asof)
    if frozen_symbols is not None:
        if set(frozen_symbols)-set(panel.symbols):
            raise ValueError("Frozen universe asset disappeared from the cache")
        columns = [panel.symbols.index(symbol) for symbol in frozen_symbols]
        panel = Panel(panel.dates, frozen_symbols, panel.stock_mask[columns],
                      {key:value[:,columns] for key,value in panel.fields.items()},
                      panel.metadata)
    if str(panel.dates[-1].date()) != asof:
        raise ValueError(f"No completed cached session for {asof}; latest is {panel.dates[-1].date()}")
    last = len(panel.dates) - 1
    incomplete = ~np.isfinite(panel.fields["close"][last])
    if incomplete.any():
        raise ValueError("Some frozen-universe assets lack the requested final bar")
    # Historical replay is allowed. A current-day bar cannot be sealed before
    # the actual exchange close, including early-close calendar exceptions.
    calendar = xc.get_calendar("XNYS")
    closing = calendar.session_close(pd.Timestamp(asof))
    if closing > pd.Timestamp.now(tz="UTC"):
        raise ValueError("Cannot seal an uncompleted US session")
    return panel


def initialize(directory, policy: Policy, asof, data_dir=DEFAULT_DATA, bps=25):
    directory = Path(directory)
    if not 0 <= bps < 1000:
        raise ValueError("Invalid cost assumption")
    panel = _load_complete(data_dir, asof)
    manifest = {
        "schema_version": 1, "mode": "local_paper_no_broker",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "asof": asof, "start_session": _next_session(asof),
        "data_dir": str(Path(data_dir).resolve()), "policy": asdict(policy),
        "policy_sha256": content_hash(asdict(policy)), "engine_sha256": engine_hash(),
        "seed_data_sha256": data_prefix_hash(panel, asof),
        "symbols": panel.symbols, "cost_bps_each_side": bps,
        "starting_capital": 10000.,
        "status": "awaiting_first_session",
        "execution_assumption": "adjusted-unit integer shares at next recorded open plus friction",
        "limitation": "paper model, not real-time executable quotes or a brokerage simulated account",
    }
    manifest["manifest_sha256"] = content_hash(manifest)
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / "manifest.json", manifest)
    return manifest


def _read_manifest(directory):
    manifest = json.loads((directory / "manifest.json").read_text())
    original = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    if content_hash(original) != manifest["manifest_sha256"]:
        raise ValueError("Frozen manifest changed")
    if content_hash(manifest["policy"]) != manifest["policy_sha256"]:
        raise ValueError("Frozen policy changed")
    if engine_hash() != manifest["engine_sha256"]:
        raise ValueError("Frozen engine changed; create a new paper version")
    return manifest


def advance(directory, asof, data_dir=None):
    directory = Path(directory)
    manifest = _read_manifest(directory)
    target = directory / f"session_{asof}.json"
    if target.exists():
        raise FileExistsError(f"Session already sealed: {target}")
    if asof < manifest["start_session"]:
        return {"status": "awaiting_first_session", "start_session": manifest["start_session"],
                "orders": [], "equity": manifest["starting_capital"]}
    prior_files = sorted(directory.glob("session_*.json"))
    previous = [json.loads(path.read_text()) for path in prior_files]
    if previous and asof <= previous[-1]["asof"]:
        raise ValueError("New paper session must be later than every sealed session")
    panel = _load_complete(data_dir or manifest["data_dir"], asof, manifest["symbols"])
    if panel.symbols != manifest["symbols"]:
        raise ValueError("Frozen universe changed; initialize a separate version")
    if data_prefix_hash(panel, manifest["asof"]) != manifest["seed_data_sha256"]:
        raise ValueError("Seed data revision; refusing to rewrite paper history")
    for recorded in previous:
        original = {k: v for k, v in recorded.items() if k != "artifact_sha256"}
        if content_hash(original) != recorded["artifact_sha256"]:
            raise ValueError("A sealed paper artifact was modified")
        if data_prefix_hash(panel, recorded["asof"]) != recorded["data_prefix_sha256"]:
            raise ValueError("Data revision changed a sealed paper session")
    result = simulate(panel, Policy(**manifest["policy"]), manifest["start_session"],
                      asof, manifest["cost_bps_each_side"])
    curve = result["curve"].to_dict("records")
    for recorded in previous:
        prefix_curve = [row for row in curve if row["date"] <= recorded["asof"]]
        prefix_orders = [row for row in result["orders"] if row["date"] <= recorded["asof"]]
        if (content_hash(prefix_curve) != recorded["curve_sha256"]
                or content_hash(prefix_orders) != recorded["orders_sha256"]):
            raise ValueError("Replay changed sealed NAV or orders")
    last_asof = previous[-1]["asof"] if previous else manifest["asof"]
    artifact = {
        "schema_version": 1, "asof": asof, "next_session": _next_session(asof),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": manifest["mode"], "manifest_sha256": manifest["manifest_sha256"],
        "data_prefix_sha256": data_prefix_hash(panel, asof),
        "curve_sha256": content_hash(curve), "orders_sha256": content_hash(result["orders"]),
        "paper_metrics": result["metrics"], "equity": curve[-1]["equity"],
        "cash": curve[-1]["cash"], "positions": result["positions"],
        "new_orders": [order for order in result["orders"] if order["date"] > last_asof],
        "curve": curve, "orders": result["orders"],
        "elapsed_sessions": len(curve), "status": "paper_session_sealed",
        "review_due": "weekly" if not previous or len(curve) // 5 >
                      previous[-1]["elapsed_sessions"] // 5 else "daily",
    }
    artifact["artifact_sha256"] = content_hash(artifact)
    write_json(target, artifact)
    return artifact


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--directory", required=True)
    init.add_argument("--policy-file", required=True)
    init.add_argument("--asof", required=True)
    init.add_argument("--data-dir", default=str(DEFAULT_DATA))
    init.add_argument("--bps", type=float, default=25)
    step = commands.add_parser("advance")
    step.add_argument("--directory", required=True)
    step.add_argument("--asof", required=True)
    step.add_argument("--data-dir")
    args = parser.parse_args()
    if args.command == "init":
        result = initialize(args.directory, Policy(**json.loads(Path(args.policy_file).read_text())),
                            args.asof, args.data_dir, args.bps)
    else:
        result = advance(args.directory, args.asof, args.data_dir)
    print(json.dumps({key: result[key] for key in
                      ("status", "asof", "start_session", "equity", "cash", "review_due")
                      if key in result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
