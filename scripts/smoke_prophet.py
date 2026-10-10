#!/usr/bin/env python3
"""Verify the real launcher and frontend proxy in a disposable CI checkout."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from urllib.request import urlopen

import prophet


def main() -> int:
    if os.environ.get("CI") != "true":
        raise RuntimeError("Run this smoke check only in a disposable CI checkout.")
    if any(
        probe.state is not prophet.ServiceState.DOWN
        for probe in prophet.current_probes()
    ):
        raise RuntimeError("The smoke check requires unused frontend/backend ports.")
    child = prophet.child_process(
        "Smoke launcher",
        [sys.executable, str(prophet.ROOT / "scripts" / "prophet.py"), "--no-open"],
        cwd=prophet.ROOT,
        log_path=prophet.PRIVATE_RUNTIME_DIR / "smoke-launcher.log",
    )
    try:
        prophet.wait_for_services([child], timeout_seconds=600)
        with urlopen(
            f"{prophet.APP_URL}/api_proxy/dashboard/summary", timeout=30
        ) as response:
            payload = json.load(response)
        if not isinstance(payload.get("holdings_count"), int) or not payload.get(
            "as_of"
        ):
            raise RuntimeError("The frontend proxy did not return a dashboard summary.")
        print("Production launcher, backend health, frontend, and API proxy passed.")
    finally:
        if os.name != "nt" and child.process.poll() is None:
            os.killpg(child.process.pid, signal.SIGINT)
            try:
                child.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass
        prophet.terminate_child(child)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if all(
            probe.state is prophet.ServiceState.DOWN
            for probe in prophet.current_probes()
        ):
            print("Launcher-owned processes stopped; ports are available again.")
            return 0
        time.sleep(0.5)
    raise RuntimeError("Launcher-owned frontend/backend processes remained running.")


if __name__ == "__main__":
    raise SystemExit(main())
