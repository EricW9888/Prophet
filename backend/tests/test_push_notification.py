from __future__ import annotations

import base64
import stat
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy import delete, func, select

from investos.config import settings
from investos.db import async_session_maker, engine
from investos.models.notification import (
    PushNotificationDelivery,
    PushNotificationEvent,
    PushSubscription,
)
from investos.models.watcher import ActiveWatcher
from investos.services.push_notification import (
    PushConfigurationError,
    PushNotificationService,
    PushSubscriptionError,
)


def encoded_bytes(length: int) -> str:
    return base64.urlsafe_b64encode(bytes(range(length))).rstrip(b"=").decode("ascii")


@pytest.fixture(autouse=True)
def valid_vapid_contact(monkeypatch) -> None:
    monkeypatch.setattr(
        settings,
        "WEB_PUSH_VAPID_SUBJECT",
        "mailto:prophet-tests@example.com",
    )
    monkeypatch.setattr(settings, "PROPHET_REMOTE_ACCESS_USER", None)


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("mailto:owner@example.com", "mailto:owner@example.com"),
        ("https://example.com", "https://example.com"),
        ("https://example.com/", "https://example.com"),
    ],
)
def test_vapid_subject_accepts_contact_uris(
    monkeypatch, configured: str, expected: str
) -> None:
    monkeypatch.setattr(settings, "WEB_PUSH_VAPID_SUBJECT", configured)

    assert PushNotificationService._vapid_subject() == expected


def test_vapid_subject_uses_owner_email_when_not_explicit(monkeypatch) -> None:
    monkeypatch.setattr(settings, "WEB_PUSH_VAPID_SUBJECT", None)
    monkeypatch.setattr(settings, "PROPHET_REMOTE_ACCESS_USER", "owner@example.com")

    assert PushNotificationService._vapid_subject() == "mailto:owner@example.com"


@pytest.mark.parametrize(
    "configured",
    [None, "https://github.com/EricW9888/Prophet", "owner@example.com"],
)
def test_vapid_subject_rejects_missing_or_invalid_contact(
    monkeypatch, configured: str | None
) -> None:
    monkeypatch.setattr(settings, "WEB_PUSH_VAPID_SUBJECT", configured)
    monkeypatch.setattr(settings, "PROPHET_REMOTE_ACCESS_USER", None)

    with pytest.raises(PushConfigurationError, match="WEB_PUSH_VAPID_SUBJECT"):
        PushNotificationService._vapid_subject()


def test_vapid_identity_is_private_and_stable(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))

    first = PushNotificationService.application_server_key()
    second = PushNotificationService.application_server_key()
    private_key_path = tmp_path / "_system" / "web_push_vapid.pem"

    assert first == second
    assert len(base64.urlsafe_b64decode(first + "=" * (-len(first) % 4))) == 65
    assert private_key_path.is_file()
    assert stat.S_IMODE(private_key_path.stat().st_mode) == 0o600


async def public_endpoint(*_args, **_kwargs):
    return SimpleNamespace()


async def clean_push_state() -> None:
    async with async_session_maker() as session:
        subscription_ids = list(
            (
                await session.scalars(
                    select(PushSubscription.id).where(
                        PushSubscription.endpoint.like("https://push.example.test/%")
                    )
                )
            ).all()
        )
        if subscription_ids:
            event_ids = list(
                (
                    await session.scalars(
                        select(PushNotificationDelivery.event_id).where(
                            PushNotificationDelivery.subscription_id.in_(
                                subscription_ids
                            )
                        )
                    )
                ).all()
            )
            await session.execute(
                delete(PushNotificationDelivery).where(
                    PushNotificationDelivery.subscription_id.in_(subscription_ids)
                )
            )
            if event_ids:
                await session.execute(
                    delete(PushNotificationEvent).where(
                        PushNotificationEvent.id.in_(event_ids)
                    )
                )
            await session.execute(
                delete(PushSubscription).where(
                    PushSubscription.id.in_(subscription_ids)
                )
            )
        await session.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_migrated_schema_owns_push_delivery_uniqueness() -> None:
    expected = {
        "push_subscriptions": {
            "uq_push_subscriptions_endpoint_hash": ("endpoint_hash",)
        },
        "push_notification_events": {
            "uq_push_notification_events_event_key": ("event_key",)
        },
        "push_notification_deliveries": {
            "uq_push_notification_deliveries_event_subscription": (
                "event_id",
                "subscription_id",
            )
        },
    }

    def inspect_constraints(connection):
        inspector = sa.inspect(connection)
        return {
            table_name: {
                constraint["name"]: tuple(constraint["column_names"])
                for constraint in inspector.get_unique_constraints(table_name)
            }
            for table_name in expected
        }

    async with engine.connect() as connection:
        actual = await connection.run_sync(inspect_constraints)

    for table_name, constraints in expected.items():
        for name, columns in constraints.items():
            assert actual[table_name].get(name) == columns


@pytest.mark.asyncio(loop_scope="session")
async def test_subscription_upsert_reactivates_one_device(monkeypatch) -> None:
    await clean_push_state()
    monkeypatch.setattr(
        "investos.services.push_notification.resolve_public_url", public_endpoint
    )
    monkeypatch.setattr(
        PushNotificationService,
        "application_server_key",
        classmethod(lambda cls: "test-public-key"),
    )
    endpoint = "https://push.example.test/subscription/one"
    try:
        async with async_session_maker() as session:
            service = PushNotificationService(session)
            first = await service.subscribe(
                endpoint=endpoint,
                p256dh=encoded_bytes(65),
                auth=encoded_bytes(16),
                user_agent="first agent",
            )
            await service.unsubscribe(endpoint=endpoint)
            second = await service.subscribe(
                endpoint=endpoint,
                p256dh=encoded_bytes(65),
                auth=encoded_bytes(16),
                user_agent="updated agent",
            )
            count = await session.scalar(
                select(func.count()).select_from(PushSubscription)
            )

        assert first.id == second.id
        assert second.is_active is True
        assert second.disabled_at is None
        assert second.user_agent == "updated agent"
        assert count == 1
    finally:
        await clean_push_state()


@pytest.mark.asyncio(loop_scope="session")
async def test_watcher_transition_enqueues_one_generic_delivery(monkeypatch) -> None:
    await clean_push_state()
    monkeypatch.setattr(
        "investos.services.push_notification.resolve_public_url", public_endpoint
    )
    monkeypatch.setattr(
        PushNotificationService,
        "application_server_key",
        classmethod(lambda cls: "test-public-key"),
    )
    watcher = ActiveWatcher(
        id=uuid4(),
        source="test",
        ticker="PRIVATE",
        condition_type="price_below",
        condition_params_json={"threshold": 10},
        objective="Private objective",
        adjustment_plan="Private plan",
        status="triggered",
        is_active=False,
    )
    try:
        async with async_session_maker() as session:
            service = PushNotificationService(session)
            await service.subscribe(
                endpoint="https://push.example.test/subscription/two",
                p256dh=encoded_bytes(65),
                auth=encoded_bytes(16),
                user_agent="test",
            )
            first = await service.enqueue_watch_transition(watcher)
            second = await service.enqueue_watch_transition(watcher)
            await session.commit()
            event = await session.scalar(select(PushNotificationEvent))
            delivery_count = await session.scalar(
                select(func.count()).select_from(PushNotificationDelivery)
            )

        assert first == 1
        assert second == 0
        assert event is not None
        assert event.event_key == f"watcher:{watcher.id}:triggered"
        assert "PRIVATE" not in event.title + event.body
        assert "Private" not in event.title + event.body
        assert delivery_count == 1
    finally:
        await clean_push_state()


@pytest.mark.asyncio(loop_scope="session")
async def test_dispatch_marks_success_without_exposing_payload(monkeypatch) -> None:
    await clean_push_state()
    monkeypatch.setattr(
        "investos.services.push_notification.resolve_public_url", public_endpoint
    )
    monkeypatch.setattr(
        PushNotificationService,
        "application_server_key",
        classmethod(lambda cls: "test-public-key"),
    )
    sent_payloads: list[str] = []

    def fake_webpush(**kwargs):
        sent_payloads.append(kwargs["data"])

    monkeypatch.setattr("investos.services.push_notification.webpush", fake_webpush)
    try:
        async with async_session_maker() as session:
            service = PushNotificationService(session)
            subscription = await service.subscribe(
                endpoint="https://push.example.test/subscription/three",
                p256dh=encoded_bytes(65),
                auth=encoded_bytes(16),
                user_agent="test",
            )
            delivery_id = await service.enqueue_test(endpoint=subscription.endpoint)
            result = await service.dispatch_pending(delivery_id=delivery_id)
            delivery = await session.get(PushNotificationDelivery, delivery_id)

        assert result["sent"] == 1
        assert delivery is not None
        assert delivery.status == "sent"
        assert delivery.delivered_at is not None
        assert len(sent_payloads) == 1
        assert "portfolio" not in sent_payloads[0].lower()
        assert "trade" not in sent_payloads[0].lower()
    finally:
        await clean_push_state()


@pytest.mark.asyncio(loop_scope="session")
async def test_invalid_vapid_contact_fails_without_retrying(monkeypatch) -> None:
    await clean_push_state()
    monkeypatch.setattr(
        "investos.services.push_notification.resolve_public_url", public_endpoint
    )
    monkeypatch.setattr(
        PushNotificationService,
        "application_server_key",
        classmethod(lambda cls: "test-public-key"),
    )
    try:
        async with async_session_maker() as session:
            service = PushNotificationService(session)
            subscription = await service.subscribe(
                endpoint="https://push.example.test/subscription/configuration",
                p256dh=encoded_bytes(65),
                auth=encoded_bytes(16),
                user_agent="test",
            )
            delivery_id = await service.enqueue_test(endpoint=subscription.endpoint)
            monkeypatch.setattr(
                settings,
                "WEB_PUSH_VAPID_SUBJECT",
                "https://github.com/EricW9888/Prophet",
            )
            result = await service.dispatch_pending(delivery_id=delivery_id)
            delivery = await session.get(PushNotificationDelivery, delivery_id)

        assert result["configuration_failed"] == 1
        assert result["retrying"] == 0
        assert delivery is not None
        assert delivery.status == "failed"
        assert delivery.next_attempt_at is not None
        assert "WEB_PUSH_VAPID_SUBJECT" in (delivery.last_error or "")
    finally:
        await clean_push_state()


def test_permanently_invalid_subscription_is_retired() -> None:
    delivery = SimpleNamespace(
        attempt_count=1,
        status="sending",
        last_error=None,
        updated_at=None,
    )
    subscription = SimpleNamespace(
        is_active=True,
        disabled_at=None,
        updated_at=None,
    )
    now = datetime.now(UTC)

    outcome = PushNotificationService._record_failure(
        delivery,
        subscription,
        status_code=410,
        now=now,
    )

    assert outcome == "retired"
    assert subscription.is_active is False
    assert subscription.disabled_at == now
    assert delivery.status == "retired"


@pytest.mark.parametrize(
    ("name", "value", "length"),
    [("p256dh", "invalid", 65), ("auth", encoded_bytes(15), 16)],
)
def test_subscription_keys_require_browser_key_shape(name, value, length) -> None:
    with pytest.raises(PushSubscriptionError, match=f"{name} key is invalid"):
        PushNotificationService._validate_key(name, value, expected_length=length)
