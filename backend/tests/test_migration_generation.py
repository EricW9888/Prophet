import ast
import shutil
from pathlib import Path

from alembic import command
from alembic.config import Config


def test_revision_generation_runs_the_configured_formatter(tmp_path: Path) -> None:
    backend = Path(__file__).resolve().parents[1]
    scripts = tmp_path / "alembic"
    shutil.copytree(
        backend / "alembic",
        scripts,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    template = scripts / "script.py.mako"
    with template.open("a", encoding="utf-8") as stream:
        stream.write("\nHOOK_FORMAT_PROBE = {'ready':True}\n")
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(scripts))

    revision = command.revision(
        config, message="Synthetic migration hook check", rev_id="synthetic_hook_check"
    )

    assert revision is not None
    source = Path(revision.path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert {"upgrade", "downgrade"} <= {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    assert 'HOOK_FORMAT_PROBE = {"ready": True}' in source
