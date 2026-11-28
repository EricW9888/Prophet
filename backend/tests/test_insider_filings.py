"""Insider trades from SEC Form 4 become plain-language ownership evidence."""

import json
from datetime import UTC, date, datetime
from uuid import uuid4

from sqlalchemy import select

from investos.config import settings
from investos.models.entity import Entity, Security
from investos.models.evidence import RawEvidence
from investos.models.portfolio import Position
from investos.services import business_map
from investos.services.insider_filings import (
    InsiderFilingService,
    combine,
    describe,
    parse_form4,
)
from tests.test_investor_workflows import workflow_session  # noqa: F401

FORM4 = """<?xml version="1.0"?>
<ownershipDocument>
  <reportingOwner><reportingOwnerId><rptOwnerName>Doe Jane</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>0</isDirector><isOfficer>1</isOfficer>
    <officerTitle>Chief Executive Officer</officerTitle></reportingOwnerRelationship></reportingOwner>
  <aff10b5One>1</aff10b5One>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-11-12</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>1200</value></transactionShares>
        <transactionPricePerShare><value>101.5</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>50000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-11-13</value></transactionDate>
      <transactionCoding><transactionCode>A</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>300</value></transactionShares>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
  <derivativeTable><derivativeTransaction><transactionDate><value>2026-11-12</value></transactionDate></derivativeTransaction></derivativeTable>
</ownershipDocument>"""


def test_common_stock_trades_are_read_with_the_insiders_role():
    sale, grant = parse_form4(FORM4)

    assert (sale.owner, sale.role, sale.code, sale.shares, sale.price) == (
        "Doe Jane",
        "Chief Executive Officer",
        "S",
        1200,
        101.5,
    )
    assert sale.owned_after == 50000 and sale.under_plan and not sale.acquired
    assert grant.code == "A" and grant.price is None and grant.acquired


def test_each_trade_reads_as_a_sentence():
    sale, grant = parse_form4(FORM4)

    title, summary = describe(sale, "SYNQ", date(2026, 11, 14))
    assert (
        title == "Doe Jane (Chief Executive Officer) sold 1,200 SYNQ shares at $101.50"
    )
    assert "Value about $121,800." in summary
    assert "Holds 50,000 shares afterwards." in summary
    assert "pre-arranged trading plan" in summary
    assert describe(grant, "SYNQ", date(2026, 11, 14))[0].endswith(
        "received 300 SYNQ shares as a grant or award"
    )


async def test_followed_companies_get_new_filings_once(
    workflow_session, monkeypatch, tmp_path
):
    session = workflow_session
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(business_map, "REQUEST_SPACING_SECONDS", 0)
    tag = uuid4().hex[:5].upper()
    entity = Entity(name=f"Synthetic Watch {tag}", entity_type="company")
    session.add(entity)
    await session.flush()
    security = Security(
        entity_id=entity.id,
        ticker=f"SW{tag}",
        asset_class="equity",
        instrument_type="common_stock",
    )
    session.add(security)
    await session.flush()
    session.add(
        Position(
            security_id=security.id, direction="long", list_type="watchlist", quantity=0
        )
    )
    await session.flush()

    filings = {
        "form": ["4", "10-Q", "4"],
        "filingDate": ["2026-11-14", "2026-11-01", "2026-05-01"],
        "accessionNumber": [
            "0000000077-26-000009",
            "0000000077-26-000008",
            "0000000077-26-000001",
        ],
        "primaryDocument": ["xslF345X06/form4.xml", "q.htm", "xslF345X06/form4.xml"],
    }
    pages = {
        business_map.TICKERS_URL: json.dumps(
            {"0": {"ticker": f"SW{tag}", "cik_str": 77}}
        ),
        business_map.SUBMISSIONS_URL.format(cik=77): json.dumps(
            {"filings": {"recent": filings}}
        ),
        "https://www.sec.gov/Archives/edgar/data/77/000000007726000009/form4.xml": FORM4,
    }

    async def fake_fetch(url):
        return pages[url]

    now = datetime(2026, 11, 20, tzinfo=UTC)
    service = InsiderFilingService(session, fetch=fake_fetch)
    counts = await service.refresh(now=now)

    # The May filing is older than the first 90-day look-back.
    assert counts == {"companies": 1, "filings": 1, "transactions": 2}
    rows = (
        await session.scalars(
            select(RawEvidence).where(
                RawEvidence.source_item_type == "insider_disclosure",
                RawEvidence.title.like(f"%SW{tag}%"),
            )
        )
    ).all()
    assert len(rows) == 2
    assert {row.metadata_json["transaction_code"] for row in rows} == {"S", "A"}

    assert await service.refresh(now=now) == {
        "companies": 1,
        "filings": 0,
        "transactions": 0,
    }

    # Later the same day the company files another Form 4: it is read once.
    filings["form"].insert(0, "4")
    filings["filingDate"].insert(0, "2026-11-14")
    filings["accessionNumber"].insert(0, "0000000077-26-000010")
    filings["primaryDocument"].insert(0, "xslF345X06/form4.xml")
    pages[business_map.SUBMISSIONS_URL.format(cik=77)] = json.dumps(
        {"filings": {"recent": filings}}
    )
    pages["https://www.sec.gov/Archives/edgar/data/77/000000007726000010/form4.xml"] = (
        FORM4
    )
    assert (await service.refresh(now=now))["filings"] == 1
    assert (await service.refresh(now=now))["filings"] == 0


def test_one_decision_filed_as_many_rows_reads_as_one_trade():
    rows = "".join(
        f"""<nonDerivativeTransaction>
      <transactionDate><value>2026-11-{day}</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>{shares}</value></transactionShares>
        <transactionPricePerShare><value>{price}</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>{after}</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
    </nonDerivativeTransaction>"""
        for day, shares, price, after in [
            (12, 1000, 100.0, 9000),
            (12, 3000, 102.0, 6000),
            (13, 1000, 106.0, 5000),
        ]
    )
    xml = (
        "<ownershipDocument><reportingOwner><reportingOwnerId><rptOwnerName>Doe Jane"
        "</rptOwnerName></reportingOwnerId><reportingOwnerRelationship><isDirector>1"
        "</isDirector></reportingOwnerRelationship></reportingOwner>"
        f"<nonDerivativeTable>{rows}</nonDerivativeTable></ownershipDocument>"
    )
    (sale,) = combine(parse_form4(xml))
    assert (sale.shares, sale.price, sale.trades) == (5000, 102.4, 3)
    assert (sale.transaction_date, sale.last_date) == (
        date(2026, 11, 12),
        date(2026, 11, 13),
    )
    assert sale.owned_after == 5000

    title, summary = describe(sale, "SYNQ", date(2026, 11, 14))
    assert title == "Doe Jane (director) sold 5,000 SYNQ shares at an average $102.40"
    assert "between Nov 12 and Nov 13, 2026" in summary
    assert "Filed as 3 separate transactions." in summary


def test_only_open_market_trades_are_timing_signals():
    from types import SimpleNamespace

    from investos.services.ownership_signals import OwnershipSignalService

    def evidence(code):
        return SimpleNamespace(
            source_item_type="insider_disclosure",
            metadata_json={"ticker": "SYNQ", "transaction_code": code},
            title="Synthetic insider trade",
        )

    source = SimpleNamespace(source_type="filing")
    for code in ("A", "F", "G", "M"):
        assert OwnershipSignalService.analyze_signal(evidence(code), source) is None
    for code in ("P", "S"):
        assert OwnershipSignalService.analyze_signal(evidence(code), source) is not None
