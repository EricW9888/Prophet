#!/usr/bin/env python3
"""Cross-platform local launcher for Prophet."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT / "backend"
FRONTEND_DIR = ROOT / "frontend"
PRIVATE_RUNTIME_DIR = ROOT / ".prophet-local" / "runtime"
CACHE_PATH = PRIVATE_RUNTIME_DIR / "launcher-cache.json"
BACKEND_LOG = PRIVATE_RUNTIME_DIR / "backend.log"
FRONTEND_LOG = PRIVATE_RUNTIME_DIR / "frontend.log"
APP_URL = "http://127.0.0.1:3000"
BACKEND_HEALTH_URL = "http://127.0.0.1:8000/health"
BACKEND_PORT = 8000
FRONTEND_PORT = 3000


class StartupError(RuntimeError):
    """An actionable local-startup failure."""


class ServiceState(str, Enum):
    READY = "ready"
    DOWN = "down"
    PORT_OCCUPIED = "port_occupied"


class DatabaseState(str, Enum):
    READY = "ready"
    DOWN = "down"
    PORT_OCCUPIED = "port_occupied"


@dataclass(frozen=True)
class Prerequisite:
    command: str
    install_hint: str


@dataclass(frozen=True)
class ServiceProbe:
    name: str
    url: str
    port: int
    state: ServiceState


@dataclass(frozen=True)
class DatabaseProbe:
    host: str
    port: int
    state: DatabaseState


@dataclass
class ChildProcess:
    name: str
    process: subprocess.Popen[bytes]
    log_path: Path
    log_handle: object


PREREQUISITES = (
    Prerequisite("node", "Install Node.js 24 LTS."),
    Prerequisite("npm", "Install npm with Node.js 24 LTS."),
)
DOCKER_PREREQUISITE = Prerequisite(
    "docker", "Install and start Docker, or configure an existing PostgreSQL server."
)
POETRY_PREREQUISITE = Prerequisite(
    "poetry", "Install Poetry 2.3.2 with pipx to prepare the backend environment."
)


def command_path(command: str) -> str | None:
    return shutil.which(command)


def http_ready(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 400
    except (OSError, urllib.error.URLError):
        return False


def tcp_open(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def postgres_ready(
    host: str,
    port: int,
    *,
    user: str,
    database: str,
    timeout: float = 0.6,
) -> bool:
    parameters = (
        b"user\x00"
        + user.encode("utf-8")
        + b"\x00database\x00"
        + database.encode("utf-8")
        + b"\x00\x00"
    )
    startup = struct.pack("!II", len(parameters) + 8, 196608) + parameters
    try:
        with socket.create_connection((host, port), timeout=timeout) as connection:
            connection.sendall(startup)
            message_type = connection.recv(1)
    except OSError:
        return False
    return message_type in {b"R", b"E", b"S", b"K", b"Z"}


def port_open(port: int, timeout: float = 0.3) -> bool:
    return tcp_open("127.0.0.1", port, timeout)


def probe_service(
    name: str,
    url: str,
    port: int,
    *,
    ready_check: Callable[[str], bool] = http_ready,
    port_check: Callable[[int], bool] = port_open,
) -> ServiceProbe:
    if ready_check(url):
        state = ServiceState.READY
    elif port_check(port):
        state = ServiceState.PORT_OCCUPIED
    else:
        state = ServiceState.DOWN
    return ServiceProbe(name=name, url=url, port=port, state=state)


def current_probes() -> tuple[ServiceProbe, ServiceProbe]:
    return (
        probe_service("Backend", BACKEND_HEALTH_URL, BACKEND_PORT),
        probe_service("Frontend", APP_URL, FRONTEND_PORT),
    )


def require_supported_python() -> None:
    if not (3, 11) <= sys.version_info[:2] <= (3, 14):
        raise StartupError(
            "Prophet requires Python 3.11 through 3.14. "
            f"This launcher is using {sys.version_info.major}.{sys.version_info.minor}."
        )


def missing_prerequisites() -> list[Prerequisite]:
    return [item for item in PREREQUISITES if command_path(item.command) is None]


def require_prerequisites() -> None:
    missing = missing_prerequisites()
    if not missing:
        return
    details = "\n".join(f"  - {item.command}: {item.install_hint}" for item in missing)
    raise StartupError(f"Missing required tools:\n{details}")


def backend_python_path() -> Path:
    return (
        BACKEND_DIR
        / ".venv"
        / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )


def backend_command() -> list[str]:
    # Next.js owns the trusted proxy boundary. FastAPI is loopback-only and must
    # use the socket peer rather than reinterpret forwarded client headers.
    return [
        str(backend_python_path()),
        "-m",
        "uvicorn",
        "investos.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(BACKEND_PORT),
        "--no-proxy-headers",
    ]


def run_command(
    command: Sequence[str],
    *,
    cwd: Path = ROOT,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(command),
        cwd=cwd,
        check=False,
        text=True,
        capture_output=capture,
    )
    if result.returncode != 0:
        rendered = " ".join(command)
        detail = (result.stderr or result.stdout or "").strip() if capture else ""
        suffix = f"\n{detail}" if detail else ""
        raise StartupError(f"Command failed: {rendered}{suffix}")
    return result


def ensure_env_file(root: Path = ROOT) -> bool:
    env_path = root / ".env"
    password_line = "POSTGRES_PASSWORD=replace-with-a-local-password"
    if env_path.exists():
        template = env_path.read_text(encoding="utf-8")
        if password_line not in template:
            if not env_values(env_path).get("POSTGRES_PASSWORD"):
                raise StartupError(
                    "The private .env must define a non-empty POSTGRES_PASSWORD."
                )
            return False
    else:
        example_path = root / ".env.example"
        if not example_path.exists():
            raise StartupError(".env.example is missing; the checkout is incomplete.")
        template = example_path.read_text(encoding="utf-8")
        if password_line not in template:
            raise StartupError(
                ".env.example does not contain the expected local password marker."
            )

    generated_password = secrets.token_urlsafe(32)
    env_path.write_text(
        template.replace(password_line, f"POSTGRES_PASSWORD={generated_password}"),
        encoding="utf-8",
    )
    try:
        env_path.chmod(0o600)
    except OSError:
        pass
    return True


def env_values(env_path: Path = ROOT / ".env") -> dict[str, str]:
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def frontend_process_environment(
    values: dict[str, str] | None = None,
    base_environment: dict[str, str] | None = None,
) -> dict[str, str]:
    current = values if values is not None else env_values()
    environment = dict(os.environ if base_environment is None else base_environment)
    configured_identity = current.get("PROPHET_REMOTE_ACCESS_USER", "").strip()
    if configured_identity and not environment.get("PROPHET_REMOTE_ACCESS_USER"):
        environment["PROPHET_REMOTE_ACCESS_USER"] = configured_identity
    return environment


def configured_database_endpoint(
    values: dict[str, str] | None = None,
) -> tuple[str, int]:
    current = values if values is not None else env_values()
    host = current.get("POSTGRES_SERVER", "localhost")
    raw_port = current.get("POSTGRES_PORT", "5432")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise StartupError("POSTGRES_PORT in .env must be an integer.") from exc
    if not 1 <= port <= 65535:
        raise StartupError("POSTGRES_PORT in .env must be between 1 and 65535.")
    return host, port


def configured_database_available(values: dict[str, str] | None = None) -> bool:
    current = values if values is not None else env_values()
    host, port = configured_database_endpoint(current)
    return postgres_ready(
        host,
        port,
        user=current.get("POSTGRES_USER", "investos"),
        database=current.get("POSTGRES_DB", "investos"),
    )


def probe_database(values: dict[str, str] | None = None) -> DatabaseProbe:
    current = values if values is not None else env_values()
    host, port = configured_database_endpoint(current)
    if configured_database_available(current):
        state = DatabaseState.READY
    elif tcp_open(host, port, timeout=0.6):
        state = DatabaseState.PORT_OCCUPIED
    else:
        state = DatabaseState.DOWN
    return DatabaseProbe(host=host, port=port, state=state)


def ensure_database_ready(values: dict[str, str] | None = None) -> None:
    current = values if values is not None else env_values()
    probe = probe_database(current)
    if probe.state is DatabaseState.READY:
        print(f"Using PostgreSQL at {probe.host}:{probe.port}.")
        return
    if probe.state is DatabaseState.PORT_OCCUPIED:
        raise StartupError(
            f"Port {probe.port} at {probe.host} is occupied by a service that is not "
            "PostgreSQL. Stop that service or change POSTGRES_PORT in the private .env."
        )
    if probe.host not in {"127.0.0.1", "localhost", "::1"}:
        raise StartupError(
            f"Configured PostgreSQL at {probe.host}:{probe.port} is unavailable."
        )
    docker = command_path("docker")
    if not docker:
        raise StartupError(DOCKER_PREREQUISITE.install_hint)
    ensure_docker_ready(docker)
    print("Preparing PostgreSQL with Docker Compose...")
    run_command([docker, "compose", "up", "-d", "db"])
    wait_for_database(docker)


def digest_paths(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(item for item in path.rglob("*") if item.is_file())
        elif path.is_file():
            files.append(path)
    for path in sorted(files):
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def load_cache(path: Path = CACHE_PATH) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return {str(key): str(value) for key, value in data.items()}


def save_cache(cache: dict[str, str], path: Path = CACHE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def backend_dependency_digest() -> str:
    return digest_paths((BACKEND_DIR / "pyproject.toml", BACKEND_DIR / "poetry.lock"))


def frontend_dependency_digest() -> str:
    return digest_paths(
        (FRONTEND_DIR / "package.json", FRONTEND_DIR / "package-lock.json")
    )


def frontend_build_digest() -> str:
    return digest_paths(
        (
            FRONTEND_DIR / "src",
            FRONTEND_DIR / "public",
            FRONTEND_DIR / "package.json",
            FRONTEND_DIR / "package-lock.json",
            FRONTEND_DIR / "next.config.ts",
            FRONTEND_DIR / "postcss.config.mjs",
            FRONTEND_DIR / "tsconfig.json",
        )
    )


def ensure_docker_ready(docker: str) -> None:
    result = subprocess.run(
        [docker, "info"],
        cwd=ROOT,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise StartupError(
            "Docker is installed but its engine is unavailable. Start Docker, then retry."
        )


def wait_for_database(docker: str, timeout_seconds: int = 45) -> None:
    values = env_values()
    user = values.get("POSTGRES_USER", "investos")
    database = values.get("POSTGRES_DB", "investos")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = subprocess.run(
            [
                docker,
                "compose",
                "exec",
                "-T",
                "db",
                "pg_isready",
                "-U",
                user,
                "-d",
                database,
            ],
            cwd=ROOT,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return
        time.sleep(1)
    raise StartupError(
        "PostgreSQL did not become ready. Run `docker compose logs db` for details."
    )


def bootstrap(*, development: bool) -> None:
    require_supported_python()
    require_prerequisites()
    npm = command_path("npm")
    assert npm

    created_env = ensure_env_file()
    if created_env:
        print("Created private .env with a generated local database password.")

    ensure_database_ready(env_values())

    cache = load_cache()
    backend_digest = backend_dependency_digest()
    backend_python = backend_python_path()
    backend_cache_matches = cache.get("backend_dependencies") == backend_digest
    if backend_python.exists() and not cache.get("backend_dependencies"):
        print("Using the existing backend environment.")
        cache["backend_dependencies"] = backend_digest
        backend_cache_matches = True
    if not backend_cache_matches or not backend_python.exists():
        poetry = command_path("poetry")
        if not poetry:
            raise StartupError(POETRY_PREREQUISITE.install_hint)
        print("Installing backend dependencies...")
        run_command(
            [poetry, "config", "virtualenvs.in-project", "true", "--local"],
            cwd=BACKEND_DIR,
        )
        run_command([poetry, "install", "--with", "dev"], cwd=BACKEND_DIR)
        cache["backend_dependencies"] = backend_digest

    frontend_digest = frontend_dependency_digest()
    if (
        cache.get("frontend_dependencies") != frontend_digest
        or not (FRONTEND_DIR / "node_modules").is_dir()
    ):
        print("Installing frontend dependencies...")
        run_command([npm, "ci"], cwd=FRONTEND_DIR)
        cache["frontend_dependencies"] = frontend_digest

    print("Applying database migrations...")
    run_command(
        [str(backend_python), "-m", "alembic", "upgrade", "head"], cwd=BACKEND_DIR
    )

    if not development:
        build_digest = frontend_build_digest()
        if (
            cache.get("frontend_build") != build_digest
            or not (FRONTEND_DIR / ".next" / "BUILD_ID").exists()
        ):
            print("Building the frontend...")
            run_command([npm, "run", "build"], cwd=FRONTEND_DIR)
            cache["frontend_build"] = build_digest

    save_cache(cache)


def ensure_ports_available(probes: Sequence[ServiceProbe]) -> None:
    occupied = [probe for probe in probes if probe.state is ServiceState.PORT_OCCUPIED]
    if not occupied:
        return
    details = "\n".join(
        f"  - {probe.name}: port {probe.port} is in use but {probe.url} is not healthy."
        for probe in occupied
    )
    raise StartupError(
        "Prophet will not stop an unknown process. Free these ports, then retry:\n"
        f"{details}"
    )


def child_process(
    name: str,
    command: Sequence[str],
    *,
    cwd: Path,
    log_path: Path,
    environment: dict[str, str] | None = None,
) -> ChildProcess:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("ab", buffering=0)
    kwargs: dict[str, object] = {
        "cwd": cwd,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
    }
    if environment is not None:
        kwargs["env"] = environment
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(list(command), **kwargs)
    return ChildProcess(
        name=name,
        process=process,
        log_path=log_path,
        log_handle=log_handle,
    )


def terminate_child(child: ChildProcess) -> None:
    if child.process.poll() is not None:
        child.log_handle.close()
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(child.process.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        try:
            os.killpg(child.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        child.process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            try:
                os.killpg(child.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        child.process.wait(timeout=3)
    child.log_handle.close()


def log_tail(path: Path, lines: int = 20) -> str:
    try:
        return "\n".join(
            path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
        )
    except OSError:
        return "Log is unavailable."


def wait_for_services(
    children: Sequence[ChildProcess], timeout_seconds: int = 75
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        failed = next(
            (child for child in children if child.process.poll() is not None), None
        )
        if failed:
            raise StartupError(
                f"{failed.name} exited during startup.\n\n{log_tail(failed.log_path)}"
            )
        if all(probe.state is ServiceState.READY for probe in current_probes()):
            return
        time.sleep(1)
    states = ", ".join(
        f"{probe.name.lower()}={probe.state.value}" for probe in current_probes()
    )
    raise StartupError(
        f"Prophet did not become ready in time ({states}). "
        f"Logs: {BACKEND_LOG} and {FRONTEND_LOG}"
    )


def supervise(children: Sequence[ChildProcess]) -> None:
    print("Prophet is running. Keep this window open; press Ctrl+C to stop it.")
    try:
        while True:
            failed = next(
                (child for child in children if child.process.poll() is not None), None
            )
            if failed:
                raise StartupError(
                    f"{failed.name} stopped unexpectedly.\n\n{log_tail(failed.log_path)}"
                )
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping Prophet...")


def start(*, development: bool, open_browser: bool) -> None:
    initial = current_probes()
    ensure_ports_available(initial)
    if all(probe.state is ServiceState.READY for probe in initial):
        print(f"Prophet is already running at {APP_URL}")
        if open_browser:
            webbrowser.open(APP_URL)
        return

    bootstrap(development=development)
    probes = current_probes()
    ensure_ports_available(probes)

    npm = command_path("npm")
    assert npm
    children: list[ChildProcess] = []
    try:
        if probes[0].state is ServiceState.DOWN:
            print("Starting backend...")
            children.append(
                child_process(
                    "Backend",
                    backend_command(),
                    cwd=BACKEND_DIR,
                    log_path=BACKEND_LOG,
                )
            )
        if probes[1].state is ServiceState.DOWN:
            print("Starting frontend...")
            frontend_command = [
                npm,
                "run",
                "dev" if development else "start",
                "--",
                "--hostname",
                "127.0.0.1",
                "--port",
                str(FRONTEND_PORT),
            ]
            children.append(
                child_process(
                    "Frontend",
                    frontend_command,
                    cwd=FRONTEND_DIR,
                    log_path=FRONTEND_LOG,
                    environment=frontend_process_environment(),
                )
            )
        wait_for_services(children)
        print(f"Prophet is ready at {APP_URL}")
        if open_browser:
            webbrowser.open(APP_URL)
        supervise(children)
    finally:
        for child in reversed(children):
            terminate_child(child)


def print_status() -> int:
    probes = current_probes()
    for probe in probes:
        label = {
            ServiceState.READY: "ready",
            ServiceState.DOWN: "not running",
            ServiceState.PORT_OCCUPIED: "blocked by another listener",
        }[probe.state]
        print(f"{probe.name}: {label} ({probe.url})")
    if all(probe.state is ServiceState.READY for probe in probes):
        print("Prophet is ready.")
        return 0
    print("Run the launcher without the `status` command to prepare and start Prophet.")
    return 1


def print_doctor() -> int:
    problems: list[str] = []
    if not (3, 11) <= sys.version_info[:2] <= (3, 14):
        problems.append(
            f"Python {sys.version_info.major}.{sys.version_info.minor} is unsupported; use 3.11-3.14."
        )
    problems.extend(
        f"{item.command} is missing. {item.install_hint}"
        for item in missing_prerequisites()
    )
    if not backend_python_path().exists() and command_path("poetry") is None:
        problems.append(
            "The backend environment is missing. " + POETRY_PREREQUISITE.install_hint
        )
    env_exists = (ROOT / ".env").exists()
    if not env_exists:
        print("Configuration: not created yet (the start command will create it).")
    else:
        print("Configuration: private .env present.")
    values = env_values() if env_exists else {}
    database_probe = probe_database(values)
    if database_probe.state is DatabaseState.READY:
        print(f"PostgreSQL: available at {database_probe.host}:{database_probe.port}.")
    elif database_probe.state is DatabaseState.PORT_OCCUPIED:
        problems.append(
            f"Port {database_probe.port} at {database_probe.host} is occupied by a "
            "service that is not PostgreSQL."
        )
    elif database_probe.host not in {"127.0.0.1", "localhost", "::1"}:
        problems.append(
            f"Configured PostgreSQL at {database_probe.host}:{database_probe.port} "
            "is unavailable."
        )
    elif command_path("docker") is None:
        problems.append(DOCKER_PREREQUISITE.install_hint)
    else:
        print(
            f"PostgreSQL: unavailable at {database_probe.host}:{database_probe.port}; "
            "the start command will try Docker Compose."
        )
    for probe in current_probes():
        print(f"{probe.name}: {probe.state.value}")
    if problems:
        print("\nNeeds attention:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nPrerequisites are available.")
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare, start, and diagnose the local Prophet application."
    )
    parser.add_argument(
        "command",
        choices=("start", "status", "doctor"),
        nargs="?",
        default="start",
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Use the Next.js development server instead of a production build.",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open Prophet in the default browser after startup.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "status":
            return print_status()
        if args.command == "doctor":
            return print_doctor()
        start(development=args.dev, open_browser=not args.no_open)
        return 0
    except StartupError as exc:
        print(f"\nProphet could not start:\n{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
