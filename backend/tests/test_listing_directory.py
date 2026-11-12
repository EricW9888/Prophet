"""Exchange metadata must not imply research, investment merit or owner consent."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from investos.config import settings
from investos.db import get_session
from investos.main import app
from investos.models.entity import Entity, Security
from investos.models.opportunity import (
    ListingDirectory,
    OpportunityUniverseMember,
    ResearchListing,
)
from investos.models.portfolio import Position, Transaction
from investos.services.automation import AutomationCoordinator
from investos.services.listing_directory import (
    ListingDirectoryService,
    parse_listing_directory,
)
from investos.services.opportunity import OpportunityDiscoveryService
from investos.services.runtime_settings import RuntimeSettingsStore
from investos.services.security_catalog import SecurityCatalogService
from tests.test_investor_workflows import workflow_session


def directory_text(rows=None):
    rows = rows or [
        (f"SYN{i}", f"Synthetic company {i} - Common Stock", "N", "N")
        for i in range(25)
    ]
    return (
        "Symbol|Security Name|Test Issue|ETF\n"
        + "\n".join("|".join(row) for row in rows)
        + "\nFile Creation Time: 0520202521:31|||\n"
    )


@pytest.fixture
def approved_scope(monkeypatch):
    runtime = RuntimeSettingsStore.load()
    runtime.opportunity_discovery.universe_scope = "us_listed"
    monkeypatch.setattr(RuntimeSettingsStore, "load", lambda: runtime)
    return runtime


async def stored_directory(session, rows=None):
    directory = ListingDirectory(
        url=f"https://example.test/{uuid4()}/listings",
        status="ok",
        last_success_at=datetime.now(UTC),
    )
    session.add(directory)
    await session.flush()
    await ListingDirectoryService(session)._store(
        directory,
        parse_listing_directory(directory_text(rows)),
        observed=datetime.now(UTC),
    )
    await session.commit()
    return directory


def test_directory_classification_is_conservative_and_retains_unknown_types():
    parsed = parse_listing_directory(
        directory_text(
            [
                ("SYNCOM", "Synthetic Ordinary Share", "N", "N"),
                ("SYNADR", "Synthetic American Depositary Shares", "N", "N"),
                ("SYNWAR", "Synthetic warrants to purchase common stock", "N", "N"),
                ("SYNETF", "Synthetic Common Stock Fund ETF", "N", "Y"),
                ("SYNTEST", "Synthetic Common Stock Test", "Y", "N"),
                ("SYNUNK", "Synthetic instrument type undisclosed", "N", "N"),
            ]
        )
    )
    assert [r["instrument_type"] for r in parsed["rows"]] == [
        "common_stock",
        "adr",
        "unclassified",
        "etf",
        "test",
        "unclassified",
    ]
    assert sum(r["research_eligible"] for r in parsed["rows"]) == 2
    assert parsed["publisher_clock"] == "File Creation Time: 0520202521:31"
    for bad in (
        "<html>Blocked</html>",
        directory_text().split("File Creation Time:")[0],
        directory_text() + "LATE|Late Common Stock|N|N\n",
        directory_text([("SYN", "Synthetic Common Stock", "?", "N")]),
    ):
        with pytest.raises(ValueError):
            parse_listing_directory(bad)


def test_other_listed_venues_follow_the_publisher_contract_and_retain_raw_codes():
    parsed = parse_listing_directory(
        "ACT Symbol|Security Name|Exchange|Test Issue|ETF\n"
        "SYN|Synthetic Common Stock|N|N|N\n"
        "SYN2|Synthetic Ordinary Shares|UNKNOWN|N|N\n"
        "File Creation Time: 0520202521:31||||\n"
    )
    assert parsed["rows"][0]["exchange"] == "NYSE"
    assert parsed["rows"][0]["metadata_json"]["directory_fields"]["Exchange"] == "N"
    assert parsed["rows"][1]["exchange"] == "UNKNOWN"


async def test_directory_metadata_is_idempotent_paged_and_outside_the_graph(
    workflow_session, approved_scope
):
    session = workflow_session
    directory = await stored_directory(session)
    service = ListingDirectoryService(session)
    await service._store(
        directory, parse_listing_directory(directory_text()), observed=datetime.now(UTC)
    )
    await session.commit()
    assert await session.scalar(select(func.count()).select_from(ResearchListing)) == 25
    assert await session.scalar(select(func.count()).select_from(Entity)) == 0
    first, second = await service.status(), await service.status(offset=20)
    assert (first["total"], first["eligible"], first["materialized"]) == (25, 25, 0)
    assert len(first["items"]) == 20 and len(second["items"]) == 5
    assert not {r["id"] for r in first["items"]} & {r["id"] for r in second["items"]}
    filtered = await service.status(query="SYN1")
    assert filtered["total"] == 25 and filtered["match_count"] == 11
    assert (await service.status(query="%"))["match_count"] == 0


async def test_materialization_is_bounded_preserves_pauses_and_captures_listing_basis(
    workflow_session, approved_scope
):
    session = workflow_session
    await stored_directory(
        session,
        [
            ("SYNA", "Synthetic A Common Stock", "N", "N"),
            ("SYNB", "Synthetic B Common Stock", "N", "N"),
            ("SYNC", "Synthetic C Common Stock", "N", "N"),
        ],
    )
    security = await SecurityCatalogService(session).resolve_or_create_equity(
        ticker="SYNA", entity_name="Owner canonical name"
    )
    existing = OpportunityUniverseMember(
        security_id=security.id,
        entity_id=security.entity_id,
        enabled=False,
        priority=0.91,
        source="manual",
    )
    session.add(existing)
    await session.commit()
    service = ListingDirectoryService(session)
    result = await service.materialize(limit=2)
    assert (result["materialized"], result["linked_existing"]) == (1, 1)
    await session.refresh(existing)
    assert existing.enabled is False and existing.priority == 0.91
    assert security.entity.name == "Owner canonical name"
    assert await session.scalar(select(func.count()).select_from(Entity)) == 2
    assert await session.scalar(select(func.count()).select_from(Position)) == 0
    assert await session.scalar(select(func.count()).select_from(Transaction)) == 0
    new = await session.scalar(
        select(OpportunityUniverseMember).where(
            OpportunityUniverseMember.source == "us_exchange_directory"
        )
    )
    origin = new.metadata_json["origins"][0]
    assert origin["metadata"]["listing_basis"]["snapshot_hash"]
    assert origin["metadata"]["listing_basis"]["directory_fields"]["Symbol"] == "SYNB"
    # Deleting a prepared subject is an exclusion, not permission to re-add it.
    await OpportunityDiscoveryService(session).remove_universe_member(new.id)
    listing = await session.scalar(
        select(ResearchListing)
        .where(ResearchListing.symbol == "SYNB")
        .execution_options(populate_existing=True)
    )
    assert listing.operator_excluded is True and listing.member_id is None
    await service.materialize(limit=2)
    assert listing.member_id is None
    await service.set_excluded(listing.id, False)
    await service.materialize(limit=2)
    await session.refresh(listing)
    assert listing.member_id is not None


async def test_refresh_failure_retains_previous_snapshot_and_does_not_retire_rows(
    workflow_session, approved_scope, monkeypatch
):
    session = workflow_session
    directory = await stored_directory(session)
    previous = directory.last_success_at
    monkeypatch.setattr(settings, "US_LISTING_DIRECTORY_URLS", [directory.url])
    monkeypatch.setattr(
        ListingDirectoryService,
        "_fetch",
        AsyncMock(side_effect=ValueError("Incomplete publisher response")),
    )
    result = await ListingDirectoryService(session).refresh()
    assert result["status"] == "warning"
    await session.refresh(directory)
    assert directory.status == "error" and directory.last_success_at == previous
    assert (
        await session.scalar(
            select(func.count())
            .select_from(ResearchListing)
            .where(ResearchListing.is_current.is_(True))
        )
        == 25
    )


async def test_complete_snapshot_retires_only_directory_presence_and_preserves_exclusion(
    workflow_session, approved_scope
):
    session = workflow_session
    directory = await stored_directory(session)
    service = ListingDirectoryService(session)
    listing = await session.scalar(
        select(ResearchListing).where(ResearchListing.symbol == "SYN0")
    )
    await service.set_excluded(listing.id, True)
    await service._store(
        directory,
        parse_listing_directory(
            directory_text([("SYN0", "Renamed Common Stock", "N", "N")])
        ),
        observed=datetime.now(UTC) + timedelta(seconds=1),
    )
    await session.commit()
    await session.refresh(listing)
    assert listing.is_current and listing.operator_excluded
    assert (
        await session.scalar(
            select(func.count())
            .select_from(ResearchListing)
            .where(ResearchListing.is_current.is_(True))
        )
        == 1
    )
    assert await session.scalar(select(func.count()).select_from(ResearchListing)) == 25


async def test_parser_upgrade_reprocesses_an_unchanged_snapshot_before_its_next_poll(
    workflow_session, approved_scope, monkeypatch
):
    session = workflow_session
    directory = await stored_directory(session)
    directory.next_refresh_at = datetime.now(UTC) + timedelta(hours=1)
    directory.metadata_json = {**directory.metadata_json, "parser_revision": 0}
    listing = await session.scalar(select(ResearchListing).limit(1))
    listing.research_eligible = False
    await session.commit()
    monkeypatch.setattr(settings, "US_LISTING_DIRECTORY_URLS", [directory.url])
    monkeypatch.setattr(
        ListingDirectoryService,
        "_fetch",
        AsyncMock(return_value=parse_listing_directory(directory_text())),
    )
    result = await ListingDirectoryService(session).refresh()
    await session.refresh(listing)
    assert result["directories_changed"] == 1 and listing.research_eligible is True


async def test_directory_api_paginates_and_keeps_research_controls_reversible(
    workflow_session, approved_scope
):
    session = workflow_session
    await stored_directory(session)

    async def dependency():
        yield session

    app.dependency_overrides[get_session] = dependency
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = await client.get("/api/opportunities/listings")
            assert first.status_code == 200 and len(first.json()["items"]) == 20
            item = first.json()["items"][0]
            changed = await client.patch(
                f"/api/opportunities/listings/{item['id']}", json={"excluded": True}
            )
            assert changed.status_code == 200 and changed.json()["operator_excluded"]
            next_page = await client.get("/api/opportunities/listings?offset=20")
            assert len(next_page.json()["items"]) == 5
            assert (
                await client.patch(
                    f"/api/opportunities/listings/{uuid4()}", json={"excluded": False}
                )
            ).status_code == 404
    finally:
        app.dependency_overrides.pop(get_session, None)


def test_listing_acquisition_never_depends_on_model_health(monkeypatch):
    monkeypatch.setattr(
        "investos.services.automation.ProviderHealthStore.public_status",
        lambda: (_ for _ in ()).throw(AssertionError("Model checked")),
    )
    assert AutomationCoordinator()._provider_defers("listing_directory") is False


async def test_broad_scope_enters_the_existing_bounded_discovery_path(
    workflow_session, approved_scope, monkeypatch
):
    session = workflow_session
    await stored_directory(session)
    approved_scope.opportunity_discovery.max_subjects_per_run = 2
    service = OpportunityDiscoveryService(session)
    dispatched = []

    async def inspect(*, run, member, security, entity):
        dispatched.append(security.ticker)
        run.inspected_count += 1
        run.remaining_member_ids_json = [
            v for v in run.remaining_member_ids_json if v != str(member.id)
        ]
        member.last_inspected_at = datetime.now(UTC)

    monkeypatch.setattr(service, "_inspect_member", inspect)
    monkeypatch.setattr(service, "_evaluate_run", AsyncMock())
    monkeypatch.setattr(service, "evaluate_due_outcomes", AsyncMock(return_value={}))
    result = await service.run_discovery()
    assert len(dispatched) == 2
    assert (
        result["planned_count"]
        == result["inspected_count"]
        == result["universe_size"]
        == 2
    )
    assert result["limits"]["universe_scope"] == "us_listed"
    assert result["limits"]["directory_frontier"]["materialized"] == 2
    assert await session.scalar(select(func.count()).select_from(Position)) == 0


async def test_scope_change_uses_existing_settings_and_wakes_acquisition(
    approved_scope, monkeypatch
):
    runtime = approved_scope
    runtime.opportunity_discovery.universe_scope = "manual"
    prior_provider = runtime.llm.provider
    coordinator = MagicMock()
    monkeypatch.setattr(app.state, "automation", coordinator, raising=False)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(
            "/api/integrations/settings",
            json={"opportunity_discovery": {"universe_scope": "us_listed"}},
        )
    assert response.status_code == 200
    assert response.json()["opportunity_discovery"]["universe_scope"] == "us_listed"
    assert runtime.llm.provider == prior_provider and runtime.plaid.enabled is False
    coordinator.signal_ready_work.assert_called_once_with("universe_scope_changed")
