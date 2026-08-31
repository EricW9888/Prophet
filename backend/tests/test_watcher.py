import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa

from investos.db import async_session_maker, engine
from investos.models.evidence import RawEvidence
from investos.models.source import Source
from investos.models.watcher import ActiveWatcher, WatcherEvidenceEvaluation
from investos.services.watcher import WatcherService
from investos.services.watcher_evidence import WatcherEvidenceService


class _FakeRegisterSession:
    def __init__(self):
        self.added = None
        self.committed = False

    def add(self, obj):
        self.added = obj

    async def commit(self):
        self.committed = True

    async def refresh(self, _obj):
        return None


class _FakeEvalSession:
    def __init__(self):
        self.committed = False

    async def execute(self, _stmt):
        return _FakeScalarRows([])

    async def commit(self):
        self.committed = True


class _FakeScalarRows:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class _FakeWatcherQuerySession:
    def __init__(self, rows):
        self.rows = rows
        self.added = None
        self.committed = False
        self.flushed = False

    async def execute(self, _stmt):
        return _FakeScalarRows(self.rows)

    def add(self, obj):
        self.added = obj

    async def flush(self):
        self.flushed = True

    async def commit(self):
        self.committed = True

    async def refresh(self, _obj):
        return None


def _watcher(**overrides):
    data = {
        "id": uuid4(),
        "source": "chat",
        "source_id": uuid4(),
        "ticker": "MEMB",
        "entity_id": None,
        "condition_type": "earnings_release",
        "condition_params_json": {"threshold": 0.0},
        "objective": "Use Memory Beta's NAND commentary as proxy for MEMA competitive environment.",
        "adjustment_plan": "If MEMB guides NAND pricing down, reduce combined memory exposure.",
        "deadline": None,
        "is_active": True,
        "status": "pending",
        "action_taken_json": None,
        "created_at": datetime(2026, 6, 1, tzinfo=UTC),
    }
    data.update(overrides)
    return ActiveWatcher(**data)


async def test_register_price_watcher_requires_numeric_threshold():
    session = _FakeRegisterSession()
    watcher = await WatcherService(session).register_watcher(
        source="chat",
        ticker="auto",
        condition_type="price_above",
        condition_params={"threshold": None},
        objective="Watch AUTO breakout",
        adjustment_plan="Revisit the thesis",
    )

    assert session.committed is True
    assert watcher is session.added
    assert watcher.ticker == "AUTO"
    assert watcher.status == "failed"
    assert watcher.is_active is False
    assert "missing numeric threshold" in watcher.trigger_detail


async def test_register_price_watcher_normalizes_threshold():
    watcher = await WatcherService(_FakeRegisterSession()).register_watcher(
        source="chat",
        ticker="auto",
        condition_type="price_below",
        condition_params={"threshold": "250.5"},
        objective="Watch AUTO downside",
        adjustment_plan="Revisit sizing",
    )

    assert watcher.status == "pending"
    assert watcher.is_active is True
    assert watcher.condition_params_json["threshold"] == 250.5


async def test_register_watcher_reuses_semantic_duplicate():
    existing = _watcher(created_at=datetime(2026, 6, 2, tzinfo=UTC))
    session = _FakeWatcherQuerySession([existing])

    watcher = await WatcherService(session).register_watcher(
        source="research_loop",
        ticker="memb",
        condition_type="earnings_release",
        condition_params={"threshold": 0},
        objective="Use Memory Beta's NAND commentary as proxy for MEMA competitive environment.",
        adjustment_plan="If MEMB guides NAND pricing down, reduce combined memory exposure.",
        deadline=datetime(2026, 6, 5, tzinfo=UTC),
    )

    assert watcher is existing
    assert session.added is None
    assert session.committed is False


async def test_deduplicate_active_watchers_supersedes_semantic_duplicates(monkeypatch):
    newest = _watcher(created_at=datetime(2026, 6, 3, tzinfo=UTC))
    older = _watcher(
        source="research_loop",
        source_id=uuid4(),
        condition_params_json={"threshold": 0.05},
        objective="Review Memory Beta earnings for memory-cycle read-through.",
        adjustment_plan="Change memory exposure only if guidance changes.",
        created_at=datetime(2026, 6, 2, tzinfo=UTC),
        deadline=datetime(2026, 6, 10, tzinfo=UTC),
    )
    distinct = _watcher(
        condition_type="price_below",
        condition_params_json={"threshold": 900.0},
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    session = _FakeWatcherQuerySession([newest, older, distinct])
    monkeypatch.setattr(
        "investos.services.watcher.AgentActionLogService.append", lambda **_kwargs: None
    )

    result = await WatcherService(session).deduplicate_active_watchers()

    assert result["scanned"] == 3
    assert result["duplicate_group_count"] == 1
    assert result["deduplicated_count"] == 1
    assert newest.is_active is True
    assert distinct.is_active is True
    assert older.is_active is False
    assert older.status == "superseded"
    assert str(newest.id) in older.trigger_detail
    assert session.committed is True
    assert session.flushed is True


def test_watcher_signal_identity_ignores_reworded_nonprice_rationale():
    first = _watcher(
        condition_params_json={"threshold": 0.0},
        objective="Use Memory Beta earnings to check NAND demand.",
        adjustment_plan="Review the combined memory position.",
    )
    second = _watcher(
        entity_id=uuid4(),
        condition_params_json={"threshold": 0.15},
        objective="Read Memory Beta guidance through to MEMA pricing.",
        adjustment_plan="Trim only if the memory-cycle evidence weakens.",
    )

    assert WatcherService._semantic_key(first) == WatcherService._semantic_key(second)


def test_watcher_signal_identity_preserves_distinct_open_ended_catalyst_tests():
    hbm_watch = _watcher(
        condition_type="hbm_revenue_confirmation",
        condition_params_json={},
        objective="Check whether HBM revenue confirms the memory-cycle bull case.",
        adjustment_plan="Increase confidence only if guidance and backlog both confirm.",
    )
    debt_watch = _watcher(
        condition_type="balance_sheet_debt_refinancing",
        condition_params_json={},
        objective="Check whether debt refinancing pressure constrains the thesis.",
        adjustment_plan="Lower confidence if interest coverage or liquidity weakens.",
    )

    assert WatcherService._semantic_key(hbm_watch) != WatcherService._semantic_key(
        debt_watch
    )


def test_watcher_signal_identity_preserves_distinct_absolute_price_levels():
    lower = _watcher(
        condition_type="price_below",
        condition_params_json={"threshold": 900},
        objective="Watch first downside level.",
    )
    severe = _watcher(
        condition_type="price_below",
        condition_params_json={"threshold": 800},
        objective="Watch severe downside level.",
    )

    assert WatcherService._semantic_key(lower) != WatcherService._semantic_key(severe)


def test_watcher_response_includes_countdown_state():
    now = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
    watcher = _watcher(deadline=now + timedelta(hours=1, minutes=30))

    payload = WatcherService.to_response(watcher, now=now)

    assert payload["countdown_seconds"] == 5400
    assert payload["is_overdue"] is False
    assert payload["reminder_kind"] == "deadline_and_condition"
    assert payload["deadline"] == now + timedelta(hours=1, minutes=30)


def test_watcher_response_marks_pending_deadline_overdue():
    now = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
    watcher = _watcher(deadline=now - timedelta(minutes=5))

    payload = WatcherService.to_response(watcher, now=now)

    assert payload["countdown_seconds"] == 0
    assert payload["is_overdue"] is True


async def test_evaluate_disables_legacy_invalid_price_watcher(monkeypatch):
    watcher = SimpleNamespace(
        id=uuid4(),
        ticker="AUTO",
        condition_type="price_above",
        condition_params_json={"threshold": None},
        objective="Legacy bad watcher",
        adjustment_plan="Ignore",
        deadline=None,
        status="pending",
        is_active=True,
        last_checked_at=None,
        triggered_at=None,
        trigger_detail=None,
    )
    service = WatcherService(_FakeEvalSession())

    async def list_active():
        return [watcher]

    async def get_live_price(_self, _ticker):
        return {"price": 300.0}

    service.list_active = list_active
    monkeypatch.setattr(
        "investos.services.market_data.MarketDataService.get_live_price", get_live_price
    )
    monkeypatch.setattr(
        "investos.services.watcher.AgentActionLogService.append", lambda **_kwargs: None
    )

    count = await service.evaluate_watchers()

    assert count == 0
    assert watcher.status == "failed"
    assert watcher.is_active is False
    assert watcher.last_checked_at is not None
    assert "missing numeric threshold" in watcher.trigger_detail


async def test_triggered_watcher_enqueues_owner_notification(monkeypatch):
    watcher = SimpleNamespace(
        id=uuid4(),
        ticker="AUTO",
        condition_type="price_above",
        condition_params_json={"threshold": 250},
        objective="Review the breakout",
        adjustment_plan="Reassess the evidence",
        deadline=None,
        status="pending",
        is_active=True,
        last_checked_at=None,
        triggered_at=None,
        trigger_detail=None,
    )
    service = WatcherService(_FakeEvalSession())
    enqueued: list = []

    async def list_active():
        return [watcher]

    async def get_live_price(_self, _ticker):
        return {"price": 300.0}

    async def enqueue(_self, transitioned_watcher):
        enqueued.append(transitioned_watcher)
        return 1

    service.list_active = list_active
    monkeypatch.setattr(
        "investos.services.market_data.MarketDataService.get_live_price", get_live_price
    )
    monkeypatch.setattr(
        "investos.services.watcher.PushNotificationService.enqueue_watch_transition",
        enqueue,
    )
    monkeypatch.setattr(
        "investos.services.watcher.AgentActionLogService.append", lambda **_kwargs: None
    )

    count = await service.evaluate_watchers()

    assert count == 1
    assert watcher.status == "triggered"
    assert watcher.is_active is False
    assert enqueued == [watcher]


async def test_deadline_only_reminder_expires_and_enqueues_notification(monkeypatch):
    watcher = SimpleNamespace(
        id=uuid4(),
        ticker=None,
        condition_type="reminder",
        condition_params_json={},
        objective="Review the pending evidence",
        adjustment_plan="Reconcile the accepted view",
        deadline=datetime.now(UTC) - timedelta(minutes=1),
        status="pending",
        is_active=True,
        last_checked_at=None,
        triggered_at=None,
        trigger_detail=None,
    )
    service = WatcherService(_FakeEvalSession())
    enqueued: list = []

    async def list_active():
        return [watcher]

    async def enqueue(_self, transitioned_watcher):
        enqueued.append(transitioned_watcher)
        return 1

    service.list_active = list_active
    monkeypatch.setattr(
        "investos.services.watcher.PushNotificationService.enqueue_watch_transition",
        enqueue,
    )
    monkeypatch.setattr(
        "investos.services.watcher.AgentActionLogService.append", lambda **_kwargs: None
    )

    count = await service.evaluate_watchers()

    assert count == 1
    assert watcher.status == "expired"
    assert watcher.is_active is False
    assert watcher.triggered_at is not None
    assert enqueued == [watcher]


async def test_open_ended_watch_is_not_marked_checked_by_price_poll(monkeypatch):
    watcher = _watcher(last_checked_at=None)
    service = WatcherService(_FakeEvalSession())

    async def list_active():
        return [watcher]

    async def get_live_price(_self, _ticker):
        return {"price": 300.0}

    async def no_retry(*, limit):
        assert limit == 6
        return 0

    service.list_active = list_active
    service.retry_deferred_evidence_evaluations = no_retry
    monkeypatch.setattr(
        "investos.services.market_data.MarketDataService.get_live_price",
        get_live_price,
    )

    count = await service.evaluate_watchers()

    assert count == 0
    assert watcher.last_checked_at is None
    assert watcher.status == "pending"
    assert watcher.is_active is True


def _candidate_bundle(*, known_at: datetime | None) -> dict:
    return {
        "raw_evidence_id": str(uuid4()),
        "source": {
            "name": "Issuer filing",
            "type": "filing",
            "public_time": known_at,
        },
        "objects": [
            {
                "type": "fact",
                "id": str(uuid4()),
                "text": "Memory Beta reported NAND pricing above its prior guide.",
                "known_at": known_at,
            }
        ],
    }


async def _prepare_evidence_service(service, watcher, evaluation, candidates):
    async def matching(**_kwargs):
        return [watcher]

    async def ensure(_watchers, _raw_evidence_id):
        return {watcher.id: evaluation}

    async def evidence(_raw_evidence_id):
        return candidates

    service._matching_watchers = matching
    service._ensure_evaluations = ensure
    service._evidence_candidates = evidence


async def test_source_backed_evidence_triggers_once_with_attribution(monkeypatch):
    watcher = _watcher(created_at=datetime(2026, 6, 1, tzinfo=UTC))
    evaluation = SimpleNamespace(
        id=uuid4(),
        watcher_id=watcher.id,
        status="pending",
        evidence_refs_json=[],
        detail=None,
        confidence=None,
        error=None,
        evaluated_at=None,
        updated_at=None,
    )
    candidates = _candidate_bundle(known_at=datetime(2026, 6, 2, tzinfo=UTC))
    evidence_ref = candidates["objects"][0]
    service = WatcherEvidenceService(_FakeEvalSession())
    await _prepare_evidence_service(service, watcher, evaluation, candidates)
    enqueued = []

    async def evaluate(**_kwargs):
        return {
            "evaluations": [
                {
                    "watcher_id": str(watcher.id),
                    "outcome": "triggered",
                    "evidence_refs": [
                        {"type": evidence_ref["type"], "id": evidence_ref["id"]}
                    ],
                    "confidence": 0.94,
                    "detail": "The filing directly reports the watched NAND pricing result.",
                }
            ]
        }

    async def enqueue(_self, transitioned_watcher):
        enqueued.append(transitioned_watcher.id)
        return 1

    monkeypatch.setattr("investos.services.watcher_evidence.call_llm_json", evaluate)
    monkeypatch.setattr(
        "investos.services.watcher_evidence.PushNotificationService.enqueue_watch_transition",
        enqueue,
    )
    monkeypatch.setattr(
        "investos.services.watcher_evidence.AgentActionLogService.append",
        lambda **_kwargs: None,
    )

    count = await service.evaluate_new_evidence(
        subject_id=uuid4(),
        subject_type="entity",
        raw_evidence_id=uuid4(),
    )
    duplicate_count = await service.evaluate_new_evidence(
        subject_id=uuid4(),
        subject_type="entity",
        raw_evidence_id=uuid4(),
    )

    assert count == 1
    assert duplicate_count == 0
    assert watcher.status == "triggered"
    assert watcher.is_active is False
    assert evaluation.status == "triggered"
    assert evaluation.evidence_refs_json == [
        {"type": evidence_ref["type"], "id": evidence_ref["id"]}
    ]
    assert watcher.action_taken_json["trigger_evidence"]["evidence_refs"]
    assert enqueued == [watcher.id]


async def test_irrelevant_evidence_records_no_match_without_trigger(monkeypatch):
    watcher = _watcher(created_at=datetime(2026, 6, 1, tzinfo=UTC))
    evaluation = SimpleNamespace(
        id=uuid4(),
        watcher_id=watcher.id,
        status="pending",
        evidence_refs_json=[],
        detail=None,
        confidence=None,
        error=None,
        evaluated_at=None,
        updated_at=None,
    )
    candidates = _candidate_bundle(known_at=datetime(2026, 6, 2, tzinfo=UTC))
    service = WatcherEvidenceService(_FakeEvalSession())
    await _prepare_evidence_service(service, watcher, evaluation, candidates)

    async def evaluate(**_kwargs):
        return {
            "evaluations": [
                {
                    "watcher_id": str(watcher.id),
                    "outcome": "no_match",
                    "evidence_refs": [],
                    "confidence": 0.98,
                    "detail": "The source does not report an earnings release.",
                }
            ]
        }

    monkeypatch.setattr("investos.services.watcher_evidence.call_llm_json", evaluate)

    count = await service.evaluate_new_evidence(
        subject_id=uuid4(),
        subject_type="entity",
        raw_evidence_id=uuid4(),
    )

    assert count == 0
    assert evaluation.status == "no_match"
    assert watcher.is_active is True
    assert watcher.last_checked_at is not None


async def test_provider_failure_defers_without_false_check(monkeypatch):
    watcher = _watcher(last_checked_at=None)
    evaluation = SimpleNamespace(
        id=uuid4(),
        watcher_id=watcher.id,
        status="pending",
        evidence_refs_json=[],
        detail=None,
        confidence=None,
        error=None,
        evaluated_at=None,
        updated_at=None,
    )
    candidates = _candidate_bundle(known_at=datetime(2026, 6, 2, tzinfo=UTC))
    service = WatcherEvidenceService(_FakeEvalSession())
    await _prepare_evidence_service(service, watcher, evaluation, candidates)

    async def fail(**_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("investos.services.watcher_evidence.call_llm_json", fail)

    count = await service.evaluate_new_evidence(
        subject_id=uuid4(),
        subject_type="entity",
        raw_evidence_id=uuid4(),
    )

    assert count == 0
    assert evaluation.status == "deferred"
    assert "provider unavailable" in evaluation.error
    assert watcher.last_checked_at is None
    assert watcher.is_active is True


async def test_stale_source_cannot_trigger_future_watch(monkeypatch):
    watcher = _watcher(created_at=datetime(2026, 6, 1, tzinfo=UTC))
    evaluation = SimpleNamespace(
        id=uuid4(),
        watcher_id=watcher.id,
        status="pending",
        evidence_refs_json=[],
        detail=None,
        confidence=None,
        error=None,
        evaluated_at=None,
        updated_at=None,
    )
    candidates = _candidate_bundle(known_at=datetime(2020, 6, 2, tzinfo=UTC))
    evidence_ref = candidates["objects"][0]
    service = WatcherEvidenceService(_FakeEvalSession())
    await _prepare_evidence_service(service, watcher, evaluation, candidates)

    async def evaluate(**_kwargs):
        return {
            "evaluations": [
                {
                    "watcher_id": str(watcher.id),
                    "outcome": "triggered",
                    "evidence_refs": [
                        {"type": evidence_ref["type"], "id": evidence_ref["id"]}
                    ],
                    "confidence": 0.99,
                    "detail": "The condition appears in the source.",
                }
            ]
        }

    monkeypatch.setattr("investos.services.watcher_evidence.call_llm_json", evaluate)

    count = await service.evaluate_new_evidence(
        subject_id=uuid4(),
        subject_type="entity",
        raw_evidence_id=uuid4(),
    )

    assert count == 0
    assert evaluation.status == "no_match"
    assert "lacked timely attributable evidence" in evaluation.detail
    assert watcher.is_active is True


async def test_source_without_verified_public_time_cannot_trigger(monkeypatch):
    watcher = _watcher(created_at=datetime(2026, 6, 1, tzinfo=UTC))
    evaluation = SimpleNamespace(
        id=uuid4(),
        watcher_id=watcher.id,
        status="pending",
        evidence_refs_json=[],
        detail=None,
        confidence=None,
        error=None,
        evaluated_at=None,
        updated_at=None,
    )
    candidates = _candidate_bundle(known_at=None)
    evidence_ref = candidates["objects"][0]
    service = WatcherEvidenceService(_FakeEvalSession())
    await _prepare_evidence_service(service, watcher, evaluation, candidates)

    async def evaluate(**_kwargs):
        return {
            "evaluations": [
                {
                    "watcher_id": str(watcher.id),
                    "outcome": "triggered",
                    "evidence_refs": [
                        {"type": evidence_ref["type"], "id": evidence_ref["id"]}
                    ],
                    "confidence": 0.99,
                    "detail": "The source appears to satisfy the condition.",
                }
            ]
        }

    monkeypatch.setattr("investos.services.watcher_evidence.call_llm_json", evaluate)

    count = await service.evaluate_new_evidence(
        subject_id=uuid4(),
        subject_type="entity",
        raw_evidence_id=uuid4(),
    )

    assert count == 0
    assert evaluation.status == "no_match"
    assert "lacked timely attributable evidence" in evaluation.detail
    assert watcher.is_active is True


@pytest.mark.asyncio(loop_scope="session")
async def test_migrated_schema_owns_watcher_evidence_idempotency() -> None:
    def inspect_schema(connection):
        inspector = sa.inspect(connection)
        constraints = {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints(
                "watcher_evidence_evaluations"
            )
        }
        indexes = {
            index["name"]: tuple(index["column_names"])
            for index in inspector.get_indexes("watcher_evidence_evaluations")
        }
        return constraints, indexes

    async with engine.connect() as connection:
        constraints, indexes = await connection.run_sync(inspect_schema)

    assert constraints.get("uq_watcher_evaluations_watcher_evidence") == (
        "watcher_id",
        "raw_evidence_id",
    )
    assert indexes.get("ix_watcher_evidence_evaluations_watcher_id") == ("watcher_id",)
    assert indexes.get("ix_watcher_evidence_evaluations_raw_evidence_id") == (
        "raw_evidence_id",
    )
    assert indexes.get("ix_watcher_evidence_evaluations_status") == ("status",)


@pytest.mark.asyncio(loop_scope="session")
async def test_concurrent_evidence_evaluation_creation_converges_on_one_row() -> None:
    source_id = uuid4()
    raw_evidence_id = uuid4()
    watcher_id = uuid4()

    async with async_session_maker() as session:
        source = Source(
            id=source_id,
            name=f"watcher-evaluation-test-{source_id}",
            source_type="test",
        )
        session.add(source)
        await session.flush()
        session.add(
            RawEvidence(
                id=raw_evidence_id,
                source_id=source_id,
                source_item_type="test",
            )
        )
        session.add(
            ActiveWatcher(
                id=watcher_id,
                source="test",
                ticker="TEST",
                condition_type="earnings_release",
                condition_params_json={},
                objective="Observe a test event",
                adjustment_plan="No action",
                is_active=True,
                status="pending",
            )
        )
        await session.commit()

    async def ensure_once() -> str:
        async with async_session_maker() as session:
            async with session.begin():
                watcher = await session.get(ActiveWatcher, watcher_id)
                assert watcher is not None
                evaluations = await WatcherEvidenceService(session)._ensure_evaluations(
                    [watcher], raw_evidence_id
                )
                return str(evaluations[watcher_id].id)

    try:
        evaluation_ids = await asyncio.gather(ensure_once(), ensure_once())
        async with async_session_maker() as session:
            count = await session.scalar(
                sa.select(sa.func.count())
                .select_from(WatcherEvidenceEvaluation)
                .where(
                    WatcherEvidenceEvaluation.watcher_id == watcher_id,
                    WatcherEvidenceEvaluation.raw_evidence_id == raw_evidence_id,
                )
            )

        assert len(set(evaluation_ids)) == 1
        assert count == 1
    finally:
        async with async_session_maker() as session:
            await session.execute(
                sa.delete(WatcherEvidenceEvaluation).where(
                    WatcherEvidenceEvaluation.watcher_id == watcher_id
                )
            )
            await session.execute(
                sa.delete(ActiveWatcher).where(ActiveWatcher.id == watcher_id)
            )
            await session.execute(
                sa.delete(RawEvidence).where(RawEvidence.id == raw_evidence_id)
            )
            await session.execute(sa.delete(Source).where(Source.id == source_id))
            await session.commit()
