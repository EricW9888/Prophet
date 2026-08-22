from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_policy_module():
    module_path = ROOT / "scripts" / "repository_policy_check.py"
    spec = importlib.util.spec_from_file_location(
        "repository_policy_check", module_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_tracked_tree_excludes_private_runtime_state():
    policy = _load_policy_module()
    repository_paths = set(policy.list_repository_paths())

    assert "README.md" in repository_paths
    assert "scripts/repository_policy_check.py" in repository_paths
    assert "data/runtime_settings.json" not in repository_paths


def test_path_policy_fails_closed_for_private_and_generated_files():
    policy = _load_policy_module()

    assert policy.path_violation(".env.example") is None
    assert policy.path_violation(".env") == "private_runtime_file"
    assert policy.path_violation("data/runtime_settings.json") == "private_runtime_root"
    assert policy.path_violation("backend/data/cache.json") == "private_runtime_root"
    assert policy.path_violation(".ai/notes.md") == "private_runtime_root"
    assert policy.path_violation("frontend/AGENTS.md") == "local_agent_rules"
    assert policy.path_violation("CLAUDE.md") == "local_agent_rules"
    assert policy.path_violation("frontend/.next/server.js") == "generated_artifact"
    assert policy.path_violation("tmp/session.json") == "private_runtime_root"


def test_machine_specific_home_paths_are_reported_without_echoing_content():
    policy = _load_policy_module()
    personal_path = "/" + "Users" + "/sample/Prophet"

    findings = policy.text_findings("example.txt", personal_path)

    assert [(finding.line, finding.rule) for finding in findings] == [
        (1, "machine_specific_home_path")
    ]
