"""Synthetic record identities exercise the actual PostgreSQL navigation path."""

import importlib.util
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from alembic.migration import MigrationContext
from alembic.operations import Operations
from investos.api.routes.graph import get_holding_subjects
from investos.config import settings
from investos.db import get_session
from investos.main import app
from investos.models.catalog import SourceClaimRecord
from investos.models.entity import Entity, Security
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.graph import Edge
from investos.models.knowledge import Claim, Fact
from investos.models.knowledge_mutation import KnowledgeMutation
from investos.models.portfolio import CashLedgerEntry, Position
from investos.models.profile import Profile
from investos.models.review import ReviewQueueItem
from investos.models.shadow import ShadowFill, ShadowOrder
from investos.models.source import Source
from investos.schemas.portfolio import TransactionCorrectionRequest, TransactionCreate
from investos.schemas.shadow import (
    ShadowExperimentCreate,
    ShadowExperimentResponse,
    ShadowOrderCreate,
)
from investos.services.agent import PORTFOLIO_SUBJECT_ID, AgentService
from investos.services.dashboard import DashboardService
from investos.services.entity_hygiene import EntityHygieneService
from investos.services.graph_edge_state import GraphEdgeStateService
from investos.services.market_data import MarketDataService
from investos.services.operating_state import OperatingStateService
from investos.services.portfolio import PortfolioService
from investos.services.portfolio_history import PortfolioHistoryService
from investos.services.profile import ProfileService
from investos.services.retrieval import RetrievalService
from investos.services.review import ReviewService
from investos.services.shadow import ShadowService
from investos.services.source import SourceService
from investos.workers.extraction import ExtractionWorker


@pytest.fixture
async def workflow_session():
    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, poolclass=NullPool)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        ) as session:
            yield session
        await transaction.rollback()
    await engine.dispose()


async def test_portfolio_history_uses_close_time_cash_flows_gaps_and_receipts(
    workflow_session, monkeypatch
):
    from types import SimpleNamespace

    import exchange_calendars

    from investos.models.quant import AttributionResult
    from investos.models.watcher import ActiveWatcher
    from investos.services.runtime_settings import RuntimeSettingsStore

    session = workflow_session
    calendar = exchange_calendars.get_calendar("XNYS")
    now = datetime.now(UTC)
    dates = calendar.sessions_in_range(
        (now - timedelta(days=12)).date().isoformat(),
        (now - timedelta(days=1)).date().isoformat(),
    )[-3:]
    closes = [calendar.session_close(day).to_pydatetime() for day in dates]
    portfolio = PortfolioService(session)
    await portfolio.add_transaction_by_ticker(
        "SYNTIMELINE",
        TransactionCreate(
            action="buy",
            quantity=2,
            price=100,
            executed_at=calendar.session_open(dates[0]).to_pydatetime(),
        ),
    )
    # An after-hours purchase belongs after this day's closing valuation.
    purchase = await portfolio.add_transaction_by_ticker(
        "SYNTIMELINE",
        TransactionCreate(
            action="buy",
            quantity=1,
            price=100,
            executed_at=closes[1] + timedelta(hours=1),
        ),
    )
    source = Source(name="Synthetic timeline evidence", source_type="manual")
    session.add(source)
    await session.flush()
    evidence = RawEvidence(
        source_id=source.id,
        source_item_type="manual",
        title="Synthetic research",
        ingest_time=closes[1],
        is_processed=True,
    )
    watch = ActiveWatcher(
        source="owner",
        condition_type="price_below",
        objective="Synthetic alert",
        condition_params_json={},
        adjustment_plan="Inspect",
        status="triggered",
        is_active=False,
        triggered_at=closes[1],
    )
    session.add_all([evidence, watch])
    await session.flush()
    bars = [
        {
            "timestamp": calendar.session_open(day).isoformat(),
            "completed_at": close.isoformat(),
            "available_at": now.isoformat(),
            "close": price,
            "adjusted_close": price,
        }
        for day, close, price in zip(dates, closes, (100, 110, 120))
    ]
    calls = []

    async def snapshots(_service, tickers, **_kwargs):
        calls.append(tickers)
        return {
            "SYNTIMELINE": {
                "reported_symbol": "SYNTIMELINE",
                "currency": "USD",
                "reported_exchange": "NYQ",
                "exchange_timezone": "America/New_York",
                "daily_observations": bars,
            }
        }

    monkeypatch.setattr(MarketDataService, "fetch_signal_snapshots", snapshots)
    monkeypatch.setattr(
        RuntimeSettingsStore,
        "load",
        lambda: SimpleNamespace(
            market_data=SimpleNamespace(enabled=True, provider="yahoo_finance")
        ),
    )
    service = PortfolioHistoryService(session)
    result = await service.history()
    valid = [point for point in result.points if point.value is not None]
    assert [point.value for point in valid] == [200, 220, 360]
    assert [point.gain for point in valid] == [0, 20, 60]
    assert valid[1].at == closes[1]
    assert {event.kind for event in result.events} == {"ledger", "research", "alert"}
    assert any(
        event.href == f"/history/receipt/transaction/{purchase.id}"
        for event in result.events
    )
    saved = await session.get(AttributionResult, result.receipt_id)
    assert saved.factor_contributions_json["prices"]["SYNTIMELINE"][
        "daily_observations"
    ]
    assert saved.factor_contributions_json["ledger"]
    assert (await service.history()).cache_hit
    assert len(calls) == 1

    async def session_override():
        yield session

    monkeypatch.setitem(app.dependency_overrides, get_session, session_override)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        receipt = await client.get(f"/api/risk/portfolio/history/{result.receipt_id}")
        assert receipt.status_code == 200
        assert receipt.json()["response"]["receipt_id"] == str(result.receipt_id)
        alert = await client.get(f"/api/watcher/{watch.id}")
        assert alert.status_code == 200
        assert alert.json()["status"] == "triggered"

    bars.pop(1)
    refreshed = await service.history(refresh=True)
    gap = next(point for point in refreshed.points if point.at == closes[1])
    assert gap.value is None and gap.unavailable_tickers == ["SYNTIMELINE"]
    assert any(point.gain == 60 for point in refreshed.points)

    position = await session.get(Position, purchase.position_id)
    position.quantity += 1
    await session.flush()
    inconsistent = await service.history()
    assert not inconsistent.cache_hit
    assert all(point.value is None for point in inconsistent.points)
    assert inconsistent.reasons == [
        "The recorded trades do not reconstruct the current holdings."
    ]
    position.quantity -= 1
    await session.flush()
    await portfolio.add_transaction_by_ticker(
        "SYNTIMELINE",
        TransactionCreate(
            action="split",
            quantity=2,
            price=0,
            executed_at=closes[1] + timedelta(hours=2),
        ),
    )
    after_split = await service.history(refresh=True)
    assert all(
        point.value is None for point in after_split.points if point.at <= closes[1]
    )
    assert any(point.value == 720 for point in after_split.points)


async def test_portfolio_monitor_counts_relevant_reviews_beyond_the_global_preview(
    workflow_session,
):
    session = workflow_session
    held = Entity(name="Synthetic monitored issuer", entity_type="company")
    session.add(held)
    await session.flush()
    security = Security(
        entity_id=held.id,
        ticker=f"SYN{uuid4().hex[:8]}",
        asset_class="equity",
        instrument_type="common_stock",
    )
    session.add(security)
    await session.flush()
    position = Position(
        security_id=security.id, direction="long", list_type="holding", quantity=1
    )
    session.add(position)
    await session.flush()
    session.add_all(
        [
            ReviewQueueItem(
                item_type="entity",
                item_id=uuid4(),
                priority_score=100,
                trigger_reason="Unrelated high priority review",
            )
            for _ in range(100)
        ]
        + [
            ReviewQueueItem(
                item_type="entity" if index % 2 else "position",
                item_id=held.id if index % 2 else position.id,
                priority_score=80 - index,
                status="in_review" if index == 1 else "pending",
                trigger_reason=f"Relevant review {index}",
            )
            for index in range(6)
        ]
        + [
            ReviewQueueItem(
                item_type="shadow_experiment",
                item_id=uuid4(),
                priority_score=79.5,
                trigger_reason="Paper experiment review",
            ),
            ReviewQueueItem(
                item_type="entity",
                item_id=held.id,
                priority_score=99,
                status="resolved",
                trigger_reason="Resolved review",
            ),
        ]
    )
    await session.flush()

    monitor = await OperatingStateService(session).portfolio_monitor_payload()
    assert monitor["priority_review_count"] == 7
    assert [row["trigger_reason"] for row in monitor["priority_review_items"]] == [
        "Relevant review 0",
        "Paper experiment review",
        "Relevant review 1",
        "Relevant review 2",
        "Relevant review 3",
    ]
    dashboard = await DashboardService(session)._portfolio_monitor()
    assert dashboard.priority_review_count == monitor["priority_review_count"]
    assert [item.model_dump() for item in dashboard.priority_review_items] == monitor[
        "priority_review_items"
    ]


async def test_broker_equity_reconciles_statement_dates_and_active_ledger_receipts(
    workflow_session, monkeypatch
):
    session = workflow_session
    service = PortfolioService(session)
    start = datetime.now(UTC) - timedelta(days=5)
    await service.add_transaction(
        None,
        TransactionCreate(action="deposit", quantity=1, price=100, executed_at=start),
    )
    deposit = await service.add_transaction(
        None,
        TransactionCreate(
            action="deposit",
            quantity=1,
            price=900,
            executed_at=start + timedelta(days=1),
        ),
    )
    buy = await service.add_transaction_by_ticker(
        "SYNEQUITY",
        TransactionCreate(
            action="buy", quantity=2, price=100, executed_at=start + timedelta(days=2)
        ),
    )
    base = "/api/portfolio/broker-equity"

    async def session_override():
        yield session

    monkeypatch.setitem(app.dependency_overrides, get_session, session_override)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:

        async def preview(day, equity, cash, holdings=""):
            statement = {
                "as_of": (start + timedelta(days=day, hours=1)).isoformat(),
                "equity": equity,
                "cash": cash,
                "holdings_text": holdings,
                "cash_only": not holdings,
            }
            compared = await client.post(base + "/preview", json=statement)
            assert compared.status_code == 200, compared.text
            comparison = compared.json()
            return comparison["reconciliation"], {
                "statement": statement,
                "preview_token": comparison["preview_token"],
            }

        async def save(payload):
            saved = await client.post(base, json=payload)
            assert saved.status_code == 200, saved.text
            return saved.json()["id"]

        check, payload = await preview(0, 100, 100)
        assert check["reasons"] == []
        first = await save(payload)
        check, payload = await preview(2, 1100, 800, "SYNEQUITY 2")
        assert check["reasons"] == []
        second = await save(payload)
        assert await save(payload) == second
        check, payload = await preview(3, 1150, None, "SYNEQUITY 2")
        assert check["reasons"] == ["The statement has no cash balance."]
        missing = await save(payload)

        history = (await client.get(base)).json()
        assert [p["id"] for p in history["points"]] == [first, second, missing]
        assert [p["value"] for p in history["points"]] == ["100", "1100", None]
        assert [e["id"] for e in history["events"]] == [str(deposit.id), str(buy.id)]
        assert [Decimal(e["cash"]) for e in history["events"]] == [900, -200]
        receipt = (await client.get(base + "/" + second)).json()
        assert receipt["reconciliation_at_import"]["ledger_cash"] == "800"

        await session.execute(
            delete(CashLedgerEntry).where(CashLedgerEntry.transaction_id == buy.id)
        )
        check, _ = await preview(2, 1100, 800, "SYNEQUITY 2")
        assert (
            "The ledger has incomplete cash activity through this time."
            in check["reasons"]
        )

        _, stale = await preview(2, 1100, 800, "SYNEQUITY 2")
        replacement = await service.correct_transaction(
            buy.id,
            TransactionCorrectionRequest(quantity=3, reason="Synthetic correction"),
        )
        assert (await client.post(base, json=stale)).status_code == 409
        history = (await client.get(base)).json()
        assert [p["value"] for p in history["points"]] == ["100", None, None]
        assert [e["id"] for e in history["events"]] == [
            str(deposit.id),
            str(replacement.id),
        ]
        prior = (await client.get(f"/api/portfolio/transactions/{buy.id}")).json()
        assert prior["superseded_by_id"] == str(replacement.id)
        assert (await client.get(base + "/" + second)).json() == receipt

        check, payload = await preview(2, 1120, 700, "SYNEQUITY 3")
        assert check["reasons"] == []
        updated = await save(payload)
        history = (await client.get(base)).json()
        assert [p["id"] for p in history["points"]] == [first, updated, missing]
        detail = (await client.get(base + "/" + updated)).json()
        assert detail["other_receipts_at_this_time"][0]["id"] == second


async def test_position_concern_resolves_profile_identity_and_keeps_its_scope(
    workflow_session,
):
    session = workflow_session
    entity = Entity(name="Synthetic workflow company", entity_type="company")
    session.add(entity)
    await session.flush()
    security = Security(
        entity_id=entity.id,
        ticker="SYNTH",
        asset_class="equity",
        instrument_type="common_stock",
    )
    session.add(security)
    await session.flush()
    position = Position(
        security_id=security.id, direction="long", list_type="watchlist", quantity=0
    )
    profile = Profile(subject_type="entity", subject_id=entity.id)
    session.add_all([position, profile])
    await session.flush()
    item = ReviewQueueItem(
        item_type="position",
        item_id=position.id,
        trigger_reason="Synthetic contrary evidence",
        priority_score=75,
    )
    session.add(item)
    await session.flush()
    rows = await ReviewService(session).list_queue()
    result = next(row for row in rows if row.id == item.id)
    assert result.subject_type == "position" and result.subject_id == position.id
    assert result.profile_id == profile.id
    assert len({result.id, result.item_id, result.profile_id}) == 3
    assert result.subject_id != entity.id
    assert result.trigger_reason == item.trigger_reason
    position_profile = Profile(subject_type="position", subject_id=position.id)
    session.add(position_profile)
    await session.flush()
    result = await ReviewService(session)._navigation_context(item)
    assert result == {
        "subject_id": position.id,
        "subject_type": "position",
        "profile_id": position_profile.id,
    }


@pytest.mark.parametrize(
    "kind",
    ["entity", "position", "theme", "raw_evidence", "mailbox_identity", "unknown"],
)
async def test_missing_records_do_not_invent_subjects_or_profiles(
    workflow_session, kind
):
    item = ReviewQueueItem(item_type=kind, item_id=uuid4())
    assert await ReviewService(workflow_session)._navigation_context(item) == {}


@pytest.mark.parametrize(
    "kind",
    [
        "raw_evidence",
        "mailbox_identity",
        "mailbox_receipt",
        "account_transfer",
        "corporate_action",
    ],
)
async def test_receipt_reviews_link_to_the_original_without_pinning_a_research_subject(
    workflow_session, kind
):
    session = workflow_session
    raw, _ = await source_records(session)
    item = ReviewQueueItem(item_type=kind, item_id=raw.id)
    assert await ReviewService(session)._navigation_context(item) == {
        "evidence_id": raw.id
    }
    assert await ReviewService(session)._label(kind, raw.id) == raw.title
    if kind == "mailbox_identity":
        item.priority_score = 95
        item.trigger_reason = "Synthetic ambiguous receipt identity"
        session.add(item)
        await session.flush()
        result = await ReviewService(session)._serialize(item)
        assert "identity" in result.why_now_summary
        assert "held for comparison" in result.why_now_summary
        assert "Receipt identity review" in result.next_action
        assert "research more" not in result.next_action


async def test_queue_refresh_retains_connector_legacy_and_in_progress_reviews(
    workflow_session,
):
    session = workflow_session
    retained = [
        ReviewQueueItem(
            item_type=kind,
            item_id=uuid4(),
            priority_score=80,
            status=status,
            trigger_reason="Synthetic retained concern",
        )
        for kind, status in [
            ("mailbox_identity", "pending"),
            ("reconciliation", "in_review"),
            ("entity", "pending"),
        ]
    ]
    active = ReviewQueueItem(
        item_type="entity",
        item_id=uuid4(),
        queue_owner="review_refresh",
        priority_score=10,
        status="in_review",
        trigger_reason="Synthetic human review already in progress",
    )
    stale = ReviewQueueItem(
        item_type="entity",
        item_id=uuid4(),
        queue_owner="review_refresh",
        priority_score=20,
        status="pending",
        trigger_reason="Synthetic obsolete computed concern",
    )
    session.add_all([*retained, active, stale])
    await session.flush()
    expected = {
        item.id: (item.status, item.trigger_reason) for item in [*retained, active]
    }
    stale_id = stale.id
    await ReviewService(session).refresh_queue()
    rows = (
        (
            await session.execute(
                select(ReviewQueueItem).where(
                    ReviewQueueItem.id.in_([*expected, stale_id])
                )
            )
        )
        .scalars()
        .all()
    )
    assert {row.id: (row.status, row.trigger_reason) for row in rows} == expected


async def test_computed_deduplication_preserves_human_work_and_other_producers(
    workflow_session,
):
    session = workflow_session
    subject_id = uuid4()
    common = dict(
        item_type="entity",
        item_id=subject_id,
        trigger_reason="Synthetic independent concern",
    )
    active = ReviewQueueItem(
        **common, queue_owner="review_refresh", status="in_review", priority_score=1
    )
    pending = ReviewQueueItem(
        **common, queue_owner="review_refresh", status="pending", priority_score=99
    )
    external = [
        ReviewQueueItem(**common, status="pending", priority_score=90) for _ in range(2)
    ]
    session.add_all([active, pending, *external])
    await session.flush()
    expected = {active.id, *(item.id for item in external)}
    await ReviewService(session)._deduplicate_queue()
    await session.flush()
    rows = (
        (
            await session.execute(
                select(ReviewQueueItem.id).where(ReviewQueueItem.item_id == subject_id)
            )
        )
        .scalars()
        .all()
    )
    assert set(rows) == expected


async def test_queue_pagination_and_lookup_keep_lower_priorities_reachable(
    workflow_session,
):
    session = workflow_session
    service = ReviewService(session)
    baseline = await service.queue_count()
    created = datetime.now(UTC)
    items = [
        ReviewQueueItem(
            item_type="entity",
            item_id=uuid4(),
            priority_score=10000,
            trigger_reason="Synthetic paged concern",
            created_at=created,
            status="in_review" if index == 0 else "pending",
        )
        for index in range(6)
    ]
    resolved = ReviewQueueItem(
        item_type="entity",
        item_id=uuid4(),
        priority_score=20000,
        trigger_reason="Synthetic resolved concern",
        status="resolved",
    )
    session.add_all([*items, resolved])
    await session.flush()
    expected = sorted((item.id for item in items), key=str)
    pages = [await service.queue_page(limit=2, offset=offset) for offset in [0, 2, 4]]
    assert all(page.total == baseline + 6 for page in pages)
    assert [item.id for page in pages for item in page.items] == expected
    assert (await service.queued_item(expected[-1])).id == expected[-1]
    assert await service.queued_item(resolved.id) is None
    assert await service.queued_item(uuid4()) is None


async def test_legacy_ownership_migration_preserves_rows_and_classifies_only_known_formats(
    workflow_session,
):
    session = workflow_session
    # A transactional temporary table shadows the product table. Exercise the
    # actual Alembic upgrade without downgrading or rewriting the test database.
    await session.execute(
        text(
            "CREATE TEMP TABLE review_queue_items (id UUID PRIMARY KEY, item_type TEXT NOT NULL, trigger_reason TEXT NOT NULL, status TEXT NOT NULL)"
        )
    )
    examples = [
        ("entity", "Open research question: Synthetic gap", "review_refresh"),
        ("position", "Holding lacks a strong accepted state.", "review_refresh"),
        (
            "position",
            "Holding stance is uncertain at low confidence.",
            "review_refresh",
        ),
        (
            "shadow_experiment",
            "Evidence-linked shadow opportunity: Synthetic",
            "review_refresh",
        ),
        (
            "source_claim_record",
            "Pending source claim due for outcome assessment: Synthetic",
            "review_refresh",
        ),
        (
            "raw_evidence",
            "Synthetic signal. Verify the disclosure and test whether it changes the thesis mechanism. Original",
            "review_refresh",
        ),
        (
            "mailbox_identity",
            "Open research question: Human identity decision",
            "retained",
        ),
        ("position", "Reconciliation discrepancy: synthetic quantity", "retained"),
        ("entity", "An unclassified legacy concern", "retained"),
    ]
    expected = {}
    for kind, reason, owner in examples:
        key = uuid4()
        expected[key] = (kind, reason, "in_review", owner)
        await session.execute(
            text(
                "INSERT INTO review_queue_items VALUES (:id, :kind, :reason, 'in_review')"
            ),
            {"id": key, "kind": kind, "reason": reason},
        )
    spec = importlib.util.spec_from_file_location(
        "review_ownership_upgrade",
        Path(__file__).parents[1]
        / "alembic/versions/c8e5a2b0f743_review_queue_ownership.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def upgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()

    await (await session.connection()).run_sync(upgrade)
    rows = (
        await session.execute(
            text(
                "SELECT id, item_type, trigger_reason, status, queue_owner FROM review_queue_items"
            )
        )
    ).all()
    assert {row.id: tuple(row)[1:] for row in rows} == expected


async def test_review_domains_filter_before_pagination_without_resolving_holds(
    workflow_session,
):
    session = workflow_session
    service = ReviewService(session)
    before = await service.queue_counts()
    created = datetime(2026, 1, 1, tzinfo=UTC)
    holds = [
        ReviewQueueItem(
            item_type="mailbox_identity",
            item_id=uuid4(),
            review_domain="portfolio_data",
            priority_score=20000,
            trigger_reason="Synthetic posting hold",
            status="in_review" if i == 0 else "pending",
        )
        for i in range(105)
    ]
    concerns = [
        ReviewQueueItem(
            item_type=kind,
            item_id=uuid4(),
            review_domain="investment",
            priority_score=10000,
            created_at=created,
            trigger_reason="Synthetic investment concern",
        )
        for kind in ["position", "raw_evidence", "entity"]
    ]
    session.add_all([*holds, *concerns])
    await session.flush()
    identities = {
        item.id: (item.status, item.priority_score, item.item_id)
        for item in [*holds, *concerns]
    }
    # The previous mixed-ranking query spends every dashboard slot on holds.
    assert all(
        row.review_domain == "portfolio_data"
        for row in await service.list_queue(limit=6)
    )
    pages = [
        await service.queue_page(limit=2, offset=offset, domain="investment")
        for offset in [0, 2]
    ]
    expected = sorted((item.id for item in concerns), key=str)
    actual = [row.id for page in pages for row in page.items][:3]
    assert actual == expected
    assert all(page.total == before["investment"] + 3 for page in pages)
    assert pages[0].counts == {
        "investment": before["investment"] + 3,
        "portfolio_data": before["portfolio_data"] + 105,
    }
    data_page = await service.queue_page(limit=100, offset=100, domain="portfolio_data")
    assert len({row.id for row in data_page.items} & set(identities)) == 5
    dashboard = await DashboardService(session)._review_queue()
    assert [row.id for row in dashboard][:3] == expected
    assert all(row.review_domain == "investment" for row in dashboard)
    assert (await service.queued_item(holds[-1].id)).review_domain == "portfolio_data"
    await session.refresh(holds[0])
    assert {
        item.id: (item.status, item.priority_score, item.item_id)
        for item in [*holds, *concerns]
    } == identities


async def test_position_reconciliation_producer_records_data_domain_and_keeps_identity(
    workflow_session,
):
    session = workflow_session
    entity = Entity(name="Synthetic reconciliation company", entity_type="company")
    session.add(entity)
    await session.flush()
    security = Security(
        entity_id=entity.id,
        ticker="SYNREVIEW",
        asset_class="equity",
        instrument_type="common_stock",
    )
    session.add(security)
    await session.flush()
    position = Position(
        security_id=security.id, direction="long", list_type="holding", quantity=7
    )
    session.add(position)
    await session.flush()
    service = PortfolioService(session)
    await service.reconcile_positions(
        [{"ticker": security.ticker, "quantity": 9}], broker_cash=500
    )
    review = await session.scalar(
        select(ReviewQueueItem).where(
            ReviewQueueItem.item_id == position.id,
            ReviewQueueItem.review_domain == "portfolio_data",
        )
    )
    assert review is not None and review.item_type == "position"
    review_id = review.id
    await service.reconcile_positions(
        [{"ticker": security.ticker, "quantity": 10}], broker_cash=500
    )
    assert (
        await session.scalar(
            select(ReviewQueueItem).where(ReviewQueueItem.item_id == position.id)
        )
    ).id == review_id
    await session.refresh(position)
    assert position.quantity == 7
    response = await ReviewService(session)._serialize(review)
    assert "reconciliation" in response.next_action
    assert "broker" in response.why_now_summary
    assert "thesis" not in response.why_now_summary


async def test_open_holdings_remain_visible_without_profiles_and_statement_scope_is_explicit(
    workflow_session,
):
    session = workflow_session
    entity = Entity(name="Synthetic issuer without a profile", entity_type="company")
    session.add(entity)
    await session.flush()
    security = Security(
        entity_id=entity.id,
        ticker="SYNFULL",
        asset_class="equity",
        instrument_type="common_stock",
    )
    session.add(security)
    await session.flush()
    position = Position(
        security_id=security.id, direction="long", list_type="holding", quantity=2
    )
    ledger = CashLedgerEntry(
        entry_type="deposit",
        amount=100,
        balance_after=100,
        executed_at=datetime.now(UTC),
    )
    session.add_all([position, ledger])
    await session.flush()

    subject = next(
        item
        for item in await get_holding_subjects(session)
        if item.position_id == position.id
    )
    assert subject.entity_id == entity.id and subject.has_profile is False
    service = PortfolioService(session)
    cash = await service.reconcile_positions(
        None, broker_cash=110, create_review_items=False
    )
    assert cash["holdings_scope"] == "none" and not cash["discrepancies"]
    assert cash["cash_discrepancy"]["delta"] == 10
    full = await service.reconcile_positions([], create_review_items=False)
    assert any(item["ticker"] == "SYNFULL" for item in full["discrepancies"])
    partial = await service.reconcile_positions(
        [{"ticker": "OTHER", "quantity": 1}],
        listed_only=True,
        create_review_items=False,
    )
    assert partial["holdings_scope"] == "listed"
    assert {item["ticker"] for item in partial["discrepancies"]} == {"OTHER"}
    assert (
        await session.scalar(
            select(ReviewQueueItem).where(ReviewQueueItem.item_id == position.id)
        )
        is None
    )
    assert position.quantity == 2 and ledger.balance_after == 100


async def test_manual_paper_retry_and_delayed_fill_use_one_order_and_execution_time(
    workflow_session, monkeypatch
):
    session = workflow_session
    entity = Entity(name="Synthetic paper security", entity_type="company")
    session.add(entity)
    await session.flush()
    security = Security(
        entity_id=entity.id,
        ticker="SYNPAPER",
        asset_class="equity",
        instrument_type="common_stock",
    )
    session.add(security)
    await session.flush()
    service = ShadowService(session)
    experiment = await service.create_experiment(
        ShadowExperimentCreate(
            name="Synthetic paper account",
            policy_description="Test a simulated order",
            auto_run=False,
            account_basis="cash_only",
            starting_cash=10_000,
        )
    )
    experiment_id = experiment.id
    opening = experiment.initial_portfolio_state_json["run_details"][
        "manual_equity_marks"
    ]
    assert len(opening) == 1 and opening[0]["equity"] == 10_000
    quote = {
        "price": 20,
        "session": "pre",
        "quote_time": datetime.now(UTC) - timedelta(minutes=1),
    }
    live = AsyncMock(return_value=quote)
    monkeypatch.setattr(MarketDataService, "get_live_price", live)
    request = ShadowOrderCreate(
        ticker="SYNPAPER", side="buy", quantity=1.5, client_request_id=uuid4()
    )
    await service.submit_manual_paper_order(experiment_id, request)
    await service.submit_manual_paper_order(experiment_id, request)
    with pytest.raises(ValueError, match="different order"):
        await service.submit_manual_paper_order(
            experiment_id, request.model_copy(update={"quantity": 2})
        )
    assert live.await_count == 1
    order = (
        await session.scalars(
            select(ShadowOrder).where(ShadowOrder.experiment_id == experiment_id)
        )
    ).one()
    assert order.status == "accepted"
    submitted_at = order.submitted_at
    fetch = AsyncMock(return_value={"SYNPAPER": dict(quote, session="regular")})
    monkeypatch.setattr(MarketDataService, "fetch_quotes", fetch)
    assert await service.refresh_pending_paper_orders() == 0
    quote_time = datetime.now(UTC)
    fetch.return_value = {
        "SYNPAPER": {"price": 21, "session": "regular", "quote_time": quote_time}
    }
    assert await service.refresh_pending_paper_orders() == 1
    assert await service.refresh_pending_paper_orders() == 0
    fill = (
        await session.scalars(
            select(ShadowFill).where(ShadowFill.experiment_id == experiment_id)
        )
    ).one()
    assert submitted_at < fill.quote_time <= fill.filled_at
    assert fill.quantity == 1.5

    async def read_marks():
        account = await service.get_experiment(experiment_id)
        await session.refresh(account)
        return account.final_portfolio_state_json["run_details"]

    # A fill is an event, not evidence that the rest of the account was marked.
    assert (await read_marks())["manual_equity_marks"] == opening
    fetch.return_value["SYNPAPER"].update(currency="USD", reported_symbol="SYNPAPER")
    assert await service.refresh_paper_account_marks() == 1
    marked = (await read_marks())["manual_equity_marks"]
    assert len(marked) == 2
    latest = marked[-1]
    assert datetime.fromisoformat(latest["captured_at"]) >= fill.filled_at
    assert datetime.fromisoformat(latest["prices"][0]["quote_time"]) == quote_time
    assert latest["equity"] == pytest.approx(
        float(fill.cash_after) + 1.5 * 21, abs=0.01
    )
    await service.refresh_paper_account_marks()
    assert (await read_marks())["manual_equity_marks"] == marked
    fetch.return_value["SYNPAPER"]["currency"] = "KRW"
    await service.refresh_paper_account_marks()
    incomplete = await read_marks()
    assert incomplete["manual_equity_status"]["status"] == "incomplete"
    assert incomplete["manual_equity_marks"] == marked


async def test_review_domain_migration_preserves_concerns_and_only_backfills_known_producers(
    workflow_session,
):
    session = workflow_session
    await session.execute(
        text(
            "CREATE TEMP TABLE review_queue_items (id UUID PRIMARY KEY, item_type TEXT NOT NULL, trigger_reason TEXT NOT NULL, status TEXT NOT NULL, priority_score FLOAT NOT NULL)"
        )
    )
    examples = [
        ("mailbox_identity", "Synthetic identity hold", "portfolio_data"),
        ("mailbox_receipt", "Synthetic unverified receipt", "portfolio_data"),
        ("corporate_action", "Synthetic corporate action", "portfolio_data"),
        ("account_transfer", "Synthetic transfer", "portfolio_data"),
        ("reconciliation", "Synthetic cash discrepancy", "portfolio_data"),
        (
            "position",
            "Reconciliation discrepancy for SYN: book=7 vs broker=9 (quantity_mismatch)",
            "portfolio_data",
        ),
        ("position", "Holding lacks a strong accepted state.", "investment"),
        ("position", "Unclassified historical position concern", "investment"),
        ("raw_evidence", "Synthetic investment disclosure", "investment"),
        (
            "entity",
            "Reconciliation discrepancy for SYN: book=7 vs broker=9 (quantity_mismatch)",
            "investment",
        ),
    ]
    expected = {}
    for kind, reason, domain in examples:
        key = uuid4()
        expected[key] = (kind, reason, "in_review", 95.0, domain)
        await session.execute(
            text(
                "INSERT INTO review_queue_items VALUES (:id, :kind, :reason, 'in_review', 95.0)"
            ),
            {"id": key, "kind": kind, "reason": reason},
        )
    spec = importlib.util.spec_from_file_location(
        "review_domain_upgrade",
        Path(__file__).parents[1] / "alembic/versions/d9f6b3c1a854_review_domains.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def upgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()

    await (await session.connection()).run_sync(upgrade)
    rows = (
        await session.execute(
            text(
                "SELECT id, item_type, trigger_reason, status, priority_score, review_domain FROM review_queue_items"
            )
        )
    ).all()
    assert {row.id: tuple(row)[1:] for row in rows} == expected


@pytest.mark.parametrize("node_type", ["fact", "claim"])
async def test_subject_evidence_and_claim_review_retain_the_original_receipt(
    workflow_session, node_type
):
    session = workflow_session
    raw, source_item = await source_records(session)
    entity = Entity(name="Synthetic provenance subject", entity_type="company")
    session.add(entity)
    values = dict(
        source_item_id=source_item.id,
        statement="Synthetic source statement",
        confidence=0.8,
        tier="hard_fact",
        importance="high",
        directness="primary",
        novelty="breaking",
        contradiction_role="neutral",
    )
    node = (
        Fact(fact_type="financial", **values)
        if node_type == "fact"
        else Claim(claim_type="forecast", **values)
    )
    session.add(node)
    await session.flush()
    session.add(
        Edge(
            source_type=node_type,
            source_id=node.id,
            target_type="entity",
            target_id=entity.id,
            relationship_type="mentions",
        )
    )
    await session.flush()
    evidence = await ProfileService(session)._recent_evidence("entity", entity.id)
    assert len(evidence) == 1
    assert evidence[0].id == node.id and evidence[0].source_evidence_id == raw.id
    assert evidence[0].source_evidence_id != evidence[0].id
    if node_type == "claim":
        record = SourceClaimRecord(
            source_id=raw.source_id,
            claim_id=node.id,
            claim_time=datetime.now(UTC),
            assessment="pending",
        )
        session.add(record)
        await session.flush()
        navigation = await ReviewService(session)._navigation_context(
            ReviewQueueItem(item_type="source_claim_record", item_id=record.id)
        )
        assert navigation == {"evidence_id": raw.id}


async def test_backfilled_forecast_can_use_dated_outcomes_from_before_discovery(
    workflow_session,
):
    session = workflow_session
    raw, item = await source_records(session)
    entity = Entity(name="Synthetic archived earnings issuer", entity_type="company")
    session.add(entity)
    target = datetime(2026, 10, 29, tzinfo=UTC)
    discovery = target + timedelta(days=10)
    values = dict(
        source_item_id=item.id,
        confidence=0.7,
        tier="signal",
        importance="high",
        directness="secondary",
        novelty="historical",
        contradiction_role="neutral",
        ingest_time=discovery,
        created_at=discovery,
    )
    forecast = Claim(
        claim_type="forecast",
        statement="Expects Q3 earnings to beat consensus",
        valid_until=target,
        **values,
    )
    dated = Fact(
        fact_type="financial",
        statement="Q3 earnings beat consensus",
        event_time=target,
        **values,
    )
    undated = Fact(
        fact_type="financial",
        statement="An undated report suggests a different result",
        **values,
    )
    session.add_all([forecast, dated, undated])
    await session.flush()
    session.add_all(
        [
            Edge(
                source_type=kind,
                source_id=node.id,
                target_type="entity",
                target_id=entity.id,
                relationship_type="about",
            )
            for kind, node in (("claim", forecast), ("fact", dated), ("fact", undated))
        ]
    )
    record = SourceClaimRecord(
        source_id=raw.source_id,
        claim_id=forecast.id,
        claim_time=discovery,
        assessment="pending",
    )
    session.add(record)
    await session.flush()
    bundle = await SourceService(session)._source_claim_assessment_bundle(record.id)
    assert bundle["due_at"] == discovery
    assert [item["id"] for item in bundle["follow_up_evidence"]] == [str(dated.id)]
    recent = Fact(
        fact_type="financial",
        statement="Recent material result",
        event_time=datetime.now(UTC) - timedelta(minutes=10),
        **values,
    )
    session.add(recent)
    await session.flush()
    session.add(
        Edge(
            source_type="fact",
            source_id=recent.id,
            target_type="entity",
            target_id=entity.id,
            relationship_type="about",
        )
    )
    await session.flush()
    holdings = [
        {
            "ticker": "ARCHIVE",
            "entity_id": str(entity.id),
            "security_id": str(uuid4()),
            "position_id": str(uuid4()),
        },
        {
            "ticker": "NO-LINK",
            "entity_id": str(uuid4()),
            "security_id": str(uuid4()),
            "position_id": str(uuid4()),
        },
    ]
    retrieval = RetrievalService(session)
    selected = await retrieval._recent_holding_evidence(holdings, limit=10)
    assert [row.id for row in selected] == [recent.id]
    assert await retrieval._selected_holding_evidence(holdings, {recent.id}) == [
        {"ticker": "ARCHIVE", "linked_evidence_count": 1},
        {"ticker": "NO-LINK", "linked_evidence_count": 0},
    ]


async def test_cash_only_shadow_uses_its_own_starting_value_and_cannot_learn_from_late_start(
    workflow_session, monkeypatch
):
    session = workflow_session
    entity = Entity(name="Synthetic baseline issuer", entity_type="company")
    session.add(entity)
    await session.flush()
    security = Security(
        entity_id=entity.id,
        ticker="SYNBASE",
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
            quantity=2,
            current_price=25,
            market_value=50,
        )
    )
    await session.flush()
    monkeypatch.setattr(MarketDataService, "fetch_quotes", AsyncMock(return_value={}))
    service = ShadowService(session)
    now = datetime.now(UTC)
    experiment = await service.create_experiment(
        ShadowExperimentCreate(
            name="Synthetic late cash-only experiment",
            policy_description="Observe a cash baseline",
            auto_run=True,
            start_point=now - timedelta(days=1),
            end_point=now - timedelta(seconds=2),
            account_basis="cash_only",
            starting_cash=10_000,
        )
    )
    completed = await service.run_experiment(experiment.id)
    response = ShadowExperimentResponse.model_validate(
        await service.serialize_experiment(completed)
    )
    assert response.run_status == "completed"
    assert response.result.shadow_return == 0
    assert response.result.actual_return == 0
    assert response.result.alpha == 0
    assert response.run_details["observation_window"]["calibration_allowed"] is False


async def test_issuer_symbol_agreement_prevents_split_evidence_and_conflicting_merges(
    workflow_session,
):
    session = workflow_session
    canonical = Entity(name="Fixture Semiconductor Corporation", entity_type="company")
    archived = Entity(
        name="Fixture Semiconductor Corporation (SYNISS)", entity_type="company"
    )
    conflicting = Entity(name="Different Issuer (SYNISS)", entity_type="company")
    session.add_all([canonical, archived, conflicting])
    await session.flush()
    session.add(
        Security(
            entity_id=canonical.id,
            ticker="SYNISS",
            asset_class="equity",
            instrument_type="common_stock",
        )
    )
    await session.flush()
    extractor = ExtractionWorker(session)
    assert (
        await extractor._get_or_create_subject(archived.name, "entity") == canonical.id
    )
    assert (
        await extractor._get_or_create_subject(conflicting.name, "entity")
        == conflicting.id
    )
    proposed = await EntityHygieneService(session)._merge_duplicate_entities(
        dry_run=True
    )
    match = next(row for row in proposed if row["id"] == str(archived.id))
    assert match["target_id"] == str(canonical.id)
    assert not any(row["id"] == str(conflicting.id) for row in proposed)
    assert await session.get(Entity, archived.id) is not None
    _, receipt = await source_records(session)
    claim = Claim(
        source_item_id=receipt.id,
        claim_type="forecast",
        statement="Fixture archived forecast",
        confidence=0.7,
        tier="signal",
        importance="high",
        directness="secondary",
        novelty="historical",
        contradiction_role="neutral",
    )
    session.add(claim)
    await session.flush()
    session.add(
        Edge(
            source_type="claim",
            source_id=claim.id,
            target_type="entity",
            target_id=archived.id,
            relationship_type="about",
        )
    )
    await session.flush()
    await EntityHygieneService(session)._merge_duplicate_entities(dry_run=False)
    target = await session.scalar(
        select(Edge.target_id).where(
            Edge.source_id == claim.id, Edge.target_type == "entity"
        )
    )
    assert target == canonical.id
    assert await session.get(Entity, conflicting.id) is not None


@pytest.mark.parametrize("new_type", ["entity", "theme"])
async def test_subject_retarget_preserves_relationship_identity_and_handles_collisions(
    workflow_session, new_type
):
    session = workflow_session
    original, canonical, third, receipt = uuid4(), uuid4(), uuid4(), uuid4()

    def edge(source_type, source, target_type, target, relationship, **kwargs):
        return Edge(
            source_type=source_type,
            source_id=source,
            target_type=target_type,
            target_id=target,
            relationship_type=relationship,
            **kwargs,
        )

    retained = edge(
        new_type,
        canonical,
        "entity",
        third,
        "contextualizes",
        confidence=0.5,
        reasoning="original canonical rationale",
        properties_json={"origin": "canonical"},
    )
    duplicate = edge(
        "entity",
        original,
        "entity",
        third,
        "contextualizes",
        confidence=0.8,
        reasoning="stronger archived rationale",
        properties_json={"origin": "archived", "receipt": "older"},
    )
    moved = edge("claim", receipt, "entity", original, "about")
    collapsed = [
        edge("entity", original, new_type, canonical, "contextualizes"),
        edge(new_type, canonical, "entity", original, "contextualizes"),
        edge("entity", original, "entity", original, "contextualizes"),
    ]
    preexisting_self = edge(new_type, canonical, new_type, canonical, "contextualizes")
    session.add_all([retained, duplicate, moved, preexisting_self, *collapsed])
    await session.flush()
    affected_ids = {duplicate.id, moved.id, *(item.id for item in collapsed)}
    all_ids = affected_ids | {retained.id, preexisting_self.id}
    duplicate_id = duplicate.id
    service = GraphEdgeStateService(session)
    assert (
        await service.retarget_node_edges(
            old_type="entity",
            old_id=original,
            new_type=new_type,
            new_id=canonical,
            actor="entity_hygiene",
        )
        == 5
    )
    assert (
        await service.retarget_node_edges(
            old_type="entity",
            old_id=original,
            new_type=new_type,
            new_id=canonical,
            actor="entity_hygiene",
        )
        == 0
    )
    rows = (await session.scalars(select(Edge).where(Edge.id.in_(all_ids)))).all()
    assert {item.id for item in rows} == {retained.id, moved.id, preexisting_self.id}
    assert (moved.target_type, moved.target_id) == (new_type, canonical)
    assert (
        retained.confidence == 0.8
        and retained.reasoning == "stronger archived rationale"
    )
    assert retained.properties_json == {"origin": "canonical", "receipt": "older"}
    mutations = (
        await session.scalars(
            select(KnowledgeMutation).where(
                KnowledgeMutation.node_type == "edge",
                KnowledgeMutation.node_id.in_(affected_ids),
            )
        )
    ).all()
    assert len(mutations) == 5
    merge = next(item for item in mutations if item.node_id == duplicate_id)
    assert merge.metadata_json["canonical_edge_id"] == str(retained.id)
    assert merge.metadata_json["before"]["properties"]["origin"] == "archived"


async def test_autonomous_reviews_are_separate_without_rewriting_saved_turns(
    workflow_session,
):
    service = AgentService(workflow_session)
    manual, archived, autonomous = uuid4(), uuid4(), uuid4()

    async def save(session_id, message, reason=None):
        await service._persist_conversation_exchange(
            session_id=session_id,
            user_message=message,
            user_subject_id=PORTFOLIO_SUBJECT_ID,
            user_subject_type="portfolio",
            assistant_message="Saved evidence finding.",
            assistant_subject_id=PORTFOLIO_SUBJECT_ID,
            assistant_subject_type="portfolio",
            assistant_metadata={},
            autonomous_reason=reason,
        )

    await save(manual, "What happened recently?")
    await save(
        archived,
        "Autonomous review for SYN, prompted by this unresolved review signal: Verify the archived catalyst. Determine whether the supplied evidence can help.",
    )
    await save(
        autonomous,
        "A newly saved review task",
        "A material evidence gap became reviewable.",
    )
    listing = await service.list_conversations()
    assert manual in {item.session_id for item in listing.conversations}
    assert not {archived, autonomous} & {
        item.session_id for item in listing.conversations
    }
    reports = {item.session_id: item for item in listing.autonomous_reviews}
    assert reports[archived].autonomous_reason == "Verify the archived catalyst"
    assert (
        reports[autonomous].autonomous_reason
        == "A material evidence gap became reviewable."
    )
    assert reports[autonomous].title == "Portfolio · Autonomous review"
    with pytest.raises(ValueError, match="conversation_not_found"):
        await service.update_conversation(autonomous, title="A user chat")


async def source_records(session):
    source = Source(name="Synthetic workflow source", source_type="manual")
    session.add(source)
    await session.flush()
    raw = RawEvidence(
        source_id=source.id, source_item_type="manual_note", title="Synthetic original"
    )
    session.add(raw)
    await session.flush()
    source_item = SourceItem(
        raw_evidence_id=raw.id, source_id=source.id, processing_status="completed"
    )
    session.add(source_item)
    await session.flush()
    return raw, source_item
