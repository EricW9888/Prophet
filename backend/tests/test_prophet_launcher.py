from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "prophet.py"
SPEC = importlib.util.spec_from_file_location("prophet_launcher", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
prophet = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = prophet
SPEC.loader.exec_module(prophet)


def probe(state: prophet.ServiceState, *, name: str = "Backend"):
    port = 8000 if name == "Backend" else 3000
    return prophet.ServiceProbe(
        name=name,
        url=f"http://127.0.0.1:{port}",
        port=port,
        state=state,
    )


def test_probe_service_prefers_health_over_an_open_port():
    result = prophet.probe_service(
        "Backend",
        "http://127.0.0.1:8000/health",
        8000,
        ready_check=lambda _url: True,
        port_check=lambda _port: True,
    )

    assert result.state is prophet.ServiceState.READY


def test_probe_service_distinguishes_unknown_listener_from_down_service():
    occupied = prophet.probe_service(
        "Frontend",
        "http://127.0.0.1:3000",
        3000,
        ready_check=lambda _url: False,
        port_check=lambda _port: True,
    )
    down = prophet.probe_service(
        "Frontend",
        "http://127.0.0.1:3000",
        3000,
        ready_check=lambda _url: False,
        port_check=lambda _port: False,
    )

    assert occupied.state is prophet.ServiceState.PORT_OCCUPIED
    assert down.state is prophet.ServiceState.DOWN


class FakeConnection:
    def __init__(self, response: bytes):
        self.response = response
        self.sent = b""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def sendall(self, data: bytes):
        self.sent += data

    def recv(self, _size: int) -> bytes:
        return self.response


def test_database_probe_requires_a_postgresql_protocol_response(monkeypatch):
    postgres = FakeConnection(b"R")
    monkeypatch.setattr(
        prophet.socket, "create_connection", lambda *_args, **_kwargs: postgres
    )

    assert prophet.postgres_ready(
        "localhost", 5432, user="investos", database="investos"
    )
    assert b"user\x00investos\x00database\x00investos\x00" in postgres.sent

    unrelated = FakeConnection(b"H")
    monkeypatch.setattr(
        prophet.socket, "create_connection", lambda *_args, **_kwargs: unrelated
    )
    assert not prophet.postgres_ready(
        "localhost", 5432, user="investos", database="investos"
    )


def test_ensure_env_file_creates_an_ignored_local_password(tmp_path: Path):
    (tmp_path / ".env.example").write_text(
        "POSTGRES_PASSWORD=replace-with-a-local-password\n",
        encoding="utf-8",
    )

    assert prophet.ensure_env_file(tmp_path) is True

    generated = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "replace-with-a-local-password" not in generated
    assert generated.startswith("POSTGRES_PASSWORD=")
    assert len(generated.strip().split("=", 1)[1]) >= 32
    assert prophet.ensure_env_file(tmp_path) is False


def test_digest_paths_changes_with_input_content(tmp_path: Path, monkeypatch):
    source = tmp_path / "source.txt"
    source.write_text("before", encoding="utf-8")
    monkeypatch.setattr(prophet, "ROOT", tmp_path)

    before = prophet.digest_paths((source,))
    source.write_text("after", encoding="utf-8")

    assert prophet.digest_paths((source,)) != before


def test_unknown_port_listener_blocks_startup_before_process_replacement():
    probes = (
        probe(prophet.ServiceState.PORT_OCCUPIED),
        probe(prophet.ServiceState.DOWN, name="Frontend"),
    )

    with pytest.raises(prophet.StartupError, match="will not stop an unknown process"):
        prophet.ensure_ports_available(probes)


def test_running_stack_is_idempotent_and_skips_bootstrap(monkeypatch):
    probes = (
        probe(prophet.ServiceState.READY),
        probe(prophet.ServiceState.READY, name="Frontend"),
    )
    opened: list[str] = []
    monkeypatch.setattr(prophet, "current_probes", lambda: probes)
    monkeypatch.setattr(
        prophet,
        "bootstrap",
        lambda **_kwargs: pytest.fail("bootstrap should not run for a ready stack"),
    )
    monkeypatch.setattr(prophet.webbrowser, "open", opened.append)

    prophet.start(development=False, open_browser=True)

    assert opened == [prophet.APP_URL]


def test_launcher_has_no_implicit_local_model_prerequisite():
    assert {item.command for item in prophet.PREREQUISITES} == {
        "node",
        "npm",
    }


def test_configured_database_endpoint_validates_port():
    assert prophet.configured_database_endpoint(
        {"POSTGRES_SERVER": "db.internal", "POSTGRES_PORT": "5544"}
    ) == ("db.internal", 5544)

    with pytest.raises(prophet.StartupError, match="must be an integer"):
        prophet.configured_database_endpoint({"POSTGRES_PORT": "not-a-port"})


def test_existing_database_does_not_require_or_start_docker(monkeypatch, capsys):
    values = {"POSTGRES_SERVER": "localhost", "POSTGRES_PORT": "5432"}
    monkeypatch.setattr(
        prophet,
        "probe_database",
        lambda _values: prophet.DatabaseProbe(
            host="localhost", port=5432, state=prophet.DatabaseState.READY
        ),
    )
    monkeypatch.setattr(
        prophet,
        "command_path",
        lambda command: pytest.fail(f"looked up {command} after database was ready"),
    )

    prophet.ensure_database_ready(values)

    assert capsys.readouterr().out == "Using PostgreSQL at localhost:5432.\n"


def test_unavailable_remote_database_is_not_replaced_with_local_compose(monkeypatch):
    values = {"POSTGRES_SERVER": "db.internal", "POSTGRES_PORT": "5432"}
    monkeypatch.setattr(
        prophet,
        "probe_database",
        lambda _values: prophet.DatabaseProbe(
            host="db.internal", port=5432, state=prophet.DatabaseState.DOWN
        ),
    )

    with pytest.raises(prophet.StartupError, match="db.internal:5432 is unavailable"):
        prophet.ensure_database_ready(values)


def test_non_postgres_database_port_is_reported_before_compose(monkeypatch):
    values = {"POSTGRES_SERVER": "localhost", "POSTGRES_PORT": "5432"}
    monkeypatch.setattr(
        prophet,
        "probe_database",
        lambda _values: prophet.DatabaseProbe(
            host="localhost",
            port=5432,
            state=prophet.DatabaseState.PORT_OCCUPIED,
        ),
    )

    with pytest.raises(prophet.StartupError, match="not PostgreSQL"):
        prophet.ensure_database_ready(values)


def test_doctor_requires_a_database_or_docker_before_first_start(
    tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.setattr(prophet, "ROOT", tmp_path)
    monkeypatch.setattr(prophet, "missing_prerequisites", lambda: [])
    monkeypatch.setattr(prophet, "backend_python_path", lambda: tmp_path / "python")
    (tmp_path / "python").touch()
    monkeypatch.setattr(
        prophet,
        "probe_database",
        lambda _values=None: prophet.DatabaseProbe(
            host="localhost", port=5432, state=prophet.DatabaseState.DOWN
        ),
    )
    monkeypatch.setattr(prophet, "command_path", lambda _command: None)
    monkeypatch.setattr(prophet, "current_probes", lambda: ())

    assert prophet.print_doctor() == 1
    output = capsys.readouterr().out
    assert "Configuration: not created yet" in output
    assert "Install and start Docker" in output


def test_cli_defaults_to_stable_start():
    args = prophet.parse_args([])

    assert args.command == "start"
    assert args.dev is False
    assert args.no_open is False
