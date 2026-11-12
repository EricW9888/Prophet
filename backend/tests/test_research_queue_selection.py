"""Real queue ordering must not hide actionable research behind data holds."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from investos.models.review import ReviewQueueItem
from investos.services.automation import AutomationCoordinator
from investos.services.review import ReviewService
from tests.test_investor_workflows import workflow_session


async def test_research_selection_skips_data_holds_and_scans_past_ineligible_investment_page(
    workflow_session, monkeypatch
):
    session = workflow_session
    stamp = datetime(2025, 1, 1, tzinfo=UTC)
    holds = [
        ReviewQueueItem(
            item_type="mailbox_identity",
            item_id=uuid4(),
            review_domain="portfolio_data",
            queue_owner="retained",
            priority_score=100,
            status="pending",
            trigger_reason="Synthetic identity hold",
            created_at=stamp,
        )
        for _ in range(35)
    ]
    earlier = [
        ReviewQueueItem(
            item_type="source",
            item_id=uuid4(),
            review_domain="investment",
            queue_owner="retained",
            priority_score=90,
            status="pending",
            trigger_reason="Synthetic review without an actionable question",
            created_at=stamp,
        )
        for _ in range(35)
    ]
    target = ReviewQueueItem(
        item_type="position",
        item_id=uuid4(),
        review_domain="investment",
        queue_owner="retained",
        priority_score=80,
        status="pending",
        trigger_reason="Synthetic research-ready position",
        created_at=stamp,
    )
    session.add_all(holds + earlier + [target])
    await session.flush()
    coordinator = AutomationCoordinator()
    monkeypatch.setattr(coordinator, "_due_question", AsyncMock(return_value=None))
    monkeypatch.setattr(ReviewService, "refresh_queue", AsyncMock())
    question = SimpleNamespace(id=uuid4())
    visited = []

    async def qualify(_session, item):
        visited.append(item.id)
        return question if item.id == target.id else None

    monkeypatch.setattr(coordinator, "_question_for_review_item", qualify)
    monkeypatch.setattr(
        coordinator,
        "_is_portfolio_relevant_question_subject",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        coordinator,
        "_question_subject_context",
        AsyncMock(
            return_value={"subject_type": "position", "subject_id": target.item_id}
        ),
    )
    selected, review, context = await coordinator._select_research_question(session)
    assert selected is question and review.id == target.id
    assert context["subject_id"] == target.item_id
    assert not set(visited) & {item.id for item in holds}
    assert {item.id for item in earlier} <= set(visited)
    assert all(item.status == "pending" for item in holds + earlier)
