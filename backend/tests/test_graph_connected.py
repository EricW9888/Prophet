"""The knowledge map finds any subject and connects subjects through shared evidence."""

from uuid import uuid4

from investos.models.entity import Entity, Security
from investos.models.graph import Edge
from investos.schemas.graph import GraphWebNodeResponse
from investos.services.graph import GraphService
from tests.test_investor_workflows import workflow_session  # noqa: F401


async def _company(session, name, ticker=None):
    entity = Entity(name=name, entity_type="company")
    session.add(entity)
    await session.flush()
    if ticker:
        session.add(
            Security(
                entity_id=entity.id,
                ticker=ticker,
                asset_class="equity",
                instrument_type="common_stock",
            )
        )
        await session.flush()
    return entity


async def test_a_company_is_found_by_ticker_and_before_longer_namesakes(
    workflow_session,
):
    session = workflow_session
    tag = uuid4().hex[:6].upper()
    # The company is older; a newer, longer namesake must not outrank it.
    company = await _company(session, f"Zq{tag}, Inc.", ticker=f"Z{tag}")
    longer = await _company(session, f"Zq{tag} Energy Storage Business")
    await session.commit()
    service = GraphService(session)

    by_name = await service.search_nodes(query=f"zq{tag}", limit=5)
    assert [r.node_id for r in by_name[:2]] == [company.id, longer.id]
    by_ticker = await service.search_nodes(query=f"z{tag}", limit=5)
    assert by_ticker[0].node_id == company.id


def _node(entity):
    return GraphWebNodeResponse(
        key=f"entity:{entity.id}",
        id=str(entity.id),
        node_type="entity",
        label=entity.name,
        layer="knowledge",
    )


async def test_companies_named_together_in_evidence_are_linked_with_a_count(
    workflow_session, monkeypatch
):
    import investos.services.graph as module

    session = workflow_session
    tag = uuid4().hex[:6].upper()
    a, b, c = [await _company(session, f"Link{tag} {n}") for n in "ABC"]
    # Two facts name A and B; one names B and C.
    for members in ([a, b], [a, b], [b, c]):
        fact_id = uuid4()
        for entity in members:
            session.add(
                Edge(
                    source_type="fact",
                    source_id=fact_id,
                    target_type="entity",
                    target_id=entity.id,
                    relationship_type="mentions",
                )
            )
    await session.commit()
    nodes = {f"entity:{e.id}": _node(e) for e in (a, b, c)}
    edges = {}
    projected = await GraphService(session)._project_shared_evidence(nodes, edges)
    counts = {
        frozenset((e.source_key, e.target_key)): e.shared_evidence_count
        for e in projected
    }
    assert counts == {
        frozenset((f"entity:{a.id}", f"entity:{b.id}")): 2,
        frozenset((f"entity:{b.id}", f"entity:{c.id}")): 1,
    }
    assert all(e.relationship_type == "mentioned_together" for e in projected)

    # Each company keeps only its strongest links.
    monkeypatch.setattr(module, "SHARED_EVIDENCE_LINKS_PER_SUBJECT", 1)
    capped = await GraphService(session)._project_shared_evidence(nodes, {})
    assert [e.shared_evidence_count for e in capped] == [2]


async def test_a_company_name_without_its_legal_suffix_is_an_exact_match(
    workflow_session,
):
    session = workflow_session
    tag = uuid4().hex[:6].upper()
    company = await _company(session, f"Zr{tag}, Inc.")
    shorter = await _company(session, f"Zr{tag} AB")
    await session.commit()
    results = await GraphService(session).search_nodes(query=f"zr{tag}", limit=5)
    assert [r.node_id for r in results[:2]] == [company.id, shorter.id]
