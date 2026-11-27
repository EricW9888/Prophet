"""ARK's daily trades come from comparing its published daily holdings."""

from datetime import date

from sqlalchemy import select

from investos.config import settings
from investos.models.entity import Security
from investos.models.evidence import RawEvidence
from investos.models.opportunity import OpportunityUniverseMember
from investos.services import ark_holdings as module
from investos.services.ark_holdings import (
    ArkHoldingsService,
    describe,
    parse_holdings,
    trades_between,
)
from tests.test_investor_workflows import workflow_session  # noqa: F401

HEADER = "date,fund,company,ticker,cusip,shares,market value ($),weight (%)\n"
DAY1 = HEADER + (
    '11/24/2026,ARKK,SYNTH ROBOTICS INC,SYNR,000000001,"1,000,000","$50,000,000.00",10.00%\n'
    '11/24/2026,ARKK,SYNTH BIO INC,SYNB,000000002,"500,000","$10,000,000.00",2.00%\n'
    '11/24/2026,ARKK,SYNTH CLOUD INC,SYNC,000000003,"200,000","$4,000,000.00",1.00%\n'
)
DAY2 = HEADER + (
    '11/25/2026,ARKK,SYNTH ROBOTICS INC,SYNR,000000001,"1,100,000","$55,000,000.00",11.00%\n'
    '11/25/2026,ARKK,SYNTH BIO INC,SYNB,000000002,"500,200","$10,004,000.00",2.00%\n'
    '11/25/2026,ARKK,SYNTH SPACE INC,SYNS,000000004,"300,000","$9,000,000.00",1.50%\n'
)


def test_changes_between_two_days_are_trades():
    previous, current = parse_holdings(DAY1, "ARKK"), parse_holdings(DAY2, "ARKK")
    trades = {trade.ticker: trade for trade in trades_between(previous, current)}

    # SYNB moved 0.04%, which is rounding, not a trade.
    assert set(trades) == {"SYNR", "SYNC", "SYNS"}
    assert trades["SYNR"].change == 100_000 and trades["SYNR"].price == 50.0
    assert trades["SYNC"].shares_after == 0
    assert trades["SYNS"].shares_before == 0


def test_trades_read_as_sentences():
    previous, current = parse_holdings(DAY1, "ARKK"), parse_holdings(DAY2, "ARKK")
    trades = {trade.ticker: trade for trade in trades_between(previous, current)}

    assert describe(trades["SYNR"])[0] == "ARK (ARKK) bought 100,000 SYNR shares"
    assert (
        describe(trades["SYNC"])[0]
        == "ARK (ARKK) closed its position: sold 200,000 SYNC shares"
    )
    assert (
        describe(trades["SYNS"])[0]
        == "ARK (ARKK) opened a position: bought 300,000 SYNS shares"
    )
    assert "Value about $5,000,000" in describe(trades["SYNR"])[1]


async def test_first_run_is_a_baseline_and_the_next_day_records_trades(
    workflow_session, monkeypatch, tmp_path
):
    session = workflow_session
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(module, "FUNDS", {"ARKK": ["ARK_SYNTH_ARKK_HOLDINGS"]})
    pages = {"day": DAY1}

    async def fake_fetch(url):
        assert url.endswith("ARK_SYNTH_ARKK_HOLDINGS.csv")
        return pages["day"]

    service = ArkHoldingsService(session, fetch=fake_fetch)
    today = date(2026, 11, 26)
    nothing = {"funds": 1, "trades": 0, "ideas": 0, "stale": []}
    assert await service.refresh(today=today) == nothing
    assert await service.refresh(today=today) == nothing  # same day again

    pages["day"] = DAY2
    assert await service.refresh(today=today) == {
        "funds": 1,
        "trades": 3,
        "ideas": 1,
        "stale": [],
    }
    rows = (
        await session.scalars(
            select(RawEvidence).where(
                RawEvidence.source_item_type == "institutional_flow",
                RawEvidence.title.like("ARK (ARKK)%SYN%"),
            )
        )
    ).all()
    assert len(rows) == 3
    assert {row.metadata_json["transaction_type"] for row in rows} == {"buy", "sell"}
    assert parse_holdings(DAY2, "ARKK").day == date(2026, 11, 25)


async def test_a_new_position_goes_to_ideas_and_a_stalled_file_is_reported(
    workflow_session, monkeypatch, tmp_path
):
    session = workflow_session
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(module, "FUNDS", {"ARKK": ["ARK_SYNTH_ARKK_HOLDINGS"]})
    pages = {"day": DAY1}

    async def fake_fetch(url):
        return pages["day"]

    service = ArkHoldingsService(session, fetch=fake_fetch)
    await service.refresh(today=date(2026, 11, 25))
    pages["day"] = DAY2
    await service.refresh(today=date(2026, 11, 26))

    # Only the opened position (SYNS) is a discovery signal; the add (SYNR) is not.
    members = {
        security.ticker: member
        for member, security in (
            await session.execute(
                select(OpportunityUniverseMember, Security)
                .join(Security, OpportunityUniverseMember.security_id == Security.id)
                .where(Security.ticker.in_(["SYNS", "SYNR"]))
            )
        ).all()
        if any(
            origin["source_type"] == "ark_buying"
            for origin in (member.metadata_json or {}).get("origins", [])
        )
    }
    assert set(members) == {"SYNS"}
    (origin,) = [
        origin
        for origin in members["SYNS"].metadata_json["origins"]
        if origin["source_type"] == "ark_buying"
    ]
    assert origin["label"] == "ARK (ARKK) opened a position on Nov 25"
    assert members["SYNS"].priority >= module.OPENING_PRIORITY

    # Weeks later the file still says Nov 25: the fund has stopped updating.
    counts = await service.refresh(today=date(2026, 12, 20))
    assert counts["stale"] == ["ARKK (last file Nov 25)"] and counts["trades"] == 0
