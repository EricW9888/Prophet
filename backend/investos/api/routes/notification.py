from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from investos.core.url_security import UrlFetchError
from investos.db import get_session
from investos.schemas.notification import (
    PushNotificationStatusResponse,
    PushNotificationTestResponse,
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


@router.get("/status", response_model=PushNotificationStatusResponse)
async def notification_status(session: AsyncSession = Depends(get_session)):
    return await PushNotificationService(session).status()


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
