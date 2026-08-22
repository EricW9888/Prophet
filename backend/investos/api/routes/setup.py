from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from investos.config import settings
from investos.core.request_security import is_loopback_host
from investos.db import get_session
from investos.schemas.automation import AutomationJobStatus
from investos.schemas.setup import (
    DevelopmentResetRequest,
    DevelopmentResetResponse,
    SetupStatusResponse,
)
from investos.services.setup import SetupService

router = APIRouter(prefix="/setup", tags=["setup"])


def _is_loopback_client(request: Request) -> bool:
    return is_loopback_host(request.client.host if request.client else None)


@router.get("/status", response_model=SetupStatusResponse)
async def get_setup_status(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    coordinator = getattr(request.app.state, "automation", None)
    jobs: list[AutomationJobStatus] = []
    if coordinator:
        jobs = [
            AutomationJobStatus(
                name=item.name,
                enabled=item.enabled,
                interval_seconds=item.interval_seconds,
                last_run_at=item.last_run_at,
                last_status=item.last_status,
                detail=item.detail,
            )
            for item in coordinator.status()
        ]
    return await SetupService(session).status(jobs)


async def reset_development_state(
    payload: DevelopmentResetRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if not settings.DEVELOPMENT_RESET_AVAILABLE:
        raise HTTPException(status_code=404, detail="Not found.")
    if not _is_loopback_client(request):
        raise HTTPException(
            status_code=403,
            detail="Development reset is only available from the local machine.",
        )
    if payload.confirmation_text.strip().upper() != "RESET INVESTOS":
        raise HTTPException(
            status_code=400, detail="Confirmation text must be RESET INVESTOS."
        )

    result = await SetupService(session).reset_development_state()
    coordinator = getattr(request.app.state, "automation", None)
    if coordinator:
        try:
            coordinator.reset_state()
        except Exception as exc:
            result.warnings.append(f"automation_reset_failed: {exc}")
            result.detail = (
                f"{result.detail} Automation telemetry reset did not complete cleanly."
            )
    return result


# Do not register a destructive route in ordinary application processes. This
# prevents request validation, dependencies, and API metadata from exposing a
# dormant reset surface when the explicit development gate is off.
if settings.DEVELOPMENT_RESET_AVAILABLE:
    router.post("/reset", response_model=DevelopmentResetResponse)(
        reset_development_state
    )
