"""Companies get their SEC industry; the map links companies in the same industry."""

import json
from uuid import uuid4

from investos.config import settings
from investos.models.entity import Entity, Security
from investos.schemas.graph import GraphWebNodeResponse
from investos.services import business_map
from investos.services.graph import GraphService
from investos.services.sec_industry import (
    SecIndustryService,
    company_name_key,
    readable_industry,
    sector_from_office,
)
from tests.test_investor_workflows import workflow_session  # noqa: F401


def test_sec_labels_read_as_words():
    assert (
        readable_industry("SERVICES-PREPACKAGED SOFTWARE")
        == "Services-prepackaged software"
    )
    assert readable_industry("Electronic Computers") == "Electronic Computers"
    assert sector_from_office("06 Technology") == "Technology"
    assert readable_industry("") is None
    assert company_name_key("Apple Inc.") == company_name_key("APPLE INC") == "apple"


async def test_industry_is_filled_once_and_never_overwritten(
    workflow_session, monkeypatch, tmp_path
):
    session = workflow_session
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(business_map, "REQUEST_SPACING_SECONDS", 0)
    tag = uuid4().hex[:5].upper()
    blank = Entity(name=f"Synthetic Blank {tag}", entity_type="company")
    known = Entity(
        name=f"Synthetic Known {tag}", entity_type="company", industry="Kept as is"
    )
    session.add_all([blank, known])
    await session.flush()
    session.add_all(
        [
            Security(
                entity_id=blank.id,
                ticker=f"B{tag}",
                asset_class="equity",
                instrument_type="common_stock",
            ),
            Security(
                entity_id=known.id,
                ticker=f"K{tag}",
                asset_class="equity",
                instrument_type="common_stock",
            ),
        ]
    )
    await session.commit()
    pages = {
        business_map.TICKERS_URL: json.dumps(
            {
                "0": {"ticker": f"B{tag}", "cik_str": 91},
                "1": {"ticker": f"K{tag}", "cik_str": 92},
            }
        ),
        business_map.SUBMISSIONS_URL.format(cik=91): json.dumps(
            {
                "sicDescription": "SEMICONDUCTORS & RELATED DEVICES",
                "ownerOrg": "06 Technology",
            }
        ),
    }
    fetched = []

    async def fake_fetch(url):
        fetched.append(url)
        return pages[url]

    counts = await SecIndustryService(session, fetch=fake_fetch).refresh()
    await session.refresh(blank)
    await session.refresh(known)
    assert blank.industry == "Semiconductors & related devices"
    assert blank.sector == "Technology"
    assert known.industry == "Kept as is"
    assert counts["filled"] >= 1
    # A second run does not fetch the company again.
    fetched.clear()
    await SecIndustryService(session, fetch=fake_fetch).refresh()
    assert business_map.SUBMISSIONS_URL.format(cik=91) not in fetched


async def test_companies_in_the_same_industry_link_to_the_best_connected_one(
    workflow_session,
):
    session = workflow_session
    tag = uuid4().hex[:5].upper()
    companies = [
        Entity(
            name=f"Ind{tag} {n}",
            entity_type="company",
            industry=f"Synthetic industry {tag}",
        )
        for n in "ABC"
    ]
    session.add_all(companies)
    await session.commit()
    nodes = {
        f"entity:{e.id}": GraphWebNodeResponse(
            key=f"entity:{e.id}", id=str(e.id), node_type="entity", label=e.name
        )
        for e in companies
    }
    hub_key = f"entity:{companies[1].id}"
    from investos.schemas.graph import GraphWebEdgeResponse

    edges = {
        uuid4(): GraphWebEdgeResponse(
            id=uuid4(),
            source_key=hub_key,
            target_key="fact:x",
            relationship_type="mentions",
            confidence=1,
        )
    }
    projected = await GraphService(session)._project_same_industry(nodes, edges)
    assert {e.source_key for e in projected} == {hub_key}
    assert {e.target_key for e in projected} == {
        f"entity:{companies[0].id}",
        f"entity:{companies[2].id}",
    }
    assert all(e.shared_industry == f"Synthetic industry {tag}" for e in projected)


async def test_a_company_without_a_ticker_gets_its_industry_by_exact_registered_name(
    workflow_session, monkeypatch, tmp_path
):
    session = workflow_session
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(business_map, "REQUEST_SPACING_SECONDS", 0)
    tag = uuid4().hex[:5].upper()
    named = Entity(name=f"Nameonly{tag} Biotech, Inc.", entity_type="company")
    twin = Entity(name=f"Twin{tag} Corp", entity_type="company")
    session.add_all([named, twin])
    await session.commit()
    pages = {
        business_map.TICKERS_URL: json.dumps(
            {
                "0": {
                    "ticker": "ZZ1",
                    "cik_str": 93,
                    "title": f"NAMEONLY{tag} BIOTECH INC",
                },
                # Two filers share this name: ambiguous, so skipped.
                "1": {"ticker": "ZZ2", "cik_str": 94, "title": f"Twin{tag} Corp"},
                "2": {
                    "ticker": "ZZ3",
                    "cik_str": 95,
                    "title": f"Twin{tag} Corporation",
                },
            }
        ),
        business_map.SUBMISSIONS_URL.format(cik=93): json.dumps(
            {
                "sicDescription": "PHARMACEUTICAL PREPARATIONS",
                "ownerOrg": "03 Life Sciences",
            }
        ),
    }

    async def fake_fetch(url):
        return pages[url]

    await SecIndustryService(session, fetch=fake_fetch).refresh()
    await session.refresh(named)
    await session.refresh(twin)
    assert named.industry == "Pharmaceutical preparations"
    assert named.sector == "Life Sciences"
    assert twin.industry is None
    # No ticker link is created: ticker identity is the owner's decision.
    from sqlalchemy import select as sa_select

    assert (
        await session.scalar(sa_select(Security).where(Security.entity_id == named.id))
    ) is None
