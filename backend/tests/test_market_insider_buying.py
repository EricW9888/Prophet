"""Open-market insider buying anywhere in the market reaches Ideas, without crowding out Review."""

import json
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select

from investos.config import settings
from investos.models.entity import Entity, Security
from investos.models.evidence import RawEvidence
from investos.models.market_setup import MarketSetupSignal
from investos.models.opportunity import OpportunityUniverseMember
from investos.models.portfolio import Position
from investos.models.review import ReviewQueueItem
from investos.schemas.opportunity import OpportunityUniverseMemberUpdate
from investos.services import business_map
from investos.services.market_insider_buying import (
    IssuerBuying,
    MarketInsiderBuyingService,
    form4_filings,
    priority_for,
    reason_for,
)
from investos.services.opportunity import OpportunityDiscoveryService
from investos.services.ownership_signals import OwnershipSignalService
from tests.test_investor_workflows import workflow_session  # noqa: F401


def filing(ticker: str, owner: str, code: str, shares: int, price: float, day: str):
    return f"""<SEC-DOCUMENT>
<XML>
<?xml version="1.0"?>
<ownershipDocument>
  <issuer><issuerCik>0000000088</issuerCik><issuerName>Synthetic Buyers Inc</issuerName>
    <issuerTradingSymbol>{ticker}</issuerTradingSymbol></issuer>
  <reportingOwner><reportingOwnerId><rptOwnerName>{owner}</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>1</isDirector></reportingOwnerRelationship></reportingOwner>
  <nonDerivativeTable><nonDerivativeTransaction>
    <transactionDate><value>{day}</value></transactionDate>
    <transactionCoding><transactionCode>{code}</transactionCode></transactionCoding>
    <transactionAmounts><transactionShares><value>{shares}</value></transactionShares>
      <transactionPricePerShare><value>{price}</value></transactionPricePerShare>
      <transactionAcquiredDisposedCode><value>{"A" if code == "P" else "D"}</value></transactionAcquiredDisposedCode></transactionAmounts>
    <postTransactionAmounts><sharesOwnedFollowingTransaction><value>90000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
  </nonDerivativeTransaction></nonDerivativeTable>
</ownershipDocument>
</XML>
</SEC-DOCUMENT>"""


def index_file(paths: list[str]) -> str:
    header = (
        "Form Type   Company Name   CIK   Date Filed  File Name\n" + "-" * 40 + "\n"
    )
    rows = [f"4   Someone   88   20261124   {path}" for path in paths]
    # Each filing is listed once per party, and amendments are a different form.
    rows.append(f"4   Synthetic Buyers Inc   88   20261124   {paths[0]}")
    rows.append(
        "4/A   Someone   88   20261124   edgar/data/88/0000000088-26-000999.txt"
    )
    return header + "\n".join(rows) + "\n"


def test_each_form4_is_read_once_and_amendments_are_skipped():
    index = index_file(
        [
            "edgar/data/88/0000000088-26-000001.txt",
            "edgar/data/88/0000000088-26-000002.txt",
        ]
    )
    assert form4_filings(index) == [
        ("0000000088-26-000001", "edgar/data/88/0000000088-26-000001.txt"),
        ("0000000088-26-000002", "edgar/data/88/0000000088-26-000002.txt"),
    ]


def test_more_insiders_and_more_money_rank_higher_and_read_plainly():
    one = IssuerBuying(
        "SYNB", "Synthetic", {"A"}, 150_000, date(2026, 11, 24), date(2026, 11, 24)
    )
    three = IssuerBuying(
        "SYNB",
        "Synthetic",
        {"A", "B", "C"},
        4_200_000,
        date(2026, 11, 3),
        date(2026, 11, 24),
    )
    assert 0.6 <= priority_for(one) < priority_for(three) <= 0.95
    assert reason_for(one) == (
        "Insider buying: 1 insider bought $150K in the open market (Nov 24)"
    )
    assert reason_for(three) == (
        "Insider buying: 3 insiders bought $4.2M in the open market (Nov 03 to Nov 24)"
    )


async def test_open_market_buys_are_recorded_and_put_first_in_line_for_ideas(
    workflow_session, monkeypatch, tmp_path
):
    session = workflow_session
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(business_map, "REQUEST_SPACING_SECONDS", 0)
    tag = uuid4().hex[:5].upper()
    ticker = f"B{tag}"
    base = "https://www.sec.gov/Archives/"
    daily = "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/"
    listing = {
        "directory": {
            "item": [
                {"name": "form.20261119.idx"},
                {"name": "form.20261124.idx"},
                {"name": "form.20261125.idx"},
                {"name": "master.20261125.idx"},
            ]
        }
    }
    day1 = [f"edgar/data/88/0000088{tag}-26-00000{n}.txt" for n in (1, 2, 3)]
    day2 = [f"edgar/data/88/0000088{tag}-26-00000{n}.txt" for n in (4,)]
    pages = {
        daily + "index.json": json.dumps(listing),
        daily + "form.20261124.idx": index_file(day1),
        daily + "form.20261125.idx": index_file(day2),
        # A director buys $250,000; a small buy and a sale are not discovery signals.
        base + day1[0]: filing(ticker, "Roe Alex", "P", 5000, 50.0, "2026-11-24"),
        base + day1[1]: filing(ticker, "Poe Sam", "P", 100, 50.0, "2026-11-24"),
        base + day1[2]: filing(ticker, "Moe Kim", "S", 9000, 50.0, "2026-11-24"),
        # A second insider buys the next day.
        base + day2[0]: filing(ticker, "Doe Lee", "P", 3000, 51.0, "2026-11-25"),
    }
    fetched: list[str] = []

    async def fake_fetch(url):
        fetched.append(url)
        if url.endswith("form.20261119.idx"):
            raise AssertionError("a first run looks back one week, not further")
        return pages[url]

    # A first run reads the published days of the past week.
    service = MarketInsiderBuyingService(session, fetch=fake_fetch)
    counts = await service.refresh(today=date(2026, 11, 26))
    assert counts == {
        "days": 2,
        "filings": 4,
        "buys": 2,
        "companies": 1,
        "unread": 0,
        "given_up": 0,
    }

    rows = (
        await session.scalars(
            select(RawEvidence).where(
                RawEvidence.metadata_json["ticker"].astext == ticker
            )
        )
    ).all()
    assert sorted(row.metadata_json["owner"] for row in rows) == ["Doe Lee", "Roe Alex"]

    security = await session.scalar(select(Security).where(Security.ticker == ticker))
    member = await session.scalar(
        select(OpportunityUniverseMember).where(
            OpportunityUniverseMember.security_id == security.id
        )
    )
    assert member.enabled and member.source == "insider_buying"
    assert member.priority > OpportunityDiscoveryService.DEFAULT_UNIVERSE_PRIORITY
    assert member.next_inspection_at <= datetime.now(UTC)
    (origin,) = [
        o
        for o in member.metadata_json["origins"]
        if o["source_type"] == "insider_buying"
    ]
    assert origin["label"] == (
        "Insider buying: 2 insiders bought $403K in the open market (Nov 24 to Nov 25)"
    )

    # Nothing new is published: a second run reads only the listing.
    fetched.clear()
    assert await service.refresh(today=date(2026, 11, 26)) == {
        "days": 0,
        "filings": 0,
        "buys": 0,
        "companies": 0,
        "unread": 0,
        "given_up": 0,
    }
    assert fetched == [daily + "index.json"]


async def _member(session, tag: str, *, enabled: bool = True):
    entity = Entity(name=f"Synthetic Signal {tag}", entity_type="company")
    session.add(entity)
    await session.flush()
    security = Security(
        entity_id=entity.id,
        ticker=f"SG{tag}",
        asset_class="equity",
        instrument_type="common_stock",
    )
    session.add(security)
    await session.flush()
    member = OpportunityUniverseMember(
        security_id=security.id,
        entity_id=entity.id,
        enabled=enabled,
        priority=0.5,
        source="us_exchange_directory",
        metadata_json={},
        next_inspection_at=datetime.now(UTC) + timedelta(days=1),
    )
    session.add(member)
    await session.commit()
    return security, member


async def test_a_signal_raises_priority_until_inspected_or_the_owner_decides(
    workflow_session,
):
    session = workflow_session
    service = OpportunityDiscoveryService(session)
    tag = uuid4().hex[:5].upper()
    security, member = await _member(session, tag)

    async def signal(priority: float):
        return await service.note_universe_signal(
            ticker=security.ticker,
            entity_name=None,
            source_type="insider_buying",
            source_id=security.ticker,
            label="Insider buying: 1 insider bought $150K in the open market (Nov 24)",
            priority=priority,
        )

    await signal(0.8)
    await session.refresh(member)
    assert member.priority == 0.8 and member.next_inspection_at <= datetime.now(UTC)
    assert member.metadata_json["signal_boost"]["base_priority"] == 0.5

    # A later, stronger signal keeps the original base.
    await signal(0.9)
    await session.refresh(member)
    assert member.priority == 0.9
    assert member.metadata_json["signal_boost"]["base_priority"] == 0.5

    # Inspecting the company uses the raise up.
    service._end_signal_boost(member)
    assert member.priority == 0.5 and "signal_boost" not in member.metadata_json

    # The owner's priority replaces a raise.
    await signal(0.8)
    await service.update_universe_member(
        member.id, OpportunityUniverseMemberUpdate(priority=0.3)
    )
    await session.refresh(member)
    assert member.priority == 0.3 and "signal_boost" not in member.metadata_json


async def test_a_signal_never_reenables_what_the_owner_turned_off(workflow_session):
    session = workflow_session
    tag = uuid4().hex[:5].upper()
    security, member = await _member(session, tag, enabled=False)
    later = member.next_inspection_at

    await OpportunityDiscoveryService(session).note_universe_signal(
        ticker=security.ticker,
        entity_name=None,
        source_type="insider_buying",
        source_id=security.ticker,
        label="Insider buying: 1 insider bought $150K in the open market (Nov 24)",
        priority=0.9,
    )
    await session.refresh(member)
    assert not member.enabled and member.priority == 0.5
    assert member.next_inspection_at == later
    assert "signal_boost" not in member.metadata_json
    # The signal is still kept as provenance.
    assert any(
        origin["source_type"] == "insider_buying"
        for origin in member.metadata_json["origins"]
    )


async def test_market_wide_disclosures_cannot_crowd_out_a_followed_company(
    workflow_session,
):
    session = workflow_session
    tag = uuid4().hex[:5].upper()
    entity = Entity(name=f"Synthetic Followed {tag}", entity_type="company")
    session.add(entity)
    await session.flush()
    security = Security(
        entity_id=entity.id,
        ticker=f"FW{tag}",
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
    await session.commit()

    signals = OwnershipSignalService(session)
    old = datetime(2026, 11, 2, tzinfo=UTC)
    followed = await signals.ingest_disclosure(
        source_name=f"Synthetic insider feed {tag}",
        source_type="filing",
        source_item_type="insider_disclosure",
        title=f"Director sold FW{tag} shares",
        external_id=f"followed-{tag}",
        metadata={
            "ticker": f"FW{tag}",
            "transaction_type": "sold",
            "filing_date": old.date().isoformat(),
        },
    )
    for n in range(3):
        await signals.ingest_disclosure(
            source_name=f"Synthetic insider feed {tag}",
            source_type="filing",
            source_item_type="insider_disclosure",
            title=f"Director bought MW{tag}{n} shares",
            external_id=f"market-{tag}-{n}",
            metadata={
                "ticker": f"MW{tag}{n}",
                "transaction_type": "bought",
                "filing_date": "2099-01-0{}".format(n + 1),
            },
        )

    await signals.queue_review_items(limit=2)
    queued = await session.scalar(
        select(ReviewQueueItem).where(ReviewQueueItem.item_id == followed.id)
    )
    assert queued is not None


def test_a_buy_filed_in_small_rows_counts_as_one_purchase():
    from investos.services.insider_filings import combine, parse_form4
    from investos.services.market_insider_buying import is_notable_buy

    rows = "".join(
        filing("SYNB", "Roe Alex", "P", 1000, 50.0, "2026-11-24")
        .split("<nonDerivativeTable>")[1]
        .split("</nonDerivativeTable>")[0]
        for _ in range(3)
    )
    xml = (
        filing("SYNB", "Roe Alex", "P", 1000, 50.0, "2026-11-24").split(
            "<nonDerivativeTable>"
        )[0]
        + f"<nonDerivativeTable>{rows}</nonDerivativeTable></ownershipDocument>"
    )
    single_rows = parse_form4(xml)
    # Each $50,000 row is under the threshold; the purchase is $150,000.
    assert len(single_rows) == 3
    assert not any(is_notable_buy(row, 100_000) for row in single_rows)
    (purchase,) = combine(single_rows)
    assert purchase.value == 150_000 and is_notable_buy(purchase, 100_000)


async def test_many_trades_in_one_company_are_one_review_item(workflow_session):
    session = workflow_session
    tag = uuid4().hex[:5].upper()
    entity = Entity(name=f"Synthetic Busy Insider {tag}", entity_type="company")
    session.add(entity)
    await session.flush()
    security = Security(
        entity_id=entity.id,
        ticker=f"BI{tag}",
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
    await session.commit()

    signals = OwnershipSignalService(session)
    ids = []
    for n, value in enumerate([40_000, 2_500_000, 90_000]):
        row = await signals.ingest_disclosure(
            source_name=f"Synthetic Form 4 feed {tag}",
            source_type="filing",
            source_item_type="insider_disclosure",
            title=f"Officer {n} sold BI{tag} shares",
            external_id=f"busy-{tag}-{n}",
            metadata={
                "ticker": f"BI{tag}",
                "transaction_type": "sold",
                "transaction_code": "S",
                "transaction_value": value,
                "filing_date": f"2026-11-2{n}",
            },
        )
        ids.append(row.id)

    await signals.queue_review_items()
    queued = (
        await session.scalars(
            select(ReviewQueueItem).where(ReviewQueueItem.item_id.in_(ids))
        )
    ).all()
    assert len(queued) == 1
    assert "2 other" in queued[0].trigger_reason
    assert f"BI{tag}" in queued[0].trigger_reason


async def test_undated_disclosures_do_not_hold_the_review_window(workflow_session):
    session = workflow_session
    tag = uuid4().hex[:5].upper()
    signals = OwnershipSignalService(session)
    undated = await signals.ingest_disclosure(
        source_name=f"Synthetic undated feed {tag}",
        source_type="filing",
        source_item_type="insider_disclosure",
        title=f"Officer sold UD{tag} shares",
        external_id=f"undated-{tag}",
        metadata={"ticker": f"UD{tag}", "transaction_type": "sold"},
    )
    assert undated.public_time is None and undated.event_time is None
    for n in range(3):
        await signals.ingest_disclosure(
            source_name=f"Synthetic dated feed {tag}",
            source_type="filing",
            source_item_type="insider_disclosure",
            title=f"Officer sold DT{tag}{n} shares",
            external_id=f"dated-{tag}-{n}",
            metadata={
                "ticker": f"DT{tag}{n}",
                "transaction_type": "sold",
                "filing_date": f"2099-02-0{n + 1}",
            },
        )
    disclosures_seen = []
    original = OwnershipSignalService.analyze_signal

    def spy(evidence, source, **kwargs):
        disclosures_seen.append(evidence.id)
        return original(evidence, source, **kwargs)

    signals.analyze_signal = spy
    await signals.queue_review_items(limit=3)
    assert undated.id not in disclosures_seen


def _one_day(paths: list[str]) -> dict[str, str]:
    daily = "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/"
    return {
        daily
        + "index.json": json.dumps(
            {"directory": {"item": [{"name": "form.20261124.idx"}]}}
        ),
        daily + "form.20261124.idx": index_file(paths),
    }


async def test_a_filing_that_fails_to_fetch_is_retried_on_later_runs(
    workflow_session, monkeypatch, tmp_path
):
    """The day's checkpoint moves on; the failed filing is not lost with it."""
    session = workflow_session
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(business_map, "REQUEST_SPACING_SECONDS", 0)
    tag = uuid4().hex[:5].upper()
    ticker = f"R{tag}"
    base = "https://www.sec.gov/Archives/"
    paths = [f"edgar/data/88/0000099{tag}-26-00000{n}.txt" for n in (1, 2)]
    pages = _one_day(paths)
    pages[base + paths[0]] = filing(ticker, "Roe Alex", "P", 5000, 50.0, "2026-11-24")
    pages[base + paths[1]] = filing(ticker, "Doe Lee", "P", 4000, 50.0, "2026-11-24")
    failing = {base + paths[1]}

    async def fake_fetch(url):
        if url in failing:
            raise OSError("temporary failure")
        return pages[url]

    service = MarketInsiderBuyingService(session, fetch=fake_fetch)
    first = await service.refresh(today=date(2026, 11, 26))
    assert (first["days"], first["buys"], first["unread"]) == (1, 1, 1)

    # The source recovers: the next run reads the filing it missed.
    failing.clear()
    second = await service.refresh(today=date(2026, 11, 26))
    assert (second["days"], second["buys"], second["unread"]) == (0, 1, 0)
    owners = (
        await session.scalars(
            select(RawEvidence.metadata_json["owner"].astext).where(
                RawEvidence.metadata_json["ticker"].astext == ticker
            )
        )
    ).all()
    assert sorted(owners) == ["Doe Lee", "Roe Alex"]


async def test_a_filing_that_keeps_failing_is_given_up_and_counted(
    workflow_session, monkeypatch, tmp_path
):
    session = workflow_session
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(business_map, "REQUEST_SPACING_SECONDS", 0)
    tag = uuid4().hex[:5].upper()
    pages = _one_day([f"edgar/data/88/0000077{tag}-26-000001.txt"])

    async def fake_fetch(url):
        if url in pages:
            return pages[url]
        raise OSError("gone")

    service = MarketInsiderBuyingService(session, fetch=fake_fetch)
    results = [await service.refresh(today=date(2026, 11, 26)) for _ in range(5)]
    assert [r["unread"] for r in results] == [1, 1, 1, 1, 0]
    assert [r["given_up"] for r in results] == [0, 0, 0, 0, 1]


async def test_a_disclosure_whose_signal_failed_gets_it_on_the_next_ingestion(
    workflow_session, monkeypatch
):
    """Evidence is committed before its signal; a failure in between is repaired."""
    session = workflow_session
    tag = uuid4().hex[:5].upper()
    service = OwnershipSignalService(session)
    disclosure = dict(
        source_name="Synthetic insider feed",
        source_type="filing",
        source_url="https://www.sec.gov/",
        source_description="Synthetic Form 4 feed.",
        source_item_type="insider_disclosure",
        title=f"Roe Alex (Director) bought 5,000 Q{tag} shares",
        url=f"https://www.sec.gov/Archives/edgar/data/88/{tag}.txt",
        external_id=f"{tag}:P",
        author="Roe Alex",
        metadata={
            "ticker": f"Q{tag}",
            "issuer": "Synthetic Buyers Inc",
            "owner": "Roe Alex",
            "role": "Director",
            "transaction_code": "P",
            "transaction_type": "bought",
            "shares": 5000,
            "price": 50.0,
            "transaction_value": 250000.0,
            "acquired": True,
            "transaction_date": "2026-11-24",
            "filing_date": "2026-11-24",
        },
        summary="Roe Alex bought 5,000 shares.",
    )
    real_store = OwnershipSignalService._store_market_setup_signal

    async def failing_store(self, evidence, source):
        raise RuntimeError("interrupted before the signal was stored")

    monkeypatch.setattr(
        OwnershipSignalService, "_store_market_setup_signal", failing_store
    )
    try:
        await service.ingest_disclosure(**disclosure)
    except RuntimeError:
        pass
    evidence = await session.scalar(
        select(RawEvidence).where(RawEvidence.external_id == f"{tag}:P")
    )
    assert evidence is not None
    count = (
        select(func.count())
        .select_from(MarketSetupSignal)
        .where(MarketSetupSignal.raw_evidence_id == evidence.id)
    )
    assert await session.scalar(count) == 0

    monkeypatch.setattr(
        OwnershipSignalService, "_store_market_setup_signal", real_store
    )
    again = await service.ingest_disclosure(**disclosure)
    assert again.id == evidence.id
    assert await session.scalar(count) == 1
    # A third ingestion neither duplicates the evidence nor the signal.
    await service.ingest_disclosure(**disclosure)
    assert await session.scalar(count) == 1
