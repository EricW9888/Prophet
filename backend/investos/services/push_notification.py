from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid, VapidException
from pywebpush import WebPushException, webpush
from sqlalchemy import func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from investos.config import settings
from investos.core.url_security import resolve_public_url
from investos.models.notification import (
    PushNotificationDelivery,
    PushNotificationEvent,
    PushSubscription,
)
from investos.models.watcher import ActiveWatcher
from investos.services.runtime_settings import RuntimeSettingsStore


class PushSubscriptionError(ValueError):
    pass


class PushConfigurationError(RuntimeError):
    pass


class PushNotificationService:
    _vapid_lock = threading.Lock()

    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def endpoint_hash(endpoint: str) -> str:
        return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()

    @classmethod
    def _vapid_path(cls) -> Path:
        return Path(settings.STORAGE_DIR) / "_system" / "web_push_vapid.pem"

    @classmethod
    def _vapid_identity(cls) -> Vapid:
        path = cls._vapid_path()
        with cls._vapid_lock:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            vapid = Vapid.from_file(str(path))
            try:
                path.chmod(0o600)
            except OSError:
                pass
            return vapid

    @classmethod
    def application_server_key(cls) -> str:
        raw = cls._vapid_identity().public_key.public_bytes(
            Encoding.X962,
            PublicFormat.UncompressedPoint,
        )
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    @staticmethod
    def _normalize_subject(value: str, *, allow_email: bool = False) -> str:
        configured = value.strip()
        if allow_email and "@" in configured and ":" not in configured:
            configured = f"mailto:{configured}"
        clean = not any(
            char.isspace() or ord(char) < 32 for char in unquote(configured)
        )
        try:
            parsed = urlparse(configured)
            local, separator, domain = parsed.path.rpartition("@")
            valid_mailto = (
                parsed.scheme == "mailto"
                and bool(local and separator and domain)
                and "@" not in local
                and not parsed.netloc
                and not parsed.query
                and not parsed.fragment
            )
            valid_https_origin = (
                parsed.scheme == "https"
                and bool(parsed.hostname)
                and (parsed.port is None or parsed.port > 0)
                and parsed.username is None
                and parsed.password is None
                and parsed.path in {"", "/"}
                and not parsed.params
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            valid_mailto = valid_https_origin = False
        if not clean or not (valid_mailto or valid_https_origin):
            raise PushConfigurationError(
                "Enter a sender email or an HTTPS site origin in Device notifications, "
                "or set WEB_PUSH_VAPID_SUBJECT on the host."
            )
        return configured.rstrip("/") if valid_https_origin else configured

    @classmethod
    def _vapid_subject(cls, saved_contact: str | None = None) -> str:
        if saved_contact is None:
            saved_contact = RuntimeSettingsStore.load().push_sender_contact
        configured = saved_contact or (settings.WEB_PUSH_VAPID_SUBJECT or "").strip()
        if not configured:
            owner = (settings.PROPHET_REMOTE_ACCESS_USER or "").strip()
            if "@" in owner and not any(character.isspace() for character in owner):
                configured = f"mailto:{owner}"
        return cls._normalize_subject(configured)

    @classmethod
    def set_sender_contact(cls, value: str | None) -> None:
        if not settings.WEB_PUSH_ENABLED:
            raise PushConfigurationError(
                "Push delivery is disabled on the Prophet host."
            )
        contact = (
            cls._normalize_subject(value, allow_email=True)
            if value is not None
            else None
        )
        if contact is not None:
            try:
                # Check the configured signer locally; no push request is made here.
                cls._vapid_identity().sign(
                    {"aud": "https://example.invalid", "sub": contact}
                )
            except VapidException as exc:
                raise PushConfigurationError(
                    "Use a valid sender email or HTTPS site origin."
                ) from exc
        runtime = RuntimeSettingsStore.load()
        runtime.push_sender_contact = contact
        RuntimeSettingsStore.save(runtime)

    async def status(self) -> dict[str, Any]:
        active_count = int(
            await self.session.scalar(
                select(func.count())
                .select_from(PushSubscription)
                .where(PushSubscription.is_active.is_(True))
            )
            or 0
        )
        configuration_error = None
        ready = False
        saved_contact = RuntimeSettingsStore.load().push_sender_contact
        if settings.WEB_PUSH_ENABLED:
            try:
                self._vapid_subject(saved_contact or "")
                ready = True
            except PushConfigurationError as exc:
                configuration_error = str(exc)
        return {
            "enabled": settings.WEB_PUSH_ENABLED,
            "ready": ready,
            "sender_contact_saved": bool(saved_contact),
            "configuration_error": configuration_error,
            "application_server_key": self.application_server_key() if ready else None,
            "active_subscription_count": active_count,
        }

    @staticmethod
    def _event_cursor(at: datetime, event_id: UUID) -> str:
        return f"{at.isoformat()}|{event_id}"

    async def foreground_events(self, *, cursor: str | None = None, limit: int = 50):
        """Read the existing generic outbox without creating a push subscription.

        An initial read establishes a baseline, so opting in never replays the
        retained history. Device-specific tests are excluded from this feed.
        """
        after = None
        if cursor is not None:
            try:
                stamp, identity = cursor.split("|")
                at = datetime.fromisoformat(stamp)
                if at.tzinfo is None:
                    raise ValueError("Timestamp must include a timezone.")
                after = (at, UUID(identity))
            except (ValueError, TypeError) as exc:
                raise PushSubscriptionError("Invalid notification cursor.") from exc
        if not settings.WEB_PUSH_ENABLED:
            return dict(enabled=False, items=[], next_cursor=cursor, has_more=False)
        eligible = PushNotificationEvent.category == "watcher_transition"
        if after is None:
            latest = await self.session.scalar(
                select(PushNotificationEvent)
                .where(eligible)
                .order_by(
                    PushNotificationEvent.created_at.desc(),
                    PushNotificationEvent.id.desc(),
                )
                .limit(1)
            )
            baseline = self._event_cursor(
                latest.created_at if latest else datetime.now(UTC),
                latest.id if latest else UUID(int=0),
            )
            return dict(enabled=True, items=[], next_cursor=baseline, has_more=False)
        rows = list(
            await self.session.scalars(
                select(PushNotificationEvent)
                .where(
                    eligible,
                    tuple_(PushNotificationEvent.created_at, PushNotificationEvent.id)
                    > after,
                )
                .order_by(PushNotificationEvent.created_at, PushNotificationEvent.id)
                .limit(limit + 1)
            )
        )
        items = rows[:limit]
        return dict(
            enabled=True,
            items=[
                dict(
                    title=row.title,
                    body=row.body,
                    url=row.navigate_path,
                    tag=row.event_key,
                )
                for row in items
            ],
            next_cursor=(
                self._event_cursor(items[-1].created_at, items[-1].id)
                if items
                else cursor
            ),
            has_more=len(rows) > limit,
        )

    async def subscribe(
        self,
        *,
        endpoint: str,
        p256dh: str,
        auth: str,
        user_agent: str | None,
    ) -> PushSubscription:
        if not settings.WEB_PUSH_ENABLED:
            raise PushSubscriptionError("Owner notifications are disabled.")
        self._vapid_subject()
        self.application_server_key()
        endpoint = endpoint.strip()
        if len(endpoint) > 2048:
            raise PushSubscriptionError("The push endpoint is too long.")
        await resolve_public_url(endpoint, allowed_ports=frozenset({443}))
        self._validate_key("p256dh", p256dh, expected_length=65)
        self._validate_key("auth", auth, expected_length=16)

        endpoint_hash = self.endpoint_hash(endpoint)
        now = datetime.now(UTC)
        statement = (
            insert(PushSubscription)
            .values(
                id=uuid4(),
                endpoint=endpoint,
                endpoint_hash=endpoint_hash,
                p256dh=p256dh,
                auth=auth,
                user_agent=(user_agent or "")[:512] or None,
                is_active=True,
                created_at=now,
                updated_at=now,
                disabled_at=None,
            )
            .on_conflict_do_update(
                constraint="uq_push_subscriptions_endpoint_hash",
                set_={
                    "endpoint": endpoint,
                    "p256dh": p256dh,
                    "auth": auth,
                    "user_agent": (user_agent or "")[:512] or None,
                    "is_active": True,
                    "updated_at": now,
                    "disabled_at": None,
                },
            )
            .returning(PushSubscription.id)
        )
        subscription_id = await self.session.scalar(statement)
        await self.session.commit()
        subscription = await self.session.get(PushSubscription, subscription_id)
        if subscription is None:
            raise RuntimeError("The push subscription could not be stored.")
        await self.session.refresh(subscription)
        return subscription

    async def unsubscribe(self, *, endpoint: str) -> bool:
        now = datetime.now(UTC)
        result = await self.session.execute(
            update(PushSubscription)
            .where(
                PushSubscription.endpoint_hash == self.endpoint_hash(endpoint),
                PushSubscription.is_active.is_(True),
            )
            .values(is_active=False, disabled_at=now, updated_at=now)
        )
        await self.session.commit()
        return bool(result.rowcount)

    async def enqueue_watch_transition(self, watcher: ActiveWatcher) -> int:
        if not settings.WEB_PUSH_ENABLED:
            return 0
        subscription_ids = list(
            (
                await self.session.scalars(
                    select(PushSubscription.id).where(
                        PushSubscription.is_active.is_(True)
                    )
                )
            ).all()
        )
        event_key = f"watcher:{watcher.id}:{watcher.status}"
        event_id = await self.session.scalar(
            insert(PushNotificationEvent)
            .values(
                id=uuid4(),
                event_key=event_key,
                category="watcher_transition",
                title="Prophet watch needs review",
                body=(
                    "A monitored condition changed. Open Prophet to review the "
                    "evidence and adjustment plan."
                ),
                navigate_path=(
                    "/risk"
                    if watcher.condition_type == "portfolio_loss_budget"
                    else "/timeline"
                ),
                created_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(constraint="uq_push_notification_events_event_key")
            .returning(PushNotificationEvent.id)
        )
        if event_id is None:
            return 0

        if not subscription_ids:
            return 0

        now = datetime.now(UTC)
        await self.session.execute(
            insert(PushNotificationDelivery)
            .values(
                [
                    {
                        "id": uuid4(),
                        "event_id": event_id,
                        "subscription_id": subscription_id,
                        "status": "pending",
                        "attempt_count": 0,
                        "next_attempt_at": now,
                        "created_at": now,
                        "updated_at": now,
                    }
                    for subscription_id in subscription_ids
                ]
            )
            .on_conflict_do_nothing(
                constraint="uq_push_notification_deliveries_event_subscription"
            )
        )
        return len(subscription_ids)

    async def enqueue_test(self, *, endpoint: str) -> UUID:
        subscription = await self.session.scalar(
            select(PushSubscription).where(
                PushSubscription.endpoint_hash == self.endpoint_hash(endpoint),
                PushSubscription.is_active.is_(True),
            )
        )
        if subscription is None:
            raise PushSubscriptionError("This device is not subscribed.")

        event = PushNotificationEvent(
            event_key=f"test:{uuid4()}",
            category="test",
            title="Prophet notifications are ready",
            body="This device can receive private owner alerts.",
            navigate_path="/settings",
        )
        self.session.add(event)
        await self.session.flush()
        delivery = PushNotificationDelivery(
            event_id=event.id,
            subscription_id=subscription.id,
        )
        self.session.add(delivery)
        await self.session.commit()
        return delivery.id

    async def dispatch_pending(
        self, *, delivery_id: UUID | None = None, limit: int = 50
    ) -> dict[str, int]:
        if not settings.WEB_PUSH_ENABLED:
            return {
                "selected": 0,
                "sent": 0,
                "retrying": 0,
                "retired": 0,
                "failed": 0,
                "configuration_failed": 0,
            }

        now = datetime.now(UTC)
        stale_sending = now - timedelta(minutes=5)
        await self.session.execute(
            update(PushNotificationDelivery)
            .where(
                PushNotificationDelivery.status == "sending",
                PushNotificationDelivery.updated_at <= stale_sending,
            )
            .values(status="retry", next_attempt_at=now, updated_at=now)
        )
        await self.session.commit()

        statement = (
            select(
                PushNotificationDelivery,
                PushNotificationEvent,
                PushSubscription,
            )
            .join(
                PushNotificationEvent,
                PushNotificationEvent.id == PushNotificationDelivery.event_id,
            )
            .join(
                PushSubscription,
                PushSubscription.id == PushNotificationDelivery.subscription_id,
            )
            .where(
                PushNotificationDelivery.status.in_(("pending", "retry")),
                PushNotificationDelivery.next_attempt_at <= now,
                PushSubscription.is_active.is_(True),
            )
        )
        if delivery_id is not None:
            statement = statement.where(PushNotificationDelivery.id == delivery_id)
        statement = (
            statement.order_by(PushNotificationDelivery.created_at)
            .limit(max(1, min(limit, 200)))
            .with_for_update(skip_locked=True)
        )

        rows = list((await self.session.execute(statement)).all())
        if not rows:
            return {
                "selected": 0,
                "sent": 0,
                "retrying": 0,
                "retired": 0,
                "failed": 0,
                "configuration_failed": 0,
            }

        for delivery, _event, _subscription in rows:
            delivery.status = "sending"
            delivery.attempt_count += 1
            delivery.updated_at = now
        await self.session.commit()

        result = {
            "selected": len(rows),
            "sent": 0,
            "retrying": 0,
            "retired": 0,
            "failed": 0,
            "configuration_failed": 0,
        }
        for delivery, event, subscription in rows:
            outcome = await self._send(delivery, event, subscription)
            result[outcome] += 1
            await self.session.commit()
        return result

    async def _send(
        self,
        delivery: PushNotificationDelivery,
        event: PushNotificationEvent,
        subscription: PushSubscription,
    ) -> str:
        payload = json.dumps(
            {
                "title": event.title,
                "body": event.body,
                "url": event.navigate_path,
                "tag": event.event_key,
            },
            separators=(",", ":"),
        )
        try:
            request = partial(
                webpush,
                subscription_info={
                    "endpoint": subscription.endpoint,
                    "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
                },
                data=payload,
                vapid_private_key=str(self._vapid_path()),
                vapid_claims={"sub": self._vapid_subject()},
                timeout=settings.WEB_PUSH_TIMEOUT_SECONDS,
                ttl=300,
            )
            await asyncio.to_thread(request)
        except (PushConfigurationError, VapidException) as exc:
            return self._record_configuration_failure(
                delivery,
                reason=str(exc),
                now=datetime.now(UTC),
            )
        except WebPushException as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            return self._record_failure(
                delivery,
                subscription,
                status_code=status_code,
                now=datetime.now(UTC),
            )
        except Exception:
            return self._record_failure(
                delivery,
                subscription,
                status_code=None,
                now=datetime.now(UTC),
            )

        now = datetime.now(UTC)
        delivery.status = "sent"
        delivery.delivered_at = now
        delivery.last_error = None
        delivery.updated_at = now
        subscription.last_success_at = now
        subscription.updated_at = now
        return "sent"

    @staticmethod
    def _record_configuration_failure(
        delivery: PushNotificationDelivery,
        *,
        reason: str,
        now: datetime,
    ) -> str:
        delivery.status = "failed"
        delivery.last_error = f"Push configuration is invalid: {reason}"
        delivery.updated_at = now
        return "configuration_failed"

    @staticmethod
    def _record_failure(
        delivery: PushNotificationDelivery,
        subscription: PushSubscription,
        *,
        status_code: int | None,
        now: datetime,
    ) -> str:
        if status_code in {404, 410}:
            subscription.is_active = False
            subscription.disabled_at = now
            subscription.updated_at = now
            delivery.status = "retired"
            delivery.last_error = f"Push subscription retired after HTTP {status_code}."
            delivery.updated_at = now
            return "retired"

        if delivery.attempt_count >= settings.WEB_PUSH_MAX_ATTEMPTS:
            delivery.status = "failed"
            delivery.last_error = (
                f"Push delivery stopped after HTTP {status_code}."
                if status_code
                else "Push delivery stopped after a network failure."
            )
            delivery.updated_at = now
            return "failed"

        delay_seconds = min(3600, 60 * (2 ** max(0, delivery.attempt_count - 1)))
        delivery.status = "retry"
        delivery.next_attempt_at = now + timedelta(seconds=delay_seconds)
        delivery.last_error = (
            f"Push service returned HTTP {status_code}; retry scheduled."
            if status_code
            else "Push service was unreachable; retry scheduled."
        )
        delivery.updated_at = now
        return "retrying"

    @staticmethod
    def _validate_key(name: str, value: str, *, expected_length: int) -> None:
        try:
            padding = "=" * (-len(value) % 4)
            decoded = base64.urlsafe_b64decode(value + padding)
        except (ValueError, TypeError) as exc:
            raise PushSubscriptionError(f"The {name} key is invalid.") from exc
        if len(decoded) != expected_length:
            raise PushSubscriptionError(f"The {name} key is invalid.")
