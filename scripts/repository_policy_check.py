#!/usr/bin/env python3
"""Check the tracked Prophet tree for private or machine-local artifacts.

Credential detection belongs to Gitleaks and code security belongs to Bandit.
This narrow check enforces the repository boundary that those tools do not:
runtime state, generated output, and machine-specific paths must not become
tracked source files.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]

PRIVATE_PATH_PREFIXES = {
    (".ai",),
    (".claude",),
    (".prophet-local",),
    ("backups",),
    ("backend", "backups"),
    ("backend", "data"),
    ("backend", "scratch"),
    ("backend", "tmp"),
    ("data",),
    ("scratch",),
    ("tmp",),
}
GENERATED_PARTS = {
    ".next",
    ".next-dev",
    ".venv",
    "__pycache__",
    "htmlcov",
    "node_modules",
}
PRIVATE_NAMES = {
    ".env",
    "runtime_settings.json",
    "runtime_settings.json.secrets",
}
LOCAL_AGENT_RULE_NAMES = {"AGENTS.md", "CLAUDE.md"}
PRIVATE_SUFFIXES = {
    ".db",
    ".key",
    ".log",
    ".p12",
    ".pem",
    ".pfx",
    ".prophet-setup",
    ".secret",
    ".secrets",
    ".sqlite",
    ".sqlite3",
}
BINARY_SUFFIXES = {
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".png",
    ".ttf",
    ".woff",
    ".woff2",
}
PERSONAL_PATH_RE = re.compile(
    r"(?:/" + r"Users/[^/\s]+/|/" + r"home/[^/\s]+/|[A-Za-z]:\\" + r"Users\\[^\\\s]+\\)"
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str


def list_repository_paths(root: Path = ROOT) -> list[str]:
    """Return tracked and non-ignored files that are eligible for commit."""
    try:
        output = subprocess.check_output(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ]
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("Unable to enumerate tracked repository files.") from exc
    return sorted(raw.decode("utf-8") for raw in output.split(b"\0") if raw)


def path_violation(path: str) -> str | None:
    """Return the repository policy violated by a tracked path, if any."""
    normalized = PurePosixPath(path).as_posix()
    pure = PurePosixPath(normalized)
    if normalized == ".env.example":
        return None
    if any(pure.parts[: len(prefix)] == prefix for prefix in PRIVATE_PATH_PREFIXES):
        return "private_runtime_root"
    if any(part in GENERATED_PARTS for part in pure.parts):
        return "generated_artifact"
    if pure.name in LOCAL_AGENT_RULE_NAMES:
        return "local_agent_rules"
    if pure.name in PRIVATE_NAMES or pure.name.startswith(".env."):
        return "private_runtime_file"
    if pure.suffix.lower() in PRIVATE_SUFFIXES:
        return "private_runtime_file"
    return None


def text_findings(path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if PERSONAL_PATH_RE.search(line):
            findings.append(
                Finding(path=path, line=line_number, rule="machine_specific_home_path")
            )
    return findings


def check_repository(root: Path = ROOT) -> tuple[int, list[Finding]]:
    repository_paths = list_repository_paths(root)
    findings: list[Finding] = []
    for relative_path in repository_paths:
        violation = path_violation(relative_path)
        if violation:
            findings.append(Finding(path=relative_path, line=1, rule=violation))
            continue

        path = root / relative_path
        if path.suffix.lower() in BINARY_SUFFIXES or not path.is_file():
            continue
        try:
            if path.stat().st_size > 2_000_000:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        findings.extend(text_findings(relative_path, text))
    return len(repository_paths), findings


def main() -> int:
    repository_file_count, findings = check_repository()
    print("# Prophet repository policy check")
    print(f"repository_files={repository_file_count}")
    print(f"policy_findings={len(findings)}")
    for finding in findings:
        print(f"{finding.path}:{finding.line}: {finding.rule}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
