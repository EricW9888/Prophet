from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "audit_python_lock.py"
SPEC = importlib.util.spec_from_file_location("audit_python_lock", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
audit_python_lock = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit_python_lock)


def test_locked_requirements_includes_runtime_and_development_groups(tmp_path: Path):
    lock_path = tmp_path / "poetry.lock"
    lock_path.write_text(
        """
[[package]]
name = "runtime-package"
version = "1.2.3"
groups = ["main"]

[[package]]
name = "development-package"
version = "4.5.6"
groups = ["dev"]

[[package]]
name = "unrelated-package"
version = "7.8.9"
groups = ["docs"]
""".strip(),
        encoding="utf-8",
    )

    assert audit_python_lock.locked_requirements(lock_path) == [
        "development-package==4.5.6",
        "runtime-package==1.2.3",
    ]


def test_locked_requirements_rejects_an_empty_audit_scope(tmp_path: Path):
    lock_path = tmp_path / "poetry.lock"
    lock_path.write_text("package = []\n", encoding="utf-8")

    with pytest.raises(ValueError, match="No packages in groups dev, main"):
        audit_python_lock.locked_requirements(lock_path)


def test_main_propagates_audit_failure(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        audit_python_lock,
        "locked_requirements",
        lambda: ["known-bad-package==1.0"],
    )

    def fake_run(command: list[str], *, check: bool):
        requirement_path = Path(command[command.index("--requirement") + 1])
        assert requirement_path.read_text(encoding="utf-8") == (
            "known-bad-package==1.0\n"
        )
        assert check is False
        return SimpleNamespace(returncode=17)

    monkeypatch.setattr(audit_python_lock.subprocess, "run", fake_run)

    assert audit_python_lock.main() == 17
