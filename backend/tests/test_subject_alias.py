from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from investos.services.subject_alias import SubjectAliasService, normalize_alias


def test_normalize_alias_keeps_short_acronyms_searchable():
    assert normalize_alias("HBF") == "hbf"
    assert normalize_alias("high-bandwidth flash") == "high bandwidth flash"
    assert normalize_alias(" High   Bandwidth / Flash ") == "high bandwidth flash"


class FakeAliasSession:
    def __init__(self, alias):
        self.alias = alias
        self.committed = False
        self.refreshed = None
        self.deleted = None
        self.added = []
        self.flush_count = 0

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        self.flush_count += 1

    async def get(self, model, alias_id):
        if self.alias and self.alias.id == alias_id:
            return self.alias
        return None

    async def commit(self):
        self.committed = True

    async def refresh(self, item):
        self.refreshed = item

    async def delete(self, item):
        self.deleted = item
        self.alias = None


async def test_approve_alias_marks_user_reviewed():
    alias = SimpleNamespace(
        id=uuid4(),
        alias="HBF",
        normalized_alias="hbf",
        subject_type="portfolio",
        subject_id=uuid4(),
        source="seed",
        confidence=0.72,
        reason=None,
        created_at=datetime(2026, 6, 22, tzinfo=UTC),
        updated_at=datetime(2026, 6, 22, tzinfo=UTC),
    )
    session = FakeAliasSession(alias)

    response = await SubjectAliasService(session).approve_alias(alias.id)

    assert session.committed is True
    assert session.refreshed is alias
    assert alias.source == "user_approved"
    assert alias.confidence == 0.98
    assert alias.reason == "Approved by user review."
    assert response is not None
    assert response.subject_name == "Portfolio"


async def test_delete_alias_removes_record():
    alias = SimpleNamespace(
        id=uuid4(),
        alias="high bandwidth flash",
        normalized_alias="high bandwidth flash",
        subject_type="theme",
        subject_id=uuid4(),
    )
    session = FakeAliasSession(alias)

    deleted = await SubjectAliasService(session).delete_alias(alias.id)

    assert deleted is True
    assert session.deleted is alias
    assert session.committed is True
    assert session.flush_count == 1
    assert session.added[0].change_type == "deleted_alias"
