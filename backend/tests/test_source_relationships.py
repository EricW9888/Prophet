from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from investos.models.entity import Entity
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.graph import Edge
from investos.models.knowledge import Claim
from investos.models.source import Source
from investos.services.evidence_relevance import (
    EvidenceRelevanceAssessment,
    EvidenceRelevanceService,
)
from investos.services.graph import GraphService
from investos.workers.extraction import EXTRACTION_SCHEMA, ExtractionWorker
from tests.test_investor_workflows import workflow_session


def relationship(person: str, company: str, quote: str) -> dict:
    return {
        "source": {"name": person, "subject_type": "entity", "entity_type": "person"},
        "target": {"name": company, "subject_type": "entity", "entity_type": "company"},
        "relationship_type": "co_founded",
        "supporting_quote": quote,
        "confidence": 0.9,
        "event_time_raw": "1993-04-01",
        "target_horizon": "strategic",
        "valid_until_raw": None,
    }


async def source_fixture(session, quote: str):
    source = Source(name=f"Synthetic filing {uuid4()}", source_type="filing")
    session.add(source)
    await session.flush()
    evidence = RawEvidence(
        source_id=source.id,
        source_item_type="web_research",
        title="Source relationships",
        is_processed=True,
    )
    session.add(evidence)
    await session.flush()
    item = SourceItem(
        raw_evidence_id=evidence.id,
        source_id=source.id,
        extracted_text=quote,
        summary=quote,
        processing_status="processed",
    )
    session.add(item)
    await session.flush()
    return evidence, item


@pytest.mark.asyncio
async def test_relationship_assertions_keep_both_endpoints_provenance_and_source_lifecycle(
    workflow_session,
):
    session = workflow_session
    token = uuid4().hex[:8]
    person, company = f"Casey Vale {token}", f"Helix Labs {token}"
    quote = f"{person} co-founded {company} on April 1, 1993."
    evidence, item = await source_fixture(session, quote)
    worker = ExtractionWorker(session)
    payload = [relationship(person, company, quote)]
    result = await worker.persist_source_relationships(evidence, item, quote, payload)
    assert result["created"] == 1
    assert (await worker.persist_source_relationships(evidence, item, quote, payload))[
        "existing"
    ] == 1
    assertion = (
        await session.scalars(select(Claim).where(Claim.source_item_id == item.id))
    ).one()
    assert (
        assertion.statement == quote
        and not assertion.is_verified
        and not assertion.promotion_eligible
    )
    assert assertion.claim_type == "relationship:co_founded"
    entities = {
        e.name: e
        for e in (
            await session.scalars(
                select(Entity).where(Entity.name.in_([person, company]))
            )
        ).all()
    }
    assert entities[person].entity_type == "person"
    edges = (
        await session.scalars(
            select(Edge).where(
                Edge.source_type == "claim", Edge.source_id == assertion.id
            )
        )
    ).all()
    assert {(e.relationship_type, e.target_id) for e in edges} == {
        ("relationship_subject", entities[person].id),
        ("relationship_object", entities[company].id),
        ("extracted_from", item.id),
    }
    view = await GraphService(session).get_neighborhood(
        node_type="entity", node_id=entities[person].id, depth=2, limit=30
    )
    assert f"entity:{entities[company].id}" in {n.key for n in view.nodes}
    projected = [e for e in view.edges if e.supporting_claim_id == assertion.id]
    assert len(projected) == 1 and projected[0].relationship_type == "co_founded"
    assert projected[0].source_key == f"entity:{entities[person].id}"
    assert projected[0].target_key == f"entity:{entities[company].id}"
    compared = await GraphService(session).compare_nodes(
        node_a_type="entity",
        node_a_id=entities[person].id,
        node_b_type="entity",
        node_b_id=entities[company].id,
    )
    assert any(
        e.supporting_claim_id == assertion.id for e in compared.direct_relationships
    )
    detail = await GraphService(session).get_node_detail(
        node_type="claim", node_id=assertion.id
    )
    assert str(detail.citations[0].raw_evidence_id) == str(evidence.id)
    second_evidence, second_item = await source_fixture(session, quote)
    assert (
        await worker.persist_source_relationships(
            second_evidence, second_item, quote, payload
        )
    )["created"] == 1
    second = (
        await session.scalars(
            select(Claim).where(Claim.source_item_id == second_item.id)
        )
    ).one()
    await EvidenceRelevanceService(session).deprecate_source_derivatives(
        evidence=evidence,
        source_item=item,
        assessment=EvidenceRelevanceAssessment(
            status="irrelevant",
            target_supported=False,
            reason="Synthetic withdrawal",
            supported_subjects=(),
        ),
        reason="Synthetic source withdrawn",
    )
    assert assertion.is_deprecated and not second.is_deprecated
    view = await GraphService(session).get_neighborhood(
        node_type="entity", node_id=entities[person].id, depth=2, limit=30
    )
    keys = {n.key for n in view.nodes}
    assert f"claim:{assertion.id}" not in keys and f"claim:{second.id}" in keys
    assert not any(e.supporting_claim_id == assertion.id for e in view.edges)
    assert any(e.supporting_claim_id == second.id for e in view.edges)
    await session.rollback()


@pytest.mark.asyncio
async def test_unsupported_quotes_endpoints_and_self_links_create_no_relationships(
    workflow_session,
):
    session = workflow_session
    quote = "Casey Vale spoke at an industry conference. Helix Labs also attended."
    evidence, item = await source_fixture(session, quote)
    worker = ExtractionWorker(session)
    before = await session.scalar(select(func.count()).select_from(Entity))
    invented = relationship(
        "Casey Vale", "Helix Labs", "Casey Vale co-founded Helix Labs."
    )
    missing = relationship("Casey Vale", "Absent Company", quote)
    self_link = relationship("Casey Vale", "Casey Vale", quote)
    result = await worker.persist_source_relationships(
        evidence, item, quote, [invented, missing, self_link]
    )
    assert result["created"] == 0 and result["rejected"] == 3
    assert await session.scalar(select(func.count()).select_from(Entity)) == before
    assert not (
        await session.scalars(select(Claim).where(Claim.source_item_id == item.id))
    ).all()
    await session.rollback()


def test_relationship_schema_is_open_ended_and_not_limited_to_company_subjects():
    schema = EXTRACTION_SCHEMA["properties"]["relationships"]["items"]
    assert "enum" not in schema["properties"]["relationship_type"]
    assert "enum" not in schema["properties"]["source"]["properties"]["entity_type"]
    assert "enum" not in EXTRACTION_SCHEMA["properties"]["entity_type"]


@pytest.mark.asyncio
async def test_reindex_preview_is_reversible_and_apply_is_checkpointed(
    workflow_session,
):
    session = workflow_session
    token = uuid4().hex[:8]
    person, company = f"Casey Vale {token}", f"Helix Labs {token}"
    quote = f"{person} co-founded {company} on April 1, 1993."
    evidence, item = await source_fixture(session, quote)
    await session.commit()
    worker = ExtractionWorker(session)
    worker.extract_source_relationships = AsyncMock(
        return_value=[relationship(person, company, quote)]
    )
    result = await worker.reindex_source_relationships(evidence.id)
    assert result["dry_run"] and result["created"] == 1
    assert not (
        await session.scalars(select(Claim).where(Claim.source_item_id == item.id))
    ).all()
    assert not (evidence.metadata_json or {}).get("relationship_extraction")
    applied = await worker.reindex_source_relationships(evidence.id, apply=True)
    assert applied["created"] == 1 and not applied["dry_run"]
    assert (
        evidence.metadata_json["relationship_extraction"]["actor"]
        == "source_relationship_backfill"
    )
    assert (await worker.reindex_source_relationships(evidence.id, apply=True))[
        "model_calls"
    ] == 0
    assert worker.extract_source_relationships.await_count == 2


@pytest.mark.asyncio
async def test_product_kinds_and_existing_subject_identity_are_preserved(
    workflow_session,
):
    session = workflow_session
    token = uuid4().hex[:8]
    product, company = f"Ion Compiler {token}", f"Cedar Tools {token}"
    quote = f"{product} is a software product developed by {company}."
    evidence, item = await source_fixture(session, quote)
    payload = relationship(product, company, quote)
    payload["source"]["entity_type"] = "product"
    payload["relationship_type"] = "developed_by"
    worker = ExtractionWorker(session)
    assert (
        await worker.persist_source_relationships(evidence, item, quote, [payload])
    )["created"] == 1
    entity = (await session.scalars(select(Entity).where(Entity.name == product))).one()
    assert entity.entity_type == "product"
    second_evidence, second_item = await source_fixture(session, quote)
    payload["source"]["entity_type"] = "company"
    assert (
        await worker.persist_source_relationships(
            second_evidence, second_item, quote, [payload]
        )
    )["created"] == 1
    assert (
        entity.entity_type == "product"
    ), "A relation extraction must not silently reclassify an existing identity"
    assert (
        await session.scalar(
            select(func.count()).select_from(Entity).where(Entity.name == product)
        )
        == 1
    )
    await session.rollback()


@pytest.mark.asyncio
async def test_live_extraction_retains_relationships_and_cross_subject_context(
    workflow_session, monkeypatch
):
    token = uuid4().hex[:8]
    person, company = f"Casey Vale {token}", f"Helix Labs {token}"
    quote = f"{person} co-founded {company} on April 1, 1993."
    for target in [
        "investos.workers.extraction.CoverageWorker.audit_subject_coverage",
        "investos.workers.extraction.SourceLearningService.learn_from_source",
    ]:
        monkeypatch.setattr(target, AsyncMock(return_value={}))
    monkeypatch.setattr(
        "investos.workers.extraction.OperatingLoopService.refresh_subject",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(
        "investos.workers.extraction.WatcherService.evaluate_new_evidence",
        AsyncMock(return_value=0),
    )
    session = workflow_session
    evidence, item = await source_fixture(session, quote)
    evidence.is_processed = False
    item.processing_status = "extraction_pending"
    evidence.raw_content_ref = "synthetic/source.txt"
    await session.flush()
    worker = ExtractionWorker(session)
    worker.storage.get_object = AsyncMock(return_value=quote.encode())
    worker._extract_structured_data = AsyncMock(
        return_value={
            "primary_subject": company,
            "subject_type": "entity",
            "entity_type": "company",
            "summary": quote,
            "relevance_assessment": {
                "status": "relevant",
                "target_supported": True,
                "reason": "Synthetic source names both subjects",
                "supported_subjects": [person, company],
            },
            "events": [
                {
                    "subject_name": person,
                    "subject_type": "entity",
                    "entity_type": "person",
                    "relationship_to_primary_subject": "Co-founder of the company",
                    "title": "Company founding",
                    "description": quote,
                    "event_type": "founding",
                    "event_time_raw": "1993-04-01",
                }
            ],
            "facts": [],
            "claims": [],
            "fundamental_metrics": [],
            "market_setup_signals": [],
            "relationships": [relationship(person, company, quote)],
        }
    )
    outcome = await worker.process_evidence(evidence.id)
    assert worker._extract_structured_data.await_count == 1, outcome
    entities = {
        e.name: e
        for e in (
            await session.scalars(
                select(Entity).where(Entity.name.in_([person, company]))
            )
        ).all()
    }
    context_edges = (
        await session.scalars(
            select(Edge).where(
                Edge.target_type == "entity",
                Edge.target_id == entities[company].id,
                Edge.relationship_type == "context_for",
            )
        )
    ).all()
    assert context_edges, "Cross-subject event context was discarded"
    claims = (
        await session.scalars(select(Claim).where(Claim.source_item_id == item.id))
    ).all()
    assert len(claims) == 1 and claims[0].claim_type == "relationship:co_founded"
    # This product path commits; its synthetic records live only in the disposable test DB.
