"""Revenue drivers come from the filer's own tagged facts in its annual report."""

import json
from datetime import date
from uuid import uuid4

import pytest

from investos.config import settings
from investos.models.business_map import BusinessDriver
from investos.models.entity import Entity, Security
from investos.models.portfolio import Position
from investos.services import business_map as module
from investos.services.business_map import (
    BusinessMapService,
    member_label,
    parse_business_map,
    parse_labels,
)
from tests.test_investor_workflows import workflow_session  # noqa: F401


def context(cid, start, end, member=None, axis="srt:ProductOrServiceAxis"):
    segment = (
        f'<xbrli:segment><xbrldi:explicitMember dimension="{axis}">{member}'
        "</xbrldi:explicitMember></xbrli:segment>"
        if member
        else ""
    )
    return (
        f'<xbrli:context id="{cid}"><xbrli:entity>{segment}</xbrli:entity>'
        f"<xbrli:period><xbrli:startDate>{start}</xbrli:startDate>"
        f"<xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>"
    )


def fact(cid, value, name="us-gaap:Revenues", scale="6"):
    return (
        f'<ix:nonFraction name="{name}" contextRef="{cid}" unitRef="usd" '
        f'decimals="-6" scale="{scale}">{value}</ix:nonFraction>'
    )


DOCUMENT = "".join(
    [
        context("fy", "2025-01-01", "2025-12-31"),
        context("prior", "2024-01-01", "2024-12-31"),
        context("q4", "2025-10-01", "2025-12-31"),
        context("hw", "2025-01-01", "2025-12-31", "syn:HardwareMember"),
        context("sw", "2025-01-01", "2025-12-31", "syn:SoftwareMember"),
        context("products", "2025-01-01", "2025-12-31", "us-gaap:ProductMember"),
        context("svc", "2025-01-01", "2025-12-31", "us-gaap:ServiceMember"),
        context(
            "us",
            "2025-01-01",
            "2025-12-31",
            "country:US",
            "srt:StatementGeographicalAxis",
        ),
        context(
            "intl",
            "2025-01-01",
            "2025-12-31",
            "syn:InternationalMember",
            "srt:StatementGeographicalAxis",
        ),
        fact("fy", "1,000"),
        fact("prior", "800"),
        fact("q4", "300"),
        fact("hw", "600"),
        fact("sw", "300"),
        fact("products", "900"),  # a subtotal of hardware and software
        fact(
            "svc",
            "100",
            name="us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        ),
        fact("us", "550"),
        fact("intl", "450"),
    ]
)

LABELS = (
    '<link:loc xlink:type="locator" xlink:href="syn-2025.xsd#syn_HardwareMember" xlink:label="loc_hw"/>'
    '<link:label xlink:type="resource" xlink:label="lab_hw" xlink:role="http://www.xbrl.org/2003/role/label">Devices &amp; Hardware [Member]</link:label>'
    '<link:labelArc xlink:type="arc" xlink:from="loc_hw" xlink:to="lab_hw"/>'
)


def test_the_latest_full_year_is_broken_down_without_double_counting_subtotals():
    mapped = parse_business_map(DOCUMENT, parse_labels(LABELS))

    assert mapped.period_end == date(2025, 12, 31)
    assert mapped.total_revenue == 1_000_000_000
    shares = {(d.kind, d.label): round(d.share, 3) for d in mapped.drivers}
    assert shares == {
        ("product", "Devices & Hardware"): 0.6,
        ("product", "Software"): 0.3,
        ("product", "All services"): 0.1,
        ("region", "US"): 0.55,
        ("region", "International"): 0.45,
    }


def test_labels_fall_back_to_readable_member_names():
    assert member_label("syn:FoodAndSundriesMember") == "Food and sundries"
    assert member_label("syn:NorthAmericaMember") == "North america"
    assert member_label("country:US") == "US"
    assert member_label("country:CN", {"country:CN": "CHINA"}) == "China"
    assert (
        member_label("us-gaap:ProductMember", {"us-gaap:ProductMember": "Product"})
        == "All products"
    )
    assert member_label("syn:XMember", {"syn:XMember": "Cloud & AI"}) == "Cloud & AI"


def test_a_report_without_a_full_year_total_gives_no_map():
    assert (
        parse_business_map(context("q", "2025-10-01", "2025-12-31") + fact("q", "5"))
        is None
    )


async def test_refresh_maps_holdings_and_records_what_it_could_not(
    workflow_session, monkeypatch, tmp_path
):
    session = workflow_session
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(module, "REQUEST_SPACING_SECONDS", 0)
    tag = uuid4().hex[:5].upper()
    mapped_entity = Entity(name=f"Synthetic Maker {tag}", entity_type="company")
    foreign_entity = Entity(name=f"Synthetic Foreign {tag}", entity_type="company")
    session.add_all([mapped_entity, foreign_entity])
    await session.flush()
    for entity, ticker in ((mapped_entity, f"SM{tag}"), (foreign_entity, f"SF{tag}")):
        security = Security(
            entity_id=entity.id,
            ticker=ticker,
            asset_class="equity",
            instrument_type="common_stock",
        )
        session.add(security)
        await session.flush()
        session.add(
            Position(
                security_id=security.id,
                direction="long",
                list_type="holding",
                quantity=1,
                market_value=10,
            )
        )
    await session.flush()

    pages = {
        module.TICKERS_URL: json.dumps({"0": {"ticker": f"SM{tag}", "cik_str": 42}}),
        module.SUBMISSIONS_URL.format(cik=42): json.dumps(
            {
                "filings": {
                    "recent": {
                        "form": ["10-Q", "10-K"],
                        "isInlineXBRL": [1, 1],
                        "accessionNumber": [
                            "0000000042-26-000002",
                            "0000000042-26-000001",
                        ],
                        "primaryDocument": ["q.htm", "annual.htm"],
                    }
                }
            }
        ),
        "https://www.sec.gov/Archives/edgar/data/42/000000004226000001/annual.htm": DOCUMENT,
        "https://www.sec.gov/Archives/edgar/data/42/000000004226000001/index.json": json.dumps(
            {"directory": {"item": [{"name": "syn-2025_lab.xml"}]}}
        ),
        "https://www.sec.gov/Archives/edgar/data/42/000000004226000001/syn-2025_lab.xml": LABELS,
    }

    async def fake_fetch(url):
        return pages[url]

    service = BusinessMapService(session, fetch=fake_fetch)
    outcomes = await service.refresh(limit=50)

    assert outcomes.get("mapped") == 1 and outcomes.get("no_sec_filer") == 1
    latest = await service.latest_map(mapped_entity.id)
    assert latest["period_end"] == "2025-12-31"
    assert latest["filing_url"].endswith("/annual.htm")
    assert {d["label"] for d in latest["drivers"] if d["kind"] == "product"} == {
        "Devices & Hardware",
        "Software",
        "All services",
    }
    assert await service.latest_map(foreign_entity.id) is None
    # Checked companies are not fetched again within the month.
    assert await service.refresh(limit=50) == {}
