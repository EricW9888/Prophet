"""Exercise prospective captures through the real risk routes and PostgreSQL."""

import copy
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, func, select
from test_portfolio_risk import frozen_packet

from investos.api.routes.risk import router
from investos.db import async_session_maker, get_session
from investos.models.entity import Entity, Security
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.graph import Edge
from investos.models.knowledge import Claim
from investos.models.notification import PushNotificationEvent
from investos.models.portfolio import Position
from investos.models.quant import ScenarioAnalysis
from investos.models.source import Source
from investos.models.watcher import ActiveWatcher
from investos.services.advisory_policy import decide, settle
from investos.services.advisory_trial import AdvisoryTrialService, case_name
from investos.services.market_data import MarketDataService
from investos.services.portfolio_risk import SCENARIO_NAME, clock, digest

PATH = "/risk/portfolio/advisory-trial"
BENCHMARK = "SYN-MARKET"


class TestClock(datetime):
    __test__ = False
    current = datetime(2025, 5, 21, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)


def market():
    packet = frozen_packet()
    benchmark = frozen_packet(rising=True)["snapshots"]["SYN-A"]
    benchmark["reported_symbol"] = BENCHMARK
    for snap in [*packet["snapshots"].values(), benchmark]:
        last = snap["daily_observations"][-1]
        last["session_open_at"] = last["timestamp"]
    for name in ("SYN-A", "SYN-B"):
        bars = packet["snapshots"][name]["daily_observations"]
        bars[-1]["close"] = bars[-2]["close"] * 0.97
        bars[-1]["adjusted_close"] = bars[-1]["close"]
    return packet, benchmark


def next_session(snapshots, *, close_fraction=0.92):
    result = copy.deepcopy(snapshots)
    for ticker, snap in result.items():
        previous = snap["daily_observations"][-1]
        price = previous["close"]
        opening = clock(previous["timestamp"]) + timedelta(days=1)
        closing = opening + timedelta(hours=6)
        available = closing + timedelta(minutes=1)
        close = price if ticker == BENCHMARK else price * close_fraction
        snap["daily_observations"].append(
            {
                "timestamp": opening.isoformat(),
                "session_open_at": opening.isoformat(),
                "completed_at": closing.isoformat(),
                "available_at": available.isoformat(),
                "open": price * 0.98,
                "close": close,
                "adjusted_close": close,
                "volume": 1000,
            }
        )
        snap["retrieved_at"] = available.isoformat()
    return result


@pytest.mark.asyncio(loop_scope="session")
async def test_paper_trial_uses_live_entry_point_and_frozen_source_clock(monkeypatch):
    packet, benchmark = market()
    snapshots = {**packet["snapshots"], BENCHMARK: benchmark}
    day = clock(packet["cutoff"])
    TestClock.current = day.replace(hour=0)
    monkeypatch.setattr("investos.services.advisory_trial.datetime", TestClock)
    monkeypatch.setattr("investos.services.portfolio_risk.datetime", TestClock)
    monkeypatch.setattr(
        AdvisoryTrialService, "benchmark_ticker", staticmethod(lambda: BENCHMARK)
    )

    async def fetch(_self, tickers):
        return {
            ticker: copy.deepcopy(snapshots[ticker])
            for ticker in tickers
            if ticker in snapshots
        }

    monkeypatch.setattr(MarketDataService, "fetch_signal_snapshots", fetch)
    fixture_ids = {
        model: []
        for model in (
            Edge,
            Claim,
            SourceItem,
            RawEvidence,
            Source,
            Position,
            Security,
            Entity,
        )
    }
    trial_id = None

    async with async_session_maker() as session:

        async def save(row):
            session.add(row)
            await session.flush()
            fixture_ids[type(row)].append(row.id)
            return row

        async def db():
            yield session

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_session] = db
        try:
            subjects = []
            for position in packet["positions"]:
                entity = await save(
                    Entity(name=position["ticker"], entity_type="company")
                )
                security = await save(
                    Security(
                        entity_id=entity.id,
                        ticker=position["ticker"],
                        asset_class="equity",
                        instrument_type="common_stock",
                    )
                )
                await save(
                    Position(
                        security_id=security.id,
                        direction="long",
                        list_type="holding",
                        quantity=10,
                    )
                )
                subjects.append(entity.id)
            source = await save(Source(name="Synthetic source", source_type="official"))

            async def evidence(offset, subject_ids):
                public = day + offset
                stored = public + timedelta(minutes=5)
                raw = await save(
                    RawEvidence(
                        source_id=source.id,
                        source_item_type="official",
                        title="Synthetic issuer disruption",
                        url="https://example.test/notice",
                        content_hash=digest(str(public)),
                        public_time=public,
                        ingest_time=stored,
                        updated_at=stored,
                        created_at=stored,
                    )
                )
                item = await save(
                    SourceItem(
                        raw_evidence_id=raw.id,
                        source_id=source.id,
                        processing_status="completed",
                    )
                )
                for entity_id in subject_ids:
                    claim = await save(
                        Claim(
                            statement="Synthetic issuer exposure to a new operating disruption.",
                            claim_type="risk",
                            sentiment="negative",
                            source_item_id=item.id,
                            confidence=0.9,
                            tier="credible_interpretation",
                            importance="high",
                            directness="primary",
                            novelty="breaking",
                            contradiction_role="neutral",
                            event_time=public,
                            public_time=public,
                            ingest_time=stored,
                            eligible_action_time=public,
                            created_at=stored,
                            updated_at=stored,
                        )
                    )
                    await save(
                        Edge(
                            source_type="claim",
                            source_id=claim.id,
                            target_type="entity",
                            target_id=entity_id,
                            relationship_type="supports",
                            created_at=stored,
                        )
                    )
                return raw

            early = await evidence(-timedelta(hours=1), subjects[:2])
            future = await evidence(timedelta(hours=1), subjects[:2])
            await session.commit()
            watch_count = await session.scalar(
                select(func.count()).select_from(ActiveWatcher)
            )
            outbox_count = await session.scalar(
                select(func.count()).select_from(PushNotificationEvent)
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(PATH)
                response.raise_for_status()
                trial = response.json()["trial"]
                trial_id = trial["id"]
                assert digest(trial["declaration"]) == trial["declaration_hash"]
                assert (await client.post(PATH)).json()["trial"]["id"] == trial_id
                assert not trial["observations"]

                TestClock.current = day
                (await client.post("/risk/portfolio/refresh")).raise_for_status()
                (await client.post("/risk/portfolio/refresh")).raise_for_status()
                trial = (await client.get(PATH)).json()["trial"]
                (observation,) = trial["observations"]
                assert observation["decision"]["candidate_alert"]
                assert not observation["decision"]["benchmark_alert"]
                assert observation["evidence"]["id"] == str(early.id)
                link = PATH + "/receipts/" + observation["id"]
                frozen = (await client.get(link)).json()
                receipt = frozen["receipt"]
                assert digest(receipt) == frozen["receipt_hash"]
                assert {row["evidence_id"] for row in receipt["sources"]} == {
                    str(early.id)
                }
                assert str(future.id) not in str(receipt)
                assert receipt["delivery"] == {
                    "status": "paper_only",
                    "delivered_at": None,
                }
                assert observation["outcome"] is None

                policy = trial["declaration"]["policy"]
                late_source = copy.deepcopy(receipt["sources"])
                late_source[0]["claims"][0]["available_at"] = (
                    day + timedelta(minutes=1)
                ).isoformat()
                assert not decide(
                    receipt["market_packet"],
                    receipt["benchmark"],
                    late_source,
                    policy,
                    set(),
                )["candidate_alert"]
                assert not decide(
                    receipt["market_packet"],
                    receipt["benchmark"],
                    receipt["sources"],
                    policy,
                    {early.content_hash},
                )["candidate_alert"]

                snapshots = next_session(snapshots)
                TestClock.current = day + timedelta(days=1)
                (await client.post("/risk/portfolio/refresh")).raise_for_status()
                scored = (await client.get(link)).json()
                assert scored["receipt_hash"] == frozen["receipt_hash"]
                outcome = scored["outcome"]
                assert digest(outcome) == scored["outcome_hash"]
                assert outcome["status"] == "scored"
                assert outcome["opening_gap_pct"] == pytest.approx(-2)
                assert outcome["hold_return_pct"] == pytest.approx(-8)
                assert outcome["candidate"]["advantage_bps"] > 0
                assert outcome["benchmark"]["missed_warning"]
                assert outcome["benchmark"]["advantage_bps"] == 0

                latest = (await client.get(PATH)).json()["trial"]["observations"][-1]
                newer = (await client.get(PATH + "/receipts/" + latest["id"])).json()[
                    "receipt"
                ]
                late = settle(
                    {**receipt, "detected_at": outcome["paper_open_at"]},
                    newer["market_packet"],
                    outcome["benchmark_mark"],
                    policy,
                )
                assert late["candidate"]["missed_warning"]
                assert not late["candidate"]["acted"]
                rebound = copy.deepcopy(newer["market_packet"])
                for snap in rebound["snapshots"].values():
                    bar = snap["daily_observations"][-1]
                    bar["close"] = bar["adjusted_close"] = (
                        snap["daily_observations"][-2]["close"] * 1.03
                    )
                rising = settle(receipt, rebound, outcome["benchmark_mark"], policy)
                assert rising["candidate"]["unnecessary_alert"]
                assert rising["candidate"]["lost_upside_bps"] > 0
                revised = copy.deepcopy(newer["market_packet"])
                revised["snapshots"]["SYN-A"]["daily_observations"][-1][
                    "adjusted_close"
                ] *= 0.5
                assert (
                    settle(receipt, revised, outcome["benchmark_mark"], policy)[
                        "status"
                    ]
                    == "unavailable"
                )
                assert (await client.post(f"{PATH}/{trial_id}/stop")).json()["trial"][
                    "phase"
                ] == "stopped"
            assert (
                await session.scalar(select(func.count()).select_from(ActiveWatcher))
                == watch_count
            )
            assert (
                await session.scalar(
                    select(func.count()).select_from(PushNotificationEvent)
                )
                == outbox_count
            )
        finally:
            await session.rollback()
            if trial_id:
                await session.execute(
                    delete(ScenarioAnalysis).where(
                        (ScenarioAnalysis.id == UUID(trial_id))
                        | (ScenarioAnalysis.name == case_name(UUID(trial_id)))
                    )
                )
            await session.execute(
                delete(ScenarioAnalysis).where(
                    ScenarioAnalysis.computed_at.in_([day, day + timedelta(days=1)]),
                    ScenarioAnalysis.name == SCENARIO_NAME,
                )
            )
            for model, ids in fixture_ids.items():
                if ids:
                    await session.execute(delete(model).where(model.id.in_(ids)))
            await session.commit()
