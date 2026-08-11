#!/usr/bin/env python3
"""Single launchd-owned supervisor for every OKX demo-trading process."""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, time as clock_time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.okx_intraday_agent import kill_switch, monitor_control

LOG = logging.getLogger("okx-runtime")
NY = ZoneInfo("America/New_York")
V5_REPORT_PATH = ROOT / "data" / "okx_gap_strategy_v5_backtest.json"


def v5_refresh_due(now: datetime, report_path: Path = V5_REPORT_PATH) -> bool:
    """Return true once a completed weekday session is absent from the V5 report."""
    local = now.astimezone(NY)
    if local.weekday() >= 5 or local.time() < clock_time(16, 15):
        return False
    try:
        end = str(json.loads(report_path.read_text())["effective_sessions"]["end"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        end = ""
    return end != local.date().isoformat()


class Runtime:
    def __init__(self) -> None:
        self.running = True
        self.children: dict[str, subprocess.Popen] = {}
        self.recap: subprocess.Popen | None = None
        self.research_refresh: subprocess.Popen | None = None
        self.research_refresh_started_at: float | None = None
        self.last_recap_check = 0.0
        self.last_health_check = 0.0
        self.last_research_refresh_day: str | None = None
        self.v5_refresh: subprocess.Popen | None = None
        self.v5_refresh_started_at: float | None = None
        self.last_v5_refresh_day: str | None = None
        self.child_started_at: dict[str, float] = {}

    def command(self, script: str, *args: str) -> list[str]:
        return [sys.executable, str(ROOT / "scripts" / script), *args]

    def start_child(self, name: str, command: list[str]) -> None:
        LOG.info("starting %s", name)
        self.children[name] = subprocess.Popen(command, cwd=ROOT, env=os.environ.copy())
        self.child_started_at[name] = time.time()

    @staticmethod
    def heartbeat_age(path: Path, field: str = "updated_at") -> float:
        try:
            value = json.loads(path.read_text()).get(field)
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return max(0.0, time.time() - stamp.timestamp())
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return float("inf")

    def restart_stale_child(self, name: str, reason: str) -> None:
        child = self.children.get(name)
        if child is None or child.poll() is not None:
            return
        LOG.warning("restarting stale %s: %s", name, reason)
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()

    def check_heartbeats(self) -> None:
        now = time.time()
        if now - self.last_health_check < 30:
            return
        self.last_health_check = now
        checks = {
            "microstructure": (ROOT / "data" / "okx_microstructure.json", "last_message_at", 150),
            "micro_model": (ROOT / "data" / "okx_microstructure_model_state.json", "updated_at", 180),
            "micro_executor": (ROOT / "data" / "okx_micro_execution_state.json", "updated_at", 60),
        }
        for name, (path, field, maximum_age) in checks.items():
            child = self.children.get(name)
            # Allow authentication, schema migration and the first heartbeat.
            if child is None or child.poll() is not None or now - self.child_started_at.get(name, now) < 180:
                continue
            age = self.heartbeat_age(path, field)
            if age > maximum_age:
                self.restart_stale_child(name, f"{field} age {age:.0f}s > {maximum_age}s")

    def ensure_children(self) -> None:
        if not monitor_control()["enabled"]:
            self.stop_trading_children()
            return
        commands = {
            "microstructure": self.command("okx_microstructure_collector.py"),
            "shadow_labeler": self.command("okx_shadow_labeler.py"),
            "return_shadow": self.command("okx_return_shadow.py"),
            "micro_report": self.command("okx_microstructure_report.py"),
            "gap_shadow": self.command("okx_gap_shadow.py"),
        }
        kill_switch_enabled = bool(kill_switch().get("enabled"))
        if not kill_switch_enabled:
            commands.update({
                "scanner": self.command("okx_intraday_agent.py", "--loop"),
                "executor": self.command("okx_candidate_ws.py"),
                "micro_model": self.command("okx_microstructure_model.py"),
                "micro_executor": self.command("okx_microstructure_executor.py"),
            })
            # This is a separately opted-in Demo experiment, but it is still an
            # order path and must obey the same emergency kill switch.
            if os.getenv("OKX_GAP_EXPLORATORY_DEMO") == "1":
                commands["gap_demo_executor"] = self.command("okx_gap_demo_executor.py")
        # A research kill switch must stop the rejected strategy itself, not
        # merely rely on each execution adapter declining orders.  Public data
        # collectors and explicitly shadow-only evaluators remain alive.
        for name in set(self.children) - set(commands):
            child = self.children[name]
            if child.poll() is None:
                LOG.info("research kill switch active; stopping %s", name)
                child.terminate()
        for name, command in commands.items():
            child = self.children.get(name)
            if child is None or child.poll() is not None:
                if child is not None:
                    LOG.warning("%s exited with code %s; restarting", name, child.returncode)
                self.start_child(name, command)

    def stop_trading_children(self) -> None:
        for name, child in list(self.children.items()):
            if child.poll() is not None:
                continue
            LOG.info("monitoring paused; stopping %s", name)
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()

    def check_recap(self) -> None:
        now = time.monotonic()
        if now - self.last_recap_check < 600:
            return
        self.last_recap_check = now
        if self.recap is not None and self.recap.poll() is None:
            LOG.warning("previous close recap is still running; skipped this check")
            return
        self.recap = subprocess.Popen(self.command("okx_close_recap.py"), cwd=ROOT, env=os.environ.copy())

    def check_research_refresh(self) -> None:
        """Refresh public research metadata at most once per New York day."""
        today = datetime.now(NY).date().isoformat()
        if self.last_research_refresh_day == today:
            if (self.research_refresh is not None and self.research_refresh.poll() is None
                    and self.research_refresh_started_at is not None
                    and time.time() - self.research_refresh_started_at > 180):
                LOG.warning("research refresh exceeded 180s after writing its cache; terminating it")
                self.research_refresh.terminate()
            return
        if self.research_refresh is not None and self.research_refresh.poll() is None:
            return
        self.research_refresh = subprocess.Popen(
            self.command("okx_research_refresh.py"), cwd=ROOT, env=os.environ.copy()
        )
        self.research_refresh_started_at = time.time()
        self.last_research_refresh_day = today

    def check_v5_refresh(self) -> None:
        """Refresh the rolling V5 choices after the completed US cash session."""
        local = datetime.now(NY)
        today = local.date().isoformat()
        if self.v5_refresh is not None and self.v5_refresh.poll() is None:
            if self.v5_refresh_started_at is not None and time.time() - self.v5_refresh_started_at > 300:
                LOG.warning("V5 post-close refresh exceeded 300s; terminating it")
                self.v5_refresh.terminate()
            return
        if self.last_v5_refresh_day == today or not v5_refresh_due(local):
            return
        LOG.info("starting V5 post-close rolling refresh for %s", today)
        self.v5_refresh = subprocess.Popen(
            self.command("okx_gap_strategy_v5.py"), cwd=ROOT, env=os.environ.copy()
        )
        self.v5_refresh_started_at = time.time()
        self.last_v5_refresh_day = today

    def stop(self) -> None:
        self.running = False
        processes = [child for child in self.children.values() if child.poll() is None]
        if self.recap is not None and self.recap.poll() is None:
            processes.append(self.recap)
        if self.research_refresh is not None and self.research_refresh.poll() is None:
            processes.append(self.research_refresh)
        if self.v5_refresh is not None and self.v5_refresh.poll() is None:
            processes.append(self.v5_refresh)
        for child in processes:
            child.terminate()
        deadline = time.monotonic() + 10
        for child in processes:
            try:
                child.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.kill()

    def run(self) -> None:
        while self.running:
            self.ensure_children()
            self.check_heartbeats()
            self.check_research_refresh()
            self.check_v5_refresh()
            self.check_recap()
            time.sleep(5)


def main() -> None:
    runtime = Runtime()
    signal.signal(signal.SIGTERM, lambda *_: runtime.stop())
    signal.signal(signal.SIGINT, lambda *_: runtime.stop())
    try:
        runtime.run()
    finally:
        runtime.stop()


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("OKX_INTRADAY_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    main()
