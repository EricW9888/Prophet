"""The Feed puts the owner's companies first without hiding critical news elsewhere."""

from uuid import uuid4

from investos.api.routes.timeline import (
    FOLLOWED_SUBJECT_BONUS,
    IMPORTANCE_SCORE,
    get_timeline,
)
from investos.models.entity import Entity, Security
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.graph import Edge
from investos.models.knowledge import Fact
from investos.models.portfolio import Position
from investos.models.source import Source
from tests.test_investor_workflows import workflow_session  # noqa: F401


def test_a_critical_item_elsewhere_still_outranks_a_medium_one_about_a_holding():
    weight = 0.45  # importance's share of the fact score
    critical_elsewhere = IMPORTANCE_SCORE["critical"] * weight
    medium_followed = IMPORTANCE_SCORE["medium"] * weight + FOLLOWED_SUBJECT_BONUS
    assert critical_elsewhere > medium_followed


async def _fact(session, statement, entity, importance="high"):
    source = Source(name=f"Feed relevance {uuid4().hex}", source_type="web_research")
    session.add(source)
    await session.flush()
    evidence = RawEvidence(
        source_id=source.id, source_item_type="web_research", title=statement
    )
    session.add(evidence)
    await session.flush()
    item = SourceItem(
        raw_evidence_id=evidence.id,
        source_id=source.id,
        extracted_text=statement,
        summary=statement,
        processing_status="processed",
    )
    session.add(item)
    await session.flush()
    fact = Fact(
        statement=statement,
        fact_type="observation",
        source_item_id=item.id,
        tier="hard_fact",
        importance=importance,
        directness="primary",
        novelty="breaking",
        contradiction_role="neutral",
        confidence=0.9,
    )
    session.add(fact)
    await session.flush()
    session.add(
        Edge(
            source_type="fact",
            source_id=fact.id,
            target_type="entity",
            target_id=entity.id,
            relationship_type="mentions",
        )
    )
    return fact


async def test_an_item_about_a_followed_company_outranks_an_equal_one_elsewhere(
    workflow_session,
):
    session = workflow_session
    tag = uuid4().hex[:6].upper()
    held = Entity(name=f"Held{tag} Inc", entity_type="company")
    other = Entity(name=f"Other{tag} Inc", entity_type="company")
    session.add_all([held, other])
    await session.flush()
    security = Security(
        entity_id=held.id,
        ticker=f"H{tag}",
        asset_class="equity",
        instrument_type="common_stock",
    )
    session.add(security)
    await session.flush()
    session.add(
        Position(
            security_id=security.id, direction="long", list_type="holding", quantity=1
        )
    )
    elsewhere = await _fact(session, f"Other{tag} raised guidance.", other)
    about_held = await _fact(session, f"Held{tag} raised guidance.", held)
    await session.commit()

    items = await get_timeline(limit=50, item_type="fact", session=session)
    order = [item.id for item in items]
    assert order.index(about_held.id) < order.index(elsewhere.id)
