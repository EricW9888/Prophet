#!/usr/bin/env python3
"""Audit every Poetry-locked application dependency with pip-audit."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import tomllib
from collections import defaultdict
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
    batches: list[list[str]] = [[]]
    occurrences: dict[str, int] = defaultdict(int)
    for requirement in requirements:
        name = requirement.partition("==")[0].lower()
        index = occurrences[name]
        if index == len(batches):
            batches.append([])
        batches[index].append(requirement)
        occurrences[name] += 1
    status = 0
    with tempfile.TemporaryDirectory(prefix="prophet-locked-") as directory:
        for index, batch in enumerate(batches):
            # pip-audit rejects two versions of one name in a requirements file.
            # Audit every locked variant, including other supported interpreters.
            requirements_path = Path(directory) / f"requirements-{index}.txt"
            requirements_path.write_text("\n".join(batch) + "\n", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip_audit",
                    "--requirement",
                    str(requirements_path),
                    "--no-deps",
                    "--disable-pip",
                    "--progress-spinner",
                    "off",
                ],
                check=False,
            )
            if result.returncode:
                status = result.returncode
    return status


if __name__ == "__main__":
    raise SystemExit(main())
