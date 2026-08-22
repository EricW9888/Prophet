#!/usr/bin/env python3
"""Audit every Poetry-locked application dependency with pip-audit."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "backend" / "poetry.lock"
AUDITED_GROUPS = frozenset({"main", "dev"})


def locked_requirements(
    lock_path: Path = LOCK_PATH,
    groups: frozenset[str] = AUDITED_GROUPS,
) -> list[str]:
    payload = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    requirements = {
        f"{package['name']}=={package['version']}"
        for package in payload.get("package", [])
        if groups.intersection(package.get("groups", []))
    }
    if not requirements:
        audited = ", ".join(sorted(groups))
        raise ValueError(f"No packages in groups {audited} were found in {lock_path}")
    return sorted(requirements, key=str.casefold)


def main() -> int:
    requirements = locked_requirements()
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="prophet-locked-",
        suffix=".txt",
    ) as handle:
        handle.write("\n".join(requirements) + "\n")
        handle.flush()
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip_audit",
                "--requirement",
                handle.name,
                "--no-deps",
                "--disable-pip",
                "--progress-spinner",
                "off",
            ],
            check=False,
        )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
