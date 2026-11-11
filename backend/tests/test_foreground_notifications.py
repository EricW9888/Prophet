"""Foreground alerts reuse the generic outbox; they never subscribe or trade."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from investos.api.routes.notification import router
from investos.config import settings
from investos.db import get_session
from investos.models.notification import (
    PushNotificationDelivery,
    PushNotificationEvent,
    PushSubscription,
)
from investos.models.watcher import ActiveWatcher
from investos.services.push_notification import PushNotificationService


@pytest.fixture
async def notification_session(monkeypatch):
    monkeypatch.setattr(settings, "WEB_PUSH_ENABLED", True)
    monkeypatch.setattr(settings, "WEB_PUSH_VAPID_SUBJECT", None)
    monkeypatch.setattr(settings, "PROPHET_REMOTE_ACCESS_USER", None)
    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, poolclass=NullPool)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            await session.execute(delete(PushNotificationDelivery))
            await session.execute(delete(PushSubscription))
            await session.execute(delete(PushNotificationEvent))
            yield session
        if transaction.is_active:
            await transaction.rollback()
    await engine.dispose()


async def test_watch_event_survives_without_push_setup_or_subscriptions(
    notification_session,
):
    service = PushNotificationService(notification_session)
    baseline = await service.foreground_events()
    assert baseline["items"] == []
    assert baseline["enabled"] is True
    watcher = ActiveWatcher(
        id=uuid4(),
        source="test",
        ticker="PRIVATE",
        condition_type="price_below",
        condition_params_json={"threshold": 3},
        objective="Private position",
        adjustment_plan="Private plan",
        status="triggered",
        is_active=False,
    )
    assert await service.enqueue_watch_transition(watcher) == 0
    assert await service.enqueue_watch_transition(watcher) == 0
    page = await service.foreground_events(cursor=baseline["next_cursor"])
    assert len(page["items"]) == 1
    assert page["items"][0]["url"] == "/timeline"
    assert "PRIVATE" not in str(page["items"])
    assert "Private plan" not in str(page["items"])
    assert (
        await notification_session.scalar(
            select(func.count()).select_from(PushNotificationDelivery)
        )
        == 0
    )
    assert (await service.foreground_events(cursor=page["next_cursor"]))["items"] == []
    assert (await service.foreground_events())["items"] == []


async def test_event_pages_preserve_same_timestamp_order_exclude_tests_and_validate_cursor(
    notification_session, monkeypatch
):
    service = PushNotificationService(notification_session)
    stamp = datetime.now(UTC) + timedelta(seconds=1)
    notification_session.add_all(
        [
            PushNotificationEvent(
                id=UUID(int=index),
                event_key=f"fixture:{index}",
                category="watcher_transition",
                title="Watch changed",
                body="Open Prophet to review.",
                navigate_path="/risk",
                created_at=stamp,
            )
            for index in range(1, 132)
        ]
    )
    notification_session.add(
        PushNotificationEvent(
            event_key=f"test:{uuid4()}",
            category="test",
            title="Private device test",
            body="Test only",
            navigate_path="/settings",
            created_at=stamp,
        )
    )
    await notification_session.flush()
    app = FastAPI()
    app.include_router(router)

    async def session_override():
        yield notification_session

    app.dependency_overrides[get_session] = session_override
    cursor = service._event_cursor(stamp - timedelta(seconds=1), UUID(int=0))
    seen = []
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        for _ in range(3):
            response = await client.get(
                "/notifications/events", params={"cursor": cursor}
            )
            assert response.status_code == 200
            page = response.json()
            assert len(page["items"]) <= 50
            seen.extend(item["tag"] for item in page["items"])
            cursor = page["next_cursor"]
        assert page["has_more"] is False
        assert seen == [f"fixture:{index}" for index in range(1, 132)]
        assert (
            await client.get("/notifications/events", params={"cursor": "invalid"})
        ).status_code == 400
        naive = service._event_cursor(datetime(2025, 1, 1), UUID(int=0))
        assert (
            await client.get("/notifications/events", params={"cursor": naive})
        ).status_code == 400
        assert (
            await client.get("/notifications/events", params={"limit": 101})
        ).status_code == 422
        monkeypatch.setattr(settings, "WEB_PUSH_ENABLED", False)
        disabled = (
            await client.get("/notifications/events", params={"cursor": cursor})
        ).json()
        assert disabled == dict(
            enabled=False, items=[], next_cursor=cursor, has_more=False
        )
