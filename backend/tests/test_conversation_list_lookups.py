from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from investos.services.agent import AgentService


async def test_conversation_list_resolves_each_shared_subject_once():
    subject_id = uuid4()
    now = datetime.now(UTC)
    rows = [
        SimpleNamespace(
            id=uuid4(),
            title=f"Question {index}",
            raw_content_ref=None,
            created_at=now - timedelta(minutes=index),
            metadata_json={
                "session_id": str(uuid4()),
                "role": "user",
                "subject_id": str(subject_id),
                "subject_type": "theme",
            },
        )
        for index in range(25)
    ]
    session = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    session.execute = AsyncMock(return_value=result)
    service = AgentService(session)
    service._get_or_create_conversation_source = AsyncMock(
        return_value=SimpleNamespace(id=uuid4())
    )
    service._subject_name = AsyncMock(return_value="Memory cycle")

    listing = await service.list_conversations()

    assert len(listing.conversations) == 25
    assert {row.subject_name for row in listing.conversations} == {"Memory cycle"}
    # Before: one lookup (up to four queries) per conversation.
    assert service._subject_name.await_count == 1
