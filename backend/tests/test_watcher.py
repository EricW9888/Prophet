from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from investos.models.watcher import ActiveWatcher
from investos.services.watcher import WatcherService


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
