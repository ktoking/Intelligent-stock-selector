#!/usr/bin/env python3
"""Refresh read-only OKX research universe and event labels once per NY day."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.okx_event_collector import main as collect_events
from scripts.okx_research_universe import OUTPUT as UNIVERSE_OUTPUT
from scripts.okx_research_universe import discover


def main() -> None:
    # This job deliberately has no account, order, or execution dependency.
    universe = discover()
    UNIVERSE_OUTPUT.write_text(json.dumps(universe, ensure_ascii=False, indent=2))
    collect_events()
    print(json.dumps({
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
        "historical_90d": len(universe["historical_90d"]),
        "forward_observation": len(universe["forward_observation"]),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
