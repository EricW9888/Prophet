import copy
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from test_portfolio_risk import frozen_packet

from investos.config import settings
from investos.db import async_session_maker
from investos.models.notification import (
    PushNotificationDelivery,
    PushNotificationEvent,
    PushSubscription,
)
from investos.models.quant import ScenarioAnalysis
from investos.models.watcher import ActiveWatcher
from investos.schemas.portfolio_risk import RiskWatchRequest
from investos.services.portfolio_risk import (
    CONDITION,
    SCENARIO_NAME,
    PortfolioRiskService,
    digest,
)
from investos.services.push_notification import PushNotificationService
from investos.services.watcher import WatcherService


async def clean():
    async with async_session_maker() as session:
        await session.execute(delete(PushNotificationDelivery))
        await session.execute(delete(PushNotificationEvent))
        await session.execute(delete(PushSubscription))
        await session.execute(
            delete(ActiveWatcher).where(ActiveWatcher.condition_type == CONDITION)
        )
        await session.execute(
            delete(ScenarioAnalysis).where(ScenarioAnalysis.name == SCENARIO_NAME)
        )
        await session.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_frozen_assessment_watch_delivery_and_rearm_use_real_stores(monkeypatch):
    await clean()
    monkeypatch.setattr(settings, "WEB_PUSH_ENABLED", True)
    monkeypatch.setattr(
        settings, "WEB_PUSH_VAPID_SUBJECT", "mailto:synthetic@example.test"
    )
    sent = []
    monkeypatch.setattr(
        "investos.services.push_notification.webpush",
        lambda **kwargs: sent.append(json.loads(kwargs["data"])),
    )
    try:
        async with async_session_maker() as session:
            subscription = PushSubscription(
                id=uuid4(),
                endpoint="https://push.example.test/risk",
                endpoint_hash=PushNotificationService.endpoint_hash(
                    "https://push.example.test/risk"
                ),
                p256dh="synthetic",
                auth="synthetic",
                is_active=True,
            )
            session.add(subscription)
            await session.commit()
            service = PortfolioRiskService(session)
            request = RiskWatchRequest(loss_budget=50)
            first = await service.configure(request)
            again = await service.configure(request)
            assert first.watch["id"] == again.watch["id"]
            packet = frozen_packet()
            state = await service.capture(packet)
            assert state.watch["status"] == "triggered"
            assert state.watch["triggered_at"] is not None
            assert state.delivery[0]["status"] == "pending"
            frozen = await service.packet(state.assessment.id)
            assert (
                digest(frozen["packet"])
                == frozen["packet_hash"]
                == state.assessment.packet_hash
            )
            assert state.watch["assessment"]["assessment_id"] == str(
                state.assessment.id
            )
            retry = await service.capture(packet)
            assert retry.assessment.id == state.assessment.id
            assert len(retry.delivery) == 1
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ScenarioAnalysis)
                    .where(ScenarioAnalysis.name == SCENARIO_NAME)
                )
                == 1
            )
            response = await PushNotificationService(session).dispatch_pending()
            assert response["sent"] == 1
            delivered = await service.state()
            assert (
                delivered.delivery[0]["delivered_at"] >= delivered.watch["triggered_at"]
            )
            assert sent[0]["url"] == "/risk"
            assert "SYN-" not in json.dumps(sent) and "50" not in sent[0]["body"]
            assert len(sent) == 1
            rearmed = await service.configure(request)
            assert rearmed.watch["id"] != state.watch["id"]
            state2 = await service.capture(packet)
            assert state2.watch["status"] == "triggered"
            assert len(state2.delivery) == 1
            assert (
                await session.scalar(
                    select(func.count()).select_from(PushNotificationEvent)
                )
                == 2
            )
    finally:
        await clean()


@pytest.mark.asyncio(loop_scope="session")
async def test_missing_data_stays_armed_without_alert_and_cannot_be_overridden_by_model(
    monkeypatch,
):
    await clean()
    try:
        async with async_session_maker() as session:
            service = PortfolioRiskService(session)
            await service.configure(RiskWatchRequest(loss_budget=50))
            packet = frozen_packet()
            packet["snapshots"] = {}
            result = await service.capture(packet)
            assert result.watch["enabled"] is True
            assert result.watch["assessment"]["decision"]["state"] == "unavailable"
            assert result.delivery == []
            with pytest.raises(ValueError, match="configured by the owner"):
                await WatcherService(session).register_watcher(
                    source="chat",
                    condition_type=CONDITION,
                    condition_params={"loss_budget": 1},
                    objective="Synthetic",
                    adjustment_plan="Synthetic",
                )
            disarmed = await service.configure(
                RiskWatchRequest(loss_budget=50, enabled=False)
            )
            assert disarmed.watch["enabled"] is False
    finally:
        await clean()


@pytest.mark.asyncio(loop_scope="session")
async def test_equivalent_refresh_keeps_original_packet_and_updates_heartbeat():
    await clean()
    try:
        async with async_session_maker() as session:
            service = PortfolioRiskService(session)
            packet = frozen_packet()
            first = await service.capture(packet)
            refreshed = copy.deepcopy(packet)
            refreshed["cutoff"] = "2025-05-21T22:01:00+00:00"
            for snapshot in refreshed["snapshots"].values():
                snapshot["retrieved_at"] = "2025-05-21T22:00:30+00:00"
                for bar in snapshot["daily_observations"]:
                    bar["available_at"] = snapshot["retrieved_at"]
            second = await service.capture(refreshed)
            assert first.assessment.id == second.assessment.id
            assert second.last_refresh_at >= first.last_refresh_at
            frozen = await service.packet(second.assessment.id)
            assert frozen["packet"] == packet
            assert frozen["packet_hash"] == digest(packet)
            assert (
                await service.snapshots_at(
                    ["SYN-A"], datetime.fromisoformat(packet["cutoff"])
                )
                == {}
            )
            stored = await service.snapshots_at(
                ["SYN-A", "NOT-IN-CAPTURE"], datetime.now(UTC)
            )
            assert set(stored) == {"SYN-A"}
            assert stored["SYN-A"]["capture_hash"] == frozen["packet_hash"]
            assert stored["SYN-A"]["age_hours"] > 0
    finally:
        await clean()
