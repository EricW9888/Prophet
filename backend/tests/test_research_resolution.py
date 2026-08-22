from types import SimpleNamespace
from uuid import uuid4

import pytest

from investos.models.coverage import Resolution
from investos.services.research import ResearchService


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _ResolutionSession:
    def __init__(self, source_item, existing_resolution=None):
        self.results = [source_item, existing_resolution]
        self.added = []
        self.commits = 0

    async def execute(self, _statement):
        return _ScalarResult(self.results.pop(0))

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_processed_source_can_close_research_question(monkeypatch):
    evidence_id = uuid4()
    question = SimpleNamespace(
        id=uuid4(),
        question_text="What is Memory Beta's HBM supply outlook?",
        status="investigating",
    )
    source_item = SimpleNamespace(
        summary="Memory Beta said HBM output is sold out through calendar 2027.",
        extracted_text=(
            "Management stated that committed customer demand covers planned HBM "
            "capacity through 2027, while execution and yield remain risks."
        ),
    )
    session = _ResolutionSession(source_item)

    async def answer_question(**_kwargs):
        return {
            "answered": True,
            "summary": "Memory Beta says planned HBM capacity is committed through 2027.",
            "remaining_uncertainty": "Yield and execution can still change realized supply.",
        }

    monkeypatch.setattr("investos.services.research.call_llm_json", answer_question)
    monkeypatch.setattr(
        ResearchService,
        "_log_research_action",
        staticmethod(lambda **_kwargs: None),
    )

    resolved = await ResearchService(session)._maybe_resolve_question(
        question,
        evidence_id,
    )

    assert resolved is True
    assert question.status == "answered"
    assert session.commits == 1
    assert len(session.added) == 1
    resolution = session.added[0]
    assert isinstance(resolution, Resolution)
    assert resolution.resolving_evidence_ids == [evidence_id]
    assert "Remaining uncertainty" in resolution.summary


@pytest.mark.asyncio
async def test_related_source_keeps_question_investigating(monkeypatch):
    evidence_id = uuid4()
    question = SimpleNamespace(
        id=uuid4(),
        question_text="What revenue is committed under the Anthropic agreement?",
        status="investigating",
    )
    source_item = SimpleNamespace(
        summary="Memory Beta and Anthropic announced a strategic agreement.",
        extracted_text=(
            "The parties will collaborate on memory and storage architecture. "
            "The announcement did not disclose pricing, capacity, or revenue terms."
        ),
    )
    session = _ResolutionSession(source_item)

    async def leave_open(**_kwargs):
        return {
            "answered": False,
            "summary": "The agreement is confirmed but no committed revenue is disclosed.",
            "remaining_uncertainty": "Commercial terms remain unknown.",
        }

    monkeypatch.setattr("investos.services.research.call_llm_json", leave_open)
    monkeypatch.setattr(
        ResearchService,
        "_log_research_action",
        staticmethod(lambda **_kwargs: None),
    )

    resolved = await ResearchService(session)._maybe_resolve_question(
        question,
        evidence_id,
    )

    assert resolved is False
    assert question.status == "investigating"
    assert session.added == []
    assert session.commits == 0
