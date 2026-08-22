from datetime import UTC, datetime
from uuid import uuid4

from investos.api.routes.timeline import _mutation_tombstone_payload
from investos.models.knowledge_mutation import KnowledgeMutation
from investos.services.knowledge_audit import KnowledgeAuditService
from investos.services.pruning import PruningService


class FakeSession:
    def __init__(self):
        self.added = []
        self.flush_count = 0

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        self.flush_count += 1


class FakeResult:
    def scalar_one_or_none(self):
        return None


class FakeRestoreSession(FakeSession):
    def __init__(self, node):
        super().__init__()
        self.node = node
        self.committed = False

    async def get(self, model, node_id):
        if getattr(self.node, "id", None) == node_id:
            return self.node
        return None

    async def execute(self, stmt):
        return FakeResult()

    async def commit(self):
        self.committed = True


async def test_knowledge_audit_records_append_only_mutation_event():
    session = FakeSession()
    node_id = uuid4()
    source_id = uuid4()
    subject_id = uuid4()

    event = await KnowledgeAuditService(session).record_change(
        node_type="fact",
        node_id=node_id,
        change_type="created",
        reason="Extracted from source evidence: HBM demand revision",
        actor="extraction_worker",
        source_type="source_item",
        source_id=source_id,
        subject_type="entity",
        subject_id=subject_id,
        metadata={"raw_evidence_id": str(uuid4())},
    )

    assert isinstance(event, KnowledgeMutation)
    assert session.added == [event]
    assert session.flush_count == 1
    assert event.node_type == "fact"
    assert event.node_id == node_id
    assert event.change_type == "created"
    assert event.actor == "extraction_worker"
    assert event.source_type == "source_item"
    assert event.source_id == source_id
    assert event.subject_type == "entity"
    assert event.subject_id == subject_id
    assert event.metadata_json and "raw_evidence_id" in event.metadata_json


def test_mutation_tombstone_payload_keeps_deleted_graph_events_visible():
    source_id = uuid4()
    target_id = uuid4()
    mutation = KnowledgeMutation(
        node_id=uuid4(),
        node_type="edge",
        change_type="deleted_orphan",
        reason="Integrity repair removed an edge whose target was missing.",
        actor="integrity_repair",
        subject_type="entity",
        subject_id=source_id,
        metadata_json={
            "source_type": "entity",
            "source_id": str(source_id),
            "target_type": "fact",
            "target_id": str(target_id),
            "relationship_type": "mentions",
            "confidence": 0.8,
        },
        created_at=datetime(2026, 6, 20, 12, 0, tzinfo=UTC),
    )

    payload = _mutation_tombstone_payload(mutation)

    assert payload.change_source == "audit_event"
    assert payload.node_type == "edge"
    assert payload.change_type == "deleted_orphan"
    assert payload.is_deprecated is True
    assert "entity:" in payload.text
    assert "mentions" in payload.text
    assert (
        payload.reason == "Integrity repair removed an edge whose target was missing."
    )
    assert payload.metadata and payload.metadata["relationship_type"] == "mentions"


async def test_pruning_restore_reactivates_node_and_records_audit_event():
    node_id = uuid4()

    class Node:
        def mark_updated(self):
            self.updated = True

    node = Node()
    node.id = node_id
    node.statement = "Restored NAND capacity fact."
    node.is_deprecated = True
    node.deprecated_reason = "duplicate: overly aggressive cleanup"
    session = FakeRestoreSession(node)
    result = await PruningService(session).restore_knowledge_node(
        "fact",
        node_id,
        reason="Useful context for MEMA/MEMB linkage.",
    )

    assert result["restored"] is True
    assert node.is_deprecated is False
    assert node.deprecated_reason is None
    assert getattr(node, "updated", False) is True
    assert session.committed is True
    assert session.flush_count == 2
    assert isinstance(session.added[0], KnowledgeMutation)
    assert session.added[0].change_type == "restored"
    assert session.added[0].metadata_json["previous_deprecated_reason"] == (
        "duplicate: overly aggressive cleanup"
    )
    assert session.added[0].metadata_json["label"] == "Restored NAND capacity fact."
