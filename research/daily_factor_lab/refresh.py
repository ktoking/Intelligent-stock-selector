"""Read-only Futunn history refresh into a NEW cache, without OpenD."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time

import exchange_calendars as xc
import pandas as pd

from research.tqqq_intraday import signed_get
from .core import DEFAULT_DATA, load_panel
from .experiment import write_json


def latest_completed_session(now=None):
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    if now.tzinfo is None:
        raise ValueError("Current time must include timezone")
    calendar = xc.get_calendar("XNYS")
    dates = calendar.sessions_in_range(now.date() - pd.Timedelta(days=14), now.date())
    completed = [day for day in dates if calendar.session_close(day) <= now]
    return str(completed[-1].date())


def merge_daily(old, batch, asof):
    new = pd.DataFrame(batch)
    if new.empty or "date" not in new:
        raise ValueError("Empty historical response")
    if len(new) >= 370:
        raise ValueError("Possible truncated historical response")
    new = new.loc[new.date.astype(int) <= int(asof.replace("-", ""))].copy()
    old = old.loc[old.date.astype(int) <= int(asof.replace("-", ""))].copy()
    if new.empty or int(new.date.max()) != int(asof.replace("-", "")):
        raise ValueError("Response has no requested completed-session bar")
    keys = ("open", "high", "low", "close", "volume", "turnover")
    overlap = old.set_index("date").index.intersection(new.set_index("date").index)
    if len(overlap):
        before = old.set_index("date").loc[overlap, list(keys)]
        after = new.set_index("date").loc[overlap, list(keys)]
        # A changed adjustment basis cannot be spliced into an older cache.
        changed = (before-after).abs() > (before.abs()*1e-9 + 1e-7)
        if changed.any().any():
            raise ValueError("Historical values revised; full symbol refresh and paper-version review required")
    merged = pd.concat([old, new]).drop_duplicates("date", keep="last").sort_values("date")
    return merged.reset_index(drop=True)


def refresh(source, output, asof, symbols=None, fetch=signed_get, sleep=time.sleep):
    source, output = Path(source), Path(output)
    if asof > latest_completed_session():
        raise ValueError("Requested daily bar is not yet complete")
    universe = json.loads((source / "universe.json").read_text())
    selected = sorted(symbols or universe["symbols"])
    if not selected or set(selected)-set(universe["symbols"]):
        raise ValueError("Symbols must come from the frozen source universe")
    output.mkdir(parents=True, exist_ok=False)
    failures = []
    successes = []
    for number, symbol in enumerate(selected, 1):
        if not re.fullmatch(r"US\.[A-Za-z0-9]+", symbol):
            raise ValueError("Unexpected US symbol")
        name = symbol[3:] + ".pkl"
        old = pd.read_pickle(source/name)
        last = pd.to_datetime(str(int(old.date.max())), format="%Y%m%d")
        start = last - pd.Timedelta(days=7)
        if (pd.Timestamp(asof)-start).days > 350:
            raise ValueError("Cache too old for bounded incremental refresh")
        batch = None
        error_type = ""
        for attempt in range(4):
            try:
                response = fetch("/api/v1.0/quote/"+symbol+"/history-kline",
                                 dict(start=str(start.date()), end=asof, ktype=2, num=370, autype=1))
                if response.get("ret_code") != 0:
                    raise ValueError("Futunn returned unsuccessful ret_code")
                batch = response["data"]["kline_list"]
                break
            except Exception as exc:
                error_type = type(exc).__name__
                if attempt < 3:
                    sleep(min(2**attempt * 2, 8))
        if batch is None:
            failures.append({"symbol":symbol, "reason":error_type})
            continue
        try:
            merged = merge_daily(old, batch, asof)
            merged.to_pickle(output/name)
            successes.append({"symbol":symbol, "rows":len(merged)})
        except ValueError as exc:
            failures.append({"symbol":symbol, "reason":str(exc)})
        if number % 20 == 0:
            print(f"history refresh {number}/{len(selected)}, failed={len(failures)}",flush=True)
        sleep(.8)
    types = json.loads((source/"security_types.json").read_text())
    write_json(output/"universe.json", {**universe, "symbols":selected,
               "fetched_at_utc":datetime.now(timezone.utc).isoformat()})
    write_json(output/"security_types.json", {s:types[s] for s in selected})
    report={"status":"complete" if not failures else "incomplete", "asof":asof,
            "source_cache":str(source.resolve()), "success":successes, "failures":failures}
    write_json(output/"refresh_report.json",report)
    if failures:
        raise RuntimeError(f"Incomplete refresh: {len(failures)} failures; old cache preserved")
    if "US.QQQ" in selected:
        panel=load_panel(output, asof=asof)
        write_json(output/"completed.json",{"status":"complete","input_sha256":panel.metadata["input_sha256"]})
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source",default=str(DEFAULT_DATA))
    parser.add_argument("--output",required=True)
    parser.add_argument("--asof",help="Defaults to the last completed XNYS session")
    parser.add_argument("--symbols",nargs="+",help="Optional quote probe subset, not a full paper cache")
    args=parser.parse_args()
    if not os.environ.get("FUTUNN_APP_KEY") or not os.environ.get("FUTUNN_PRIVATE_KEY_FILE"):
        raise SystemExit("Set FUTUNN_APP_KEY and FUTUNN_PRIVATE_KEY_FILE in the environment")
    report=refresh(args.source,args.output,args.asof or latest_completed_session(),args.symbols)
    print(json.dumps({"status":report["status"],"asof":report["asof"],"symbols":len(report["success"])}))


if __name__=="__main__":
    main()
