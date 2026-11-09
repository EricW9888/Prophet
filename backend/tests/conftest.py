"""The integration suite must never inherit the running application's state."""

import os
import re
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest


def pytest_sessionstart(session):
    database = os.environ.get("POSTGRES_DB", "")
    if not re.fullmatch(r"prophet_(?:ci|test)(?:_[a-z0-9]+)*", database):
        raise pytest.UsageError(
            "Backend tests require an explicit disposable POSTGRES_DB named "
            "prophet_test_<name> or prophet_ci[_<name>]. See backend/README.md. "
            "The suite can commit and delete synthetic fixtures."
        )
    directory = TemporaryDirectory(prefix="prophet-test-")
    session.config.add_cleanup(directory.cleanup)
    root = Path(directory.name)
    os.environ["RUNTIME_SETTINGS_PATH"] = str(root / "runtime.json")
    os.environ["STORAGE_DIR"] = str(root / "storage")
