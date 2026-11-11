from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from investos.core.url_security import UrlFetchError
from investos.db import get_session
from investos.schemas.notification import (
    ForegroundNotificationPage,
    PushNotificationStatusResponse,
    PushNotificationTestResponse,
    PushSenderContactUpdate,
    PushSubscriptionCreate,
    PushSubscriptionEndpointRequest,
    PushSubscriptionResponse,
)
from investos.services.push_notification import (
    PushConfigurationError,
    PushNotificationService,
    PushSubscriptionError,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("/events", response_model=ForegroundNotificationPage)
async def foreground_notification_events(
    cursor: str | None = Query(None, max_length=128),
    limit: int = Query(50, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
):
    try:
        return await PushNotificationService(session).foreground_events(
            cursor=cursor, limit=limit
        )
    except PushSubscriptionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/status", response_model=PushNotificationStatusResponse)
async def notification_status(session: AsyncSession = Depends(get_session)):
    return await PushNotificationService(session).status()


@router.put("/contact", response_model=PushNotificationStatusResponse)
async def set_notification_contact(
    payload: PushSenderContactUpdate,
    session: AsyncSession = Depends(get_session),
):
    service = PushNotificationService(session)
    try:
        service.set_sender_contact(payload.sender_contact)
    except PushConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await service.status()


@router.post("/subscriptions", response_model=PushSubscriptionResponse)
async def subscribe_device(
    payload: PushSubscriptionCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    try:
        subscription = await PushNotificationService(session).subscribe(
            endpoint=payload.endpoint,
            p256dh=payload.keys.p256dh,
            auth=payload.keys.auth,
            user_agent=request.headers.get("user-agent"),
        )
    except (PushSubscriptionError, PushConfigurationError, UrlFetchError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return PushSubscriptionResponse(
        subscription_id=subscription.id,
        active=subscription.is_active,
    )


@router.post("/subscriptions/remove")
async def unsubscribe_device(
    payload: PushSubscriptionEndpointRequest,
    session: AsyncSession = Depends(get_session),
):
    removed = await PushNotificationService(session).unsubscribe(
        endpoint=payload.endpoint
    )
    return {"removed": removed}


@router.post("/test", response_model=PushNotificationTestResponse)
async def test_notification(
    payload: PushSubscriptionEndpointRequest,
    session: AsyncSession = Depends(get_session),
):
    service = PushNotificationService(session)
    try:
        delivery_id = await service.enqueue_test(endpoint=payload.endpoint)
    except PushSubscriptionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = await service.dispatch_pending(delivery_id=delivery_id)
    sent = result["sent"] == 1
    if sent:
        status = "sent"
    elif result["retrying"]:
        status = "retry_scheduled"
    elif result["retired"]:
        status = "subscription_retired"
    elif result["configuration_failed"]:
        status = "configuration_error"
    else:
        status = "failed"
    return PushNotificationTestResponse(status=status, sent=sent)
