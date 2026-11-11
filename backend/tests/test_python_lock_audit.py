import importlib.util
from pathlib import Path
from types import SimpleNamespace


def test_lock_audit_checks_every_version_without_hiding_a_failed_batch(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts" / "audit_python_lock.py"
    spec = importlib.util.spec_from_file_location("lock_audit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    requirements = ["array-helper==1.0", "array-helper==2.0", "other==3.0"]
    monkeypatch.setattr(module, "locked_requirements", lambda: requirements)
    observed = []

    def audit(command, **_kwargs):
        requirement_file = Path(command[command.index("--requirement") + 1])
        observed.append(requirement_file.read_text().splitlines())
        return SimpleNamespace(returncode=1 if len(observed) == 1 else 0)

    monkeypatch.setattr(module.subprocess, "run", audit)
    assert module.main() == 1
    assert len(observed) == 2
    assert sorted(line for batch in observed for line in batch) == requirements
    for batch in observed:
        names = [line.partition("==")[0] for line in batch]
        assert len(names) == len(set(names))
