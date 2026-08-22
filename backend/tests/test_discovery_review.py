from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from investos.services.operating_state import OperatingStateService


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


async def test_approve_discovery_uses_the_injected_session_atomically():
    profile = SimpleNamespace(is_autonomous=True, review_status="pending")
    position = SimpleNamespace(is_autonomous=True, review_status="pending")
    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[_ScalarResult(profile), _ScalarResult(position)]
    )
    session.commit = AsyncMock()

    changed = await OperatingStateService(session).approve_discovery(uuid4(), "entity")

    assert changed is True
    assert profile.is_autonomous is False
    assert profile.review_status == "approved"
    assert position.is_autonomous is False
    assert position.review_status == "approved"
    session.commit.assert_awaited_once()


async def test_dismiss_discovery_records_feedback_and_deletes_watchlist_position():
    profile = SimpleNamespace(review_status="pending")
    position = SimpleNamespace(review_status="pending", list_type="watchlist")
    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[_ScalarResult(profile), _ScalarResult(position)]
    )
    session.delete = AsyncMock()
    session.commit = AsyncMock()

    changed = await OperatingStateService(session).dismiss_discovery(
        uuid4(),
        "entity",
        "This candidate does not fit the mandate.",
    )

    assert changed is True
    assert profile.review_status == "dismissed"
    assert position.review_status == "dismissed"
    session.add.assert_called_once()
    session.delete.assert_awaited_once_with(position)
    session.commit.assert_awaited_once()


@pytest.mark.parametrize("operation", ["approve", "dismiss"])
async def test_discovery_review_rejects_unknown_subject_types(operation: str):
    session = MagicMock()
    service = OperatingStateService(session)

    with pytest.raises(ValueError, match="Only entity discoveries"):
        if operation == "approve":
            await service.approve_discovery(uuid4(), "theme")
        else:
            await service.dismiss_discovery(uuid4(), "theme")
