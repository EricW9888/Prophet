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

    assert describe(trades["SYNR"])[0] == "ARK (ARKK) bought about 100,000 SYNR shares"
    assert "ARK does not report this figure" in describe(trades["SYNR"])[1]
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
    nothing = {"funds": 1, "trades": 0, "ideas": 0, "stale": [], "splits": []}
    assert await service.refresh(today=today) == nothing
    assert await service.refresh(today=today) == nothing  # same day again

    pages["day"] = DAY2
    assert await service.refresh(today=today) == {
        "funds": 1,
        "trades": 3,
        "ideas": 1,
        "stale": [],
        "splits": [],
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
    # Only the add is an estimate; an opened or closed position is exact.
    assert {
        row.metadata_json["ticker"]: row.metadata_json["estimated"] for row in rows
    } == {"SYNR": True, "SYNC": False, "SYNS": False}
    # Both days' holdings are kept, so each change can be recomputed.
    kept = sorted(p.name for p in (tmp_path / "_system" / "ark" / "ARKK").iterdir())
    assert kept == ["2026-11-24.json", "2026-11-25.json"]
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


def test_fund_wide_flows_are_not_trades():
    # Investors bought the ETF: every holding grew 3%. Only SYN6 was a decision.
    names = [f"SYN{n}" for n in range(1, 8)]
    day1 = HEADER + "".join(
        f'11/24/2026,ARKG,SYNTH {t} INC,{t},00000000{n},"100,000","$1,000,000.00",1.00%\n'
        for n, t in enumerate(names)
    )
    grown = {t: 103_000 for t in names}
    grown["SYN6"] = 125_000
    day2 = HEADER + "".join(
        f'11/25/2026,ARKG,SYNTH {t} INC,{t},00000000{n},"{grown[t]:,}","$1,000,000.00",1.00%\n'
        for n, t in enumerate(names)
    )
    previous, current = parse_holdings(day1, "ARKG"), parse_holdings(day2, "ARKG")
    (trade,) = trades_between(previous, current)
    assert trade.ticker == "SYN6"
    assert round(trade.change) == 22_000  # 125,000 - 100,000 x 1.03
    title, summary = describe(trade)
    assert title == "ARK (ARKG) bought about 22,000 SYN6 shares"
    assert "whole fund grew 3.0% as investors bought its shares" in summary


def _fund_day(day: str, rows: dict[str, tuple[int, int]]) -> str:
    return HEADER + "".join(
        f'{day},ARKQ,SYNTH {t} INC,{t},00000000{n},"{shares:,}","${value:,}.00",1.00%\n'
        for n, (t, (shares, value)) in enumerate(rows.items())
    )


def test_a_split_is_not_a_trade_but_a_real_doubling_is():
    steady = {f"SYQ{n}": (100_000, 1_000_000) for n in range(1, 6)}
    day1 = _fund_day(
        "11/24/2026",
        {**steady, "SPLT": (100_000, 5_000_000), "DBLE": (100_000, 5_000_000)},
    )
    # SPLT split 2-for-1: twice the shares, the same value. DBLE was doubled.
    day2 = _fund_day(
        "11/25/2026",
        {**steady, "SPLT": (200_000, 5_100_000), "DBLE": (200_000, 10_100_000)},
    )
    previous, current = parse_holdings(day1, "ARKQ"), parse_holdings(day2, "ARKQ")
    assert module.likely_splits(previous, current) == ["SPLT"]
    assert [trade.ticker for trade in trades_between(previous, current)] == ["DBLE"]


def test_a_change_across_unread_days_says_so():
    steady = {f"SYQ{n}": (100_000, 1_000_000) for n in range(1, 6)}
    day1 = _fund_day("11/16/2026", {**steady, "GAPS": (100_000, 1_000_000)})
    day2 = _fund_day("11/25/2026", {**steady, "GAPS": (150_000, 1_500_000)})
    (trade,) = trades_between(
        parse_holdings(day1, "ARKQ"), parse_holdings(day2, "ARKQ")
    )
    assert "may combine several days of trading" in describe(trade)[1]
