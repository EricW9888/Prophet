import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from investos.config import settings
from investos.core.request_security import api_request_allowed
from investos.services.automation import AutomationCoordinator
from investos.services.live_jobs import LiveJobTracker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    automation = AutomationCoordinator()
    app.state.automation = automation
    app.state.live_jobs = LiveJobTracker()
    try:
        automation.start()
    except Exception:
        logger.exception("Failed to start automation")

    yield

    try:
        await automation.shutdown()
    except Exception:
        logger.exception("Failed to shut down automation")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version="0.1.0",
    lifespan=lifespan,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in settings.FRONTEND_ORIGINS.split(",")
        if origin.strip()
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_frontend_origins = frozenset(
    origin.strip().rstrip("/")
    for origin in settings.FRONTEND_ORIGINS.split(",")
    if origin.strip()
)


@app.middleware("http")
async def enforce_local_api_boundary(request: Request, call_next):
    if request.url.path.startswith(settings.API_V1_STR):
        client_host = request.client.host if request.client else None
        if not api_request_allowed(
            method=request.method,
            client_host=client_host,
            origin=request.headers.get("origin"),
            allowed_origins=_frontend_origins,
            allow_non_loopback=settings.API_ALLOW_NON_LOOPBACK,
        ):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": (
                        "Prophet's private API only accepts local requests and "
                        "state changes from an allowed frontend origin."
                    )
                },
            )
    return await call_next(request)


from investos.api.routes import api_router

app.include_router(api_router, prefix=settings.API_V1_STR)


@app.get("/health")
async def health_check():
    return {"status": "ok"}
