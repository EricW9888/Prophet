from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from investos.core.prompting import compact_packet_context
from investos.models.knowledge import Claim, Event, Fact
from investos.models.knowledge_mutation import KnowledgeMutation
from investos.services.graph import GraphService
from investos.services.retrieval import RetrievalService
from investos.services.source_passages import (
    SourcePassageService,
    bind_source_passage,
    project_source_passage,
    text_digest,
)
from investos.workers.extraction import ExtractionWorker
from tests.test_extraction_retention import extraction_payload
from tests.test_investor_workflows import workflow_session
from tests.test_source_relationships import source_fixture


def retained_item(text):
    return SimpleNamespace(id=uuid4(), raw_evidence_id=uuid4(), extracted_text=text)


def test_quote_selector_preserves_unicode_source_and_qualifying_context():
    text = "😀 A prior report said approval was granted.\nCorrection: Helix's license\n  has not been granted. Operations remain conditional."
    quote = "Helix's license has not been granted."
    item = retained_item(text)
    binding = bind_source_passage(
        {"supporting_quote": quote, "quote_occurrence": 0},
        "License pending",
        text,
        source_item=item,
    )
    passage = project_source_passage(binding, "License pending", item)
    assert passage["status"] == "bound"
    assert passage["semantic_verification"] is False
    selector = passage["quote_selector"]
    assert "Correction:" in selector["prefix"]
    assert selector["exact"] == "Helix's license\n  has not been granted."
    positions = passage["position_selector"]
    assert text[positions["start"] : positions["end"]] == selector["exact"]


@pytest.mark.parametrize("quote", [None, "", "invented assertion", "same"])
def test_missing_or_ambiguous_quotes_do_not_gain_a_match(quote):
    text = "same\nCorrection: same"
    binding = bind_source_passage(
        {"supporting_quote": quote},
        "derived",
        text,
        source_item=retained_item(text),
    )
    assert binding["status"] == "unbound"


def test_submitted_window_disambiguates_without_a_model_generated_position():
    text = "same\nCorrection: same"
    item = retained_item(text)
    payload = {"supporting_quote": "same", "quote_occurrence": 1}
    binding = bind_source_passage(payload, "derived", text, source_item=item)
    assert binding["status"] == "unbound"
    payload["quote_occurrence"] = 0
    binding = bind_source_passage(
        payload,
        "derived",
        text,
        source_item=item,
        window={"input_start": 5, "end": len(text)},
    )
    assert binding["start"] == text.rindex("same")
    assert (
        bind_source_passage(
            {"supporting_quote": "Correction:", "quote_occurrence": 0},
            "derived",
            text,
            source_item=item,
            window={"input_start": 0, "end": 4},
        )["status"]
        == "unbound"
    )


def test_changed_assertion_or_source_cannot_inherit_an_old_anchor():
    item = retained_item("Helix denied the report.")
    binding = bind_source_passage(
        {"supporting_quote": item.extracted_text},
        "Helix denied report",
        item.extracted_text,
        source_item=item,
    )
    assert (
        project_source_passage(binding, "Helix confirmed report", item)["status"]
        == "assertion_changed"
    )
    item.extracted_text = "Helix confirmed the report."
    assert (
        project_source_passage(binding, "Helix denied report", item)["status"]
        == "source_changed"
    )
    assert project_source_passage(None, "legacy assertion", item)["status"] == "unknown"


def test_exact_passage_is_not_truncated_with_its_surrounding_context():
    text = "A" * 700 + " " + "qualified observation " * 60 + "Z" * 700
    exact = "qualified observation " * 60
    item = retained_item(text)
    binding = bind_source_passage(
        {"supporting_quote": exact}, "derived", text, source_item=item
    )
    passage = project_source_passage(binding, "derived", item, context_chars=30)
    assert passage["status"] == "bound"
    assert passage["quote_selector"]["exact"].rstrip() == exact.rstrip()
    assert len(passage["quote_selector"]["exact"]) > 1000
    assert len(passage["quote_selector"]["prefix"]) == 30
    assert passage["context_is_excerpt"] is True
    compacted = compact_packet_context(
        {
            "direct_evidence": [
                {
                    "id": str(uuid4()),
                    "type": "fact",
                    "text": "derived",
                    "source_passage": passage,
                }
            ]
        }
    )
    assert compacted["direct_evidence"][0]["source_passage"] == passage


async def test_real_extraction_graph_and_retrieval_retain_source_qualification(
    workflow_session, monkeypatch
):
    session = workflow_session
    subject = f"Helix Delivery {uuid4().hex}"
    text = f"{subject} reported that its license was granted.\n\nCorrection: {subject}'s license has not been granted. Operations cannot start without approval."
    evidence, item = await source_fixture(session, text)
    evidence.is_processed = False
    evidence.raw_content_ref = "synthetic/qualified-source.txt"
    item.processing_status = "extraction_pending"
    payload = extraction_payload(subject)
    payload["events"] = [
        {
            **payload["events"][0],
            "description": "License pending",
            "supporting_quote": f"{subject}'s license has not been granted.",
        }
    ]
    payload["facts"] = [
        {
            **payload["facts"][0],
            "statement": "License pending",
            "supporting_quote": f"{subject}'s license has not been granted.",
        }
    ]
    payload["claims"] = [
        {
            **payload["claims"][0],
            "statement": "Operations await approval",
            "supporting_quote": "Operations cannot start without approval.",
        }
    ]
    payload["fundamental_metrics"] = []
    payload["market_setup_signals"] = []
    monkeypatch.setattr(
        "investos.workers.extraction.call_llm_json",
        AsyncMock(return_value=deepcopy(payload)),
    )
    for target in (
        "CoverageWorker.audit_subject_coverage",
        "SourceLearningService.learn_from_source",
        "OperatingLoopService.refresh_subject",
        "WatcherService.evaluate_new_evidence",
    ):
        monkeypatch.setattr(
            f"investos.workers.extraction.{target}", AsyncMock(return_value={})
        )
    worker = ExtractionWorker(session)
    worker.storage.get_object = AsyncMock(return_value=text.encode())
    await worker.process_evidence(evidence.id)
    fact = (
        await session.scalars(select(Fact).where(Fact.source_item_id == item.id))
    ).one()
    claim = (
        await session.scalars(select(Claim).where(Claim.source_item_id == item.id))
    ).one()
    event_id = await session.scalar(
        select(KnowledgeMutation.node_id).where(
            KnowledgeMutation.node_type == "event",
            KnowledgeMutation.source_id == item.id,
        )
    )
    nodes = await RetrievalService(session)._load_nodes([fact.id, claim.id, event_id])
    assert len(nodes) == 3
    assert all(node["source_passage"]["status"] == "bound" for node in nodes)
    for node in nodes:
        quote = node["source_passage"]["quote_selector"]
        assert "Correction:" in quote["prefix"]
        assert node["source_passage"]["semantic_verification"] is False
    compacted = compact_packet_context({"direct_evidence": nodes})
    assert (
        compacted["direct_evidence"][0]["source_passage"] == nodes[0]["source_passage"]
    )
    detail = await GraphService(session).get_node_detail(
        node_type="fact", node_id=fact.id
    )
    assert detail.source_passage["status"] == "bound"
    assert detail.properties["is_verified"] is False
    assert detail.properties["promotion_eligible"] is False
    assert detail.properties["source_tier"] == fact.tier
    assert "Correction:" in detail.source_passage["quote_selector"]["prefix"]
    assert fact.is_verified is False and fact.promotion_eligible is False
    fact.statement = "The license is approved."
    await session.flush()
    stale = await RetrievalService(session)._load_nodes([fact.id])
    assert stale[0]["source_passage"]["status"] == "assertion_changed"


@pytest.mark.parametrize("original_matches", [True, False])
async def test_legacy_prefix_uses_validated_original_without_rewriting_item(
    workflow_session, monkeypatch, original_matches
):
    session = workflow_session
    original = "Earlier report: approved. Correction: Helix denied approval."
    evidence, item = await source_fixture(session, "Legacy retained prefix.")
    node_id = uuid4()
    binding = bind_source_passage(
        {"supporting_quote": "Helix denied approval."},
        "Approval denied",
        original,
        source_item=item,
    )
    session.add(
        KnowledgeMutation(
            node_type="fact",
            node_id=node_id,
            change_type="created",
            metadata_json={"source_passage": binding},
        )
    )
    await session.flush()
    returned = original if original_matches else original + " altered"
    reader = AsyncMock(return_value=(evidence, returned, text_digest(returned)))
    monkeypatch.setattr(
        "investos.services.translation.TranslationService.original", reader
    )
    nodes = [
        {
            "id": node_id,
            "type": "fact",
            "text": "Approval denied",
            "source": {"source_item_id": str(item.id)},
        }
    ]
    await SourcePassageService(session).attach(nodes)
    assert nodes[0]["source_passage"]["status"] == (
        "bound" if original_matches else "source_changed"
    )
    reader.assert_awaited_once_with(evidence.id)
    assert item.extracted_text == "Legacy retained prefix."


async def test_invalid_source_identity_cannot_break_or_attach_a_passage(
    workflow_session,
):
    session = workflow_session
    evidence, item = await source_fixture(session, "Helix denied approval.")
    nodes = []
    for source_id in ("invalid-uuid", str(item.id)):
        node_id = uuid4()
        binding = bind_source_passage(
            {"supporting_quote": item.extracted_text},
            "Approval denied",
            item.extracted_text,
            source_item=item,
        )
        binding["source_item_id"] = source_id
        session.add(
            KnowledgeMutation(
                node_type="fact",
                node_id=node_id,
                change_type="created",
                metadata_json={"source_passage": binding},
            )
        )
        nodes.append(
            {
                "id": node_id,
                "type": "fact",
                "text": "Approval denied",
                "source": {"source_item_id": str(uuid4())},
            }
        )
    await session.flush()
    await SourcePassageService(session).attach(nodes)
    assert all(node["source_passage"]["status"] == "source_changed" for node in nodes)
