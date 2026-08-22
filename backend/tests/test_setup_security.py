from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from investos.api.routes.setup import (
    _is_loopback_client,
    reset_development_state,
    router,
)
from investos.config import PROJECT_ROOT, Settings, settings
from investos.schemas.setup import DevelopmentResetRequest


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "127.12.34.56", "::1", "::ffff:127.0.0.1"],
)
def test_development_reset_accepts_only_loopback_addresses(host):
    request = SimpleNamespace(client=SimpleNamespace(host=host))

    assert _is_loopback_client(request) is True


@pytest.mark.parametrize("host", ["192.0.2.10", "example.com", "0.0.0.0"])
def test_development_reset_rejects_non_loopback_addresses(host):
    request = SimpleNamespace(client=SimpleNamespace(host=host))

    assert _is_loopback_client(request) is False


def test_destructive_reset_is_disabled_by_default():
    isolated = Settings(
        _env_file=None,
        POSTGRES_PASSWORD="unit-test-placeholder",
    )

    assert isolated.DEV_RESET_ENABLED is False
    assert isolated.DEVELOPMENT_RESET_AVAILABLE is False
    assert Settings.model_fields["POSTGRES_PASSWORD"].is_required()


def test_development_reset_registration_matches_startup_gate():
    route_is_registered = any(route.path == "/setup/reset" for route in router.routes)

    assert route_is_registered is settings.DEVELOPMENT_RESET_AVAILABLE


def test_settings_load_the_repository_root_env_file():
    assert Settings.model_config["env_file"] == PROJECT_ROOT / ".env"


@pytest.mark.asyncio
async def test_reset_route_is_hidden_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "DEV_RESET_ENABLED", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))

    with pytest.raises(HTTPException) as exc_info:
        await reset_development_state(
            DevelopmentResetRequest(confirmation_text="RESET INVESTOS"),
            request,
            None,
        )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_reset_route_rejects_remote_clients_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "DEV_RESET_ENABLED", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    request = SimpleNamespace(client=SimpleNamespace(host="192.0.2.10"))

    with pytest.raises(HTTPException) as exc_info:
        await reset_development_state(
            DevelopmentResetRequest(confirmation_text="RESET INVESTOS"),
            request,
            None,
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_reset_route_rejects_non_development_environments(monkeypatch):
    monkeypatch.setattr(settings, "DEV_RESET_ENABLED", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))

    with pytest.raises(HTTPException) as exc_info:
        await reset_development_state(
            DevelopmentResetRequest(confirmation_text="RESET INVESTOS"),
            request,
            None,
        )

    assert exc_info.value.status_code == 404
