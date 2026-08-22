from fastapi import APIRouter, HTTPException, Request

from investos.schemas.automation import AutomationJobStatus, AutomationStatusResponse

router = APIRouter(prefix="/automation", tags=["automation"])


@router.get("/status", response_model=AutomationStatusResponse)
async def get_automation_status(request: Request):
    coordinator = getattr(request.app.state, "automation", None)
    jobs = []
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
    return AutomationStatusResponse(
        automation_enabled=bool(coordinator),
        jobs=jobs,
    )


@router.post("/run/{job_name}", response_model=AutomationStatusResponse)
async def run_automation_job(job_name: str, request: Request):
    coordinator = getattr(request.app.state, "automation", None)
    if not coordinator:
        raise HTTPException(
            status_code=503, detail="Automation coordinator is not running"
        )
    try:
        await coordinator.run_job(job_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
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
    return AutomationStatusResponse(automation_enabled=True, jobs=jobs)
