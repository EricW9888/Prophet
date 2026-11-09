"""Synthetic record identities exercise the actual PostgreSQL navigation path."""

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from alembic.migration import MigrationContext
from alembic.operations import Operations
from investos.api.routes.graph import get_holding_subjects
from investos.config import settings
from investos.models.catalog import SourceClaimRecord
from investos.models.entity import Entity, Security
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.graph import Edge
from investos.models.knowledge import Claim, Fact
from investos.models.portfolio import CashLedgerEntry, Position
from investos.models.profile import Profile
from investos.models.review import ReviewQueueItem
from investos.models.shadow import ShadowFill, ShadowOrder
from investos.models.source import Source
from investos.schemas.shadow import (
    ShadowExperimentCreate,
    ShadowExperimentResponse,
    ShadowOrderCreate,
)
from investos.services.dashboard import DashboardService
from investos.services.market_data import MarketDataService
from investos.services.portfolio import PortfolioService
from investos.services.profile import ProfileService
from investos.services.retrieval import RetrievalService
from investos.services.review import ReviewService
from investos.services.shadow import ShadowService
from investos.services.source import SourceService


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
