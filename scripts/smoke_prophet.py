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

    def launch(command: str):
        return prophet.child_process(
            "Smoke launcher",
            [
                sys.executable,
                str(prophet.ROOT / "scripts" / "prophet.py"),
                command,
                "--no-open",
            ],
            cwd=prophet.ROOT,
            log_path=prophet.PRIVATE_RUNTIME_DIR / "smoke-launcher.log",
        )

    def wait_for_identity(launcher):
        prophet.wait_for_services([launcher], timeout_seconds=600)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            frontend, front_owner = prophet.runtime_identity(prophet.FRONTEND_BUILD_URL)
            backend, back_owner = prophet.runtime_identity(prophet.BACKEND_HEALTH_URL)
            if (
                frontend
                and backend
                and front_owner
                and front_owner == back_owner
                and prophet.CONTROL_PATH.exists()
            ):
                return frontend, backend, front_owner
            time.sleep(0.25)
        raise RuntimeError("The launched stack did not report its build and ownership.")

    child = launch("start")
    replacement = None
    marker = prophet.FRONTEND_DIR / "public" / "launcher-smoke-build-input.txt"
    try:
        frontend, backend, instance = wait_for_identity(child)
        if (frontend, backend) != (
            prophet.frontend_build_digest(),
            prophet.backend_build_digest(),
        ):
            raise RuntimeError("The running builds do not match the checkout.")
        with urlopen(
            f"{prophet.APP_URL}/api_proxy/dashboard/summary", timeout=30
        ) as response:
            payload = json.load(response)
        if not isinstance(payload.get("holdings_count"), int) or not payload.get(
            "as_of"
        ):
            raise RuntimeError("The frontend proxy did not return a dashboard summary.")
        with marker.open("x", encoding="utf-8") as handle:
            handle.write("changed frontend build input\n")
        if (
            prophet.runtime_identity(prophet.FRONTEND_BUILD_URL)[0]
            == prophet.frontend_build_digest()
        ):
            raise RuntimeError(
                "The running frontend reported an unbuilt source change."
            )
        replacement = launch("restart")
        if child.process.wait(timeout=45) != 0:
            raise RuntimeError(
                "The original launcher did not finish its owned shutdown."
            )
        new_frontend, new_backend, new_instance = wait_for_identity(replacement)
        if (
            new_frontend != prophet.frontend_build_digest()
            or new_frontend == frontend
            or new_backend != backend
            or new_instance == instance
        ):
            raise RuntimeError(
                "Restart did not load the changed build in a new owned stack."
            )
        prophet.request_owned_stop()
        if replacement.process.wait(timeout=15) != 0:
            raise RuntimeError("The replacement launcher did not stop successfully.")
        print(
            "Production build identity, API proxy, restart after a source edit, and owned stop passed."
        )
    finally:
        marker.unlink(missing_ok=True)
        for launcher in (replacement, child):
            if launcher is None:
                continue
            if os.name != "nt" and launcher.process.poll() is None:
                os.killpg(launcher.process.pid, signal.SIGINT)
                try:
                    launcher.process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    pass
            prophet.terminate_child(launcher)
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
