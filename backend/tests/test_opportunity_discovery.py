from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy import delete, func, select

import investos.services.opportunity as opportunity_module
from investos.db import async_session_maker, engine
from investos.models.benchmark import Benchmark, BenchmarkConstituent
from investos.models.entity import Entity, Security
from investos.models.opportunity import (
    OpportunityCandidate,
    OpportunityCandidateObservation,
    OpportunityDiscoveryRun,
    OpportunityUniverseMember,
)
from investos.models.portfolio import Position
from investos.models.profile import Profile
from investos.models.shadow import ExperimentFamilyState, ShadowExperiment
from investos.schemas.opportunity import OpportunityShadowTestRequest
from investos.services.opportunity import OpportunityDiscoveryService
from investos.services.shadow import SHADOW_DISCOVERY_SCHEMA, ShadowService

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _seed_universe_member() -> tuple[Entity, Security, OpportunityUniverseMember]:
    suffix = uuid4().hex[:8].upper()
    async with async_session_maker() as session:
        entity = Entity(name=f"Opportunity Test {suffix}", entity_type="company")
        session.add(entity)
        await session.flush()
        security = Security(
            entity_id=entity.id,
            ticker=f"O{suffix[:6]}",
            asset_class="equity",
            instrument_type="common_stock",
        )
        session.add(security)
        await session.flush()
        member = OpportunityUniverseMember(
            security_id=security.id,
            entity_id=entity.id,
            priority=1.0,
            source="test",
            metadata_json={},
        )
        session.add(member)
        await session.commit()
        return entity, security, member


async def _cleanup_catalog(
    *,
    entity_id,
    security_id,
    member_id,
    run_ids: list | None = None,
) -> None:
    async with async_session_maker() as session:
        if run_ids:
            await session.execute(
                delete(OpportunityCandidate).where(
                    OpportunityCandidate.run_id.in_(run_ids)
                )
            )
            await session.execute(
                delete(OpportunityDiscoveryRun).where(
                    OpportunityDiscoveryRun.id.in_(run_ids)
                )
            )
        await session.execute(
            delete(OpportunityUniverseMember).where(
                OpportunityUniverseMember.id == member_id
            )
        )
        await session.execute(delete(Security).where(Security.id == security_id))
        await session.execute(delete(Entity).where(Entity.id == entity_id))
        await session.commit()


async def test_migrated_schema_owns_opportunity_uniqueness() -> None:
    expected = {
        "opportunity_universe_members": {
            "uq_opportunity_universe_members_security": ("security_id",)
        },
        "opportunity_discovery_runs": {
            "uq_opportunity_discovery_runs_active_key": ("active_key",)
        },
        "opportunity_candidates": {
            "uq_opportunity_candidates_fingerprint": ("fingerprint",)
        },
        "opportunity_candidate_observations": {
            "uq_opportunity_candidate_observations_candidate_run": (
                "candidate_id",
                "run_id",
            )
        },
    }

    def inspect_constraints(connection):
        inspector = sa.inspect(connection)
        return {
            table_name: {
                constraint["name"]: tuple(constraint["column_names"])
                for constraint in inspector.get_unique_constraints(table_name)
            }
            for table_name in expected
        }

    async with engine.connect() as connection:
        actual = await connection.run_sync(inspect_constraints)

    for table_name, constraints in expected.items():
        for name, columns in constraints.items():
            assert actual[table_name].get(name) == columns


async def test_discovery_context_includes_unowned_security_without_fake_position() -> (
    None
):
    entity, security, member = await _seed_universe_member()
    try:
        async with async_session_maker() as session:
            context = await ShadowService(session).build_discovery_context(
                captured_at=datetime.now(UTC),
                additional_security_ids=[security.id],
            )
            candidate = next(
                item
                for item in context["candidates"]
                if item["security_id"] == str(security.id)
            )
            position_count = await session.scalar(
                select(func.count())
                .select_from(Position)
                .where(Position.security_id == security.id)
            )

        assert candidate["position_id"] is None
        assert candidate["list_type"] == "opportunity_universe"
        assert context["portfolio"]["opportunity_universe_subject_count"] >= 1
        assert position_count == 0
    finally:
        await _cleanup_catalog(
            entity_id=entity.id,
            security_id=security.id,
            member_id=member.id,
        )


async def test_universe_import_is_additive_deduplicated_and_source_backed(
    monkeypatch,
) -> None:
    created: dict[str, list] = {
        "entities": [],
        "securities": [],
        "positions": [],
        "profiles": [],
        "constituents": [],
        "benchmarks": [],
        "members": [],
    }
    suffix = uuid4().hex[:6].upper()
    observed_at = datetime(2026, 2, 1, tzinfo=UTC)
    try:
        async with async_session_maker() as session:
            entities = [
                Entity(name=f"Universe Import {suffix} {index}", entity_type="company")
                for index in range(4)
            ]
            session.add_all(entities)
            await session.flush()
            created["entities"] = [entity.id for entity in entities]
            securities = [
                Security(
                    entity_id=entity.id,
                    ticker=f"U{suffix}{index}",
                    asset_class="equity",
                    instrument_type="common_stock",
                    is_active=index != 3,
                )
                for index, entity in enumerate(entities)
            ]
            session.add_all(securities)
            await session.flush()
            created["securities"] = [security.id for security in securities]

            positions = [
                Position(
                    security_id=securities[0].id,
                    direction="long",
                    list_type="holding",
                    added_at=observed_at,
                ),
                Position(
                    security_id=securities[3].id,
                    direction="long",
                    list_type="watchlist",
                    added_at=observed_at,
                ),
            ]
            session.add_all(positions)
            profiles = [
                Profile(
                    subject_type="entity",
                    subject_id=entities[0].id,
                    executive_summary="An older research profile.",
                    version=1,
                    updated_at=datetime(2026, 1, 1, tzinfo=UTC),
                ),
                Profile(
                    subject_type="entity",
                    subject_id=entities[0].id,
                    executive_summary="The latest source-backed research profile.",
                    version=2,
                    updated_at=observed_at,
                ),
                Profile(
                    subject_type="entity",
                    subject_id=entities[1].id,
                    executive_summary="A source-backed research profile.",
                    updated_at=observed_at,
                ),
            ]
            session.add_all(profiles)
            benchmark = Benchmark(
                name=f"Universe Test {suffix}",
                ticker=f"B{suffix}",
                benchmark_type="custom",
            )
            session.add(benchmark)
            await session.flush()
            constituents = [
                BenchmarkConstituent(
                    benchmark_id=benchmark.id,
                    security_id=securities[1].id,
                    weight_pct=100.0,
                    as_of_date=datetime(2026, 1, 1, tzinfo=UTC),
                ),
                BenchmarkConstituent(
                    benchmark_id=benchmark.id,
                    security_id=securities[2].id,
                    weight_pct=100.0,
                    as_of_date=observed_at,
                ),
            ]
            session.add_all(constituents)
            existing = OpportunityUniverseMember(
                security_id=securities[0].id,
                entity_id=entities[0].id,
                enabled=False,
                priority=0.87,
                source="manual",
                metadata_json={
                    "origins": [
                        {
                            "source_type": "manual",
                            "source_id": "operator-existing",
                            "label": "Existing operator choice",
                            "observed_at": observed_at.isoformat(),
                            "metadata": {},
                        }
                    ]
                },
            )
            session.add(existing)
            await session.commit()
            created["positions"] = [position.id for position in positions]
            created["profiles"] = [profile.id for profile in profiles]
            created["benchmarks"] = [benchmark.id]
            created["constituents"] = [row.id for row in constituents]
            created["members"] = [existing.id]

        async with async_session_maker() as session:
            service = OpportunityDiscoveryService(session)
            preview = await service.preview_universe_import()
            candidates = {item["ticker"]: item for item in preview["candidates"]}
            summaries = {
                item["source_type"]: item for item in preview["source_summaries"]
            }

            fixture_tickers = {
                securities[0].ticker,
                securities[1].ticker,
                securities[2].ticker,
            }
            assert fixture_tickers <= set(candidates)
            assert candidates[securities[0].ticker]["status"] == "present"
            assert {
                origin["source_type"]
                for origin in candidates[securities[0].ticker]["origins"]
            } == {"tracked_positions", "researched_catalog"}
            assert [
                origin["source_id"]
                for origin in candidates[securities[0].ticker]["origins"]
                if origin["source_type"] == "researched_catalog"
            ] == [str(profiles[1].id)]
            assert {
                origin["source_type"]
                for origin in candidates[securities[1].ticker]["origins"]
            } == {"researched_catalog"}
            assert {
                origin["source_type"]
                for origin in candidates[securities[2].ticker]["origins"]
            } == {"benchmark_constituents"}
            assert summaries["tracked_positions"]["eligible_count"] >= 1
            assert summaries["tracked_positions"]["existing_count"] >= 1
            assert summaries["tracked_positions"]["skipped_count"] >= 1
            assert summaries["researched_catalog"]["eligible_count"] >= 2
            assert summaries["benchmark_constituents"]["eligible_count"] >= 1
            assert any(
                item["ticker"] == securities[3].ticker
                and item["reason"] == "inactive_or_delisted_security"
                for item in preview["skipped"]
            )
            with pytest.raises(ValueError, match="Select at least one"):
                await service.preview_universe_import([])

            fixture_preview = {
                **preview,
                "candidates": [
                    item
                    for item in preview["candidates"]
                    if item["ticker"] in fixture_tickers
                ],
                "skipped": [
                    item
                    for item in preview["skipped"]
                    if item.get("ticker") == securities[3].ticker
                ],
            }

            async def fixture_import_preview(_sources):
                return fixture_preview

            monkeypatch.setattr(
                service, "preview_universe_import", fixture_import_preview
            )

            first = await service.import_universe(
                [
                    "tracked_positions",
                    "researched_catalog",
                    "benchmark_constituents",
                ]
            )
            assert first["imported_count"] == 2
            assert first["existing_count"] == 1
            created["members"].extend(
                member_id
                for member_id in first["member_ids"]
                if member_id not in created["members"]
            )

            existing_after = await session.get(OpportunityUniverseMember, existing.id)
            assert existing_after.enabled is False
            assert float(existing_after.priority) == pytest.approx(0.87)
            assert {
                origin["source_type"]
                for origin in existing_after.metadata_json["origins"]
            } == {"manual", "tracked_positions", "researched_catalog"}

            second = await service.import_universe(
                [
                    "tracked_positions",
                    "researched_catalog",
                    "benchmark_constituents",
                ]
            )
            assert second["imported_count"] == 0
            assert second["existing_count"] == 3
            assert second["provenance_updated_count"] == 0
    finally:
        async with async_session_maker() as session:
            if created["members"]:
                await session.execute(
                    delete(OpportunityUniverseMember).where(
                        OpportunityUniverseMember.id.in_(created["members"])
                    )
                )
            if created["constituents"]:
                await session.execute(
                    delete(BenchmarkConstituent).where(
                        BenchmarkConstituent.id.in_(created["constituents"])
                    )
                )
            if created["benchmarks"]:
                await session.execute(
                    delete(Benchmark).where(Benchmark.id.in_(created["benchmarks"]))
                )
            if created["positions"]:
                await session.execute(
                    delete(Position).where(Position.id.in_(created["positions"]))
                )
            if created["profiles"]:
                await session.execute(
                    delete(Profile).where(Profile.id.in_(created["profiles"]))
                )
            if created["securities"]:
                await session.execute(
                    delete(Security).where(Security.id.in_(created["securities"]))
                )
            if created["entities"]:
                await session.execute(
                    delete(Entity).where(Entity.id.in_(created["entities"]))
                )
            await session.commit()


async def test_research_query_falls_back_when_model_drops_the_subject(
    monkeypatch,
) -> None:
    async def drifting_query(**_kwargs):
        return {
            "query": "latest semiconductor industry update",
            "research_goal": "Find a material change.",
        }

    monkeypatch.setattr(opportunity_module, "call_llm_json", drifting_query)
    service = OpportunityDiscoveryService(None)
    query, goal = await service._research_query(
        security=Security(ticker="SNDK"),
        entity=Entity(name="Sandisk Corporation", entity_type="company"),
    )

    assert query == '"Sandisk Corporation" SNDK latest material investor update'
    assert goal == "Find a material change."


async def test_provider_capacity_skip_keeps_universe_member_due(monkeypatch) -> None:
    entity, security, member = await _seed_universe_member()
    run_id = None

    async def research_query(self, **_kwargs):
        return "test query", "test goal"

    async def skipped_research(self, **_kwargs):
        return SimpleNamespace(reason="research_provider_budget_exhausted")

    async def no_telemetry(self, _run):
        return None

    monkeypatch.setattr(OpportunityDiscoveryService, "_research_query", research_query)
    monkeypatch.setattr(
        opportunity_module.ResearchService,
        "run_ad_hoc_request",
        skipped_research,
    )
    monkeypatch.setattr(
        OpportunityDiscoveryService,
        "_refresh_provider_telemetry",
        no_telemetry,
    )

    try:
        async with async_session_maker() as session:
            run = OpportunityDiscoveryRun(
                status="running",
                captured_at=datetime.now(UTC),
                started_at=datetime.now(UTC),
                heartbeat_at=datetime.now(UTC),
                remaining_member_ids_json=[str(member.id)],
            )
            session.add(run)
            await session.flush()
            run_id = run.id
            await OpportunityDiscoveryService(session)._inspect_member(
                run=run,
                member=await session.get(OpportunityUniverseMember, member.id),
                security=await session.get(Security, security.id),
                entity=await session.get(Entity, entity.id),
            )

            refreshed = await session.get(OpportunityUniverseMember, member.id)
            assert run.skipped_count == 1
            assert run.inspected_count == 0
            assert refreshed.last_inspected_at is None
            assert refreshed.next_inspection_at is None
    finally:
        await _cleanup_catalog(
            entity_id=entity.id,
            security_id=security.id,
            member_id=member.id,
            run_ids=[run_id] if run_id else None,
        )


async def test_active_run_claim_is_database_deduplicated_and_resumable() -> None:
    active_key = f"opportunity-test-{uuid4()}"
    now = datetime.now(UTC)

    async def acquire():
        async with async_session_maker() as session:
            service = OpportunityDiscoveryService(session)
            service.ACTIVE_KEY = active_key
            run, acquired = await service._acquire_run(now=now)
            return run.id, acquired

    first, second = await asyncio.gather(acquire(), acquire())
    run_ids = {first[0], second[0]}
    try:
        assert len(run_ids) == 1
        assert sorted([first[1], second[1]]) == [False, True]

        async with async_session_maker() as session:
            run = await session.get(OpportunityDiscoveryRun, next(iter(run_ids)))
            assert run is not None
            run.heartbeat_at = now - timedelta(hours=1)
            await session.commit()

        async with async_session_maker() as session:
            service = OpportunityDiscoveryService(session)
            service.ACTIVE_KEY = active_key
            resumed, acquired = await service._acquire_run(now=now + timedelta(hours=1))
            assert resumed.id in run_ids
            assert acquired is True
    finally:
        async with async_session_maker() as session:
            await session.execute(
                delete(OpportunityDiscoveryRun).where(
                    OpportunityDiscoveryRun.id.in_(run_ids)
                )
            )
            await session.commit()


async def test_discovery_run_persists_the_runtime_limits_it_used() -> None:
    active_key = f"opportunity-limits-{uuid4()}"
    run_id = None
    try:
        async with async_session_maker() as session:
            service = OpportunityDiscoveryService(session)
            service.ACTIVE_KEY = active_key
            service.runtime = SimpleNamespace(
                max_subjects_per_run=7,
                revisit_hours=36,
                candidate_ttl_days=21,
            )
            run, acquired = await service._acquire_run(now=datetime.now(UTC))
            run_id = run.id

            assert acquired is True
            assert run.limits_json == {
                "max_subjects": 7,
                "revisit_hours": 36,
                "candidate_ttl_days": 21,
            }
    finally:
        if run_id is not None:
            async with async_session_maker() as session:
                await session.execute(
                    delete(OpportunityDiscoveryRun).where(
                        OpportunityDiscoveryRun.id == run_id
                    )
                )
                await session.commit()


async def test_candidate_assumptions_are_separate_and_candidate_is_deduplicated() -> (
    None
):
    entity, security, member = await _seed_universe_member()
    run_ids = []
    profile = {
        "name": "Test opportunity",
        "family_key": "test-mechanism",
        "priority_score": 0.82,
        "signal_stage": "early",
        "why_now": "A dated leading indicator changed.",
        "investable_thesis": "The change may alter normalized earnings.",
        "portfolio_transmission": "Cash could be tested without changing a real holding.",
        "expected_edge": "The leading indicator may precede consensus revisions.",
        "expected_relative_direction": "outperform",
        "falsification_tests": ["The indicator reverses."],
        "assumptions": ["The source measurement is comparable over time."],
        "uncertainties": ["Consensus may already reflect the change."],
        "evidence_refs": ["evidence:test"],
        "evidence_snapshot": [{"ref": "evidence:test", "ticker": security.ticker}],
        "priced_in_assessment": "uncertain",
        "policy": "Observe in a paper account only.",
        "operator_prompt": "Re-check the indicator before any paper action.",
        "horizon": "adaptive",
        "horizon_days": 14,
    }
    try:
        async with async_session_maker() as session:
            first_run = OpportunityDiscoveryRun(
                status="completed",
                captured_at=datetime.now(UTC),
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
                heartbeat_at=datetime.now(UTC),
            )
            session.add(first_run)
            await session.flush()
            service = OpportunityDiscoveryService(session)
            service.runtime.candidate_ttl_days = 3
            first = await service._upsert_candidate(
                run=first_run,
                security=await session.get(Security, security.id),
                entity=await session.get(Entity, entity.id),
                profile=profile,
            )
            await session.commit()
            run_ids.append(first_run.id)
            first_id = first.id
            assert (
                timedelta(days=2, hours=23)
                < (first.expires_at - first.last_seen_at)
                < timedelta(days=3, minutes=1)
            )

        async with async_session_maker() as session:
            second_run = OpportunityDiscoveryRun(
                status="completed",
                captured_at=datetime.now(UTC),
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
                heartbeat_at=datetime.now(UTC),
            )
            session.add(second_run)
            await session.flush()
            service = OpportunityDiscoveryService(session)
            service.runtime.candidate_ttl_days = 3
            second = await service._upsert_candidate(
                run=second_run,
                security=await session.get(Security, security.id),
                entity=await session.get(Entity, entity.id),
                profile=profile,
            )
            await session.commit()
            run_ids.append(second_run.id)

            assert second.id == first_id
            assert second.assumptions_json == profile["assumptions"]
            assert second.uncertainties_json == profile["uncertainties"]
            assert second.evidence_snapshot_json == profile["evidence_snapshot"]
            observations = list(
                (
                    await session.execute(
                        select(OpportunityCandidateObservation)
                        .where(
                            OpportunityCandidateObservation.candidate_id == second.id
                        )
                        .order_by(OpportunityCandidateObservation.captured_at)
                    )
                )
                .scalars()
                .all()
            )
            assert [observation.run_id for observation in observations] == run_ids
            assert all(
                observation.profile_snapshot_json["investable_thesis"]
                == profile["investable_thesis"]
                for observation in observations
            )
    finally:
        await _cleanup_catalog(
            entity_id=entity.id,
            security_id=security.id,
            member_id=member.id,
            run_ids=run_ids,
        )


async def test_point_in_time_outcome_uses_fixed_shared_closes_and_controls(
    monkeypatch,
) -> None:
    entity, security, member = await _seed_universe_member()
    run_id = None
    captured_at = datetime(2026, 1, 5, 16, tzinfo=UTC)
    profile = {
        "name": "Point-in-time outcome",
        "family_key": f"outcome-{uuid4()}",
        "priority_score": 0.8,
        "signal_stage": "early",
        "why_now": "A dated source changed.",
        "investable_thesis": "The security should outperform the benchmark.",
        "portfolio_transmission": "Observe relative return without a real trade.",
        "expected_edge": "The source change may precede market expectations.",
        "expected_relative_direction": "outperform",
        "falsification_tests": ["Relative return remains negative."],
        "assumptions": ["Adjusted daily closes are comparable."],
        "uncertainties": ["Provider history can be revised."],
        "evidence_refs": ["evidence:dated"],
        "evidence_snapshot": [{"ref": "evidence:dated", "public_time": "2026-01-05"}],
        "policy": "Observe only.",
        "operator_prompt": "Keep the point-in-time boundary fixed.",
        "horizon": "short_term",
        "horizon_days": 7,
    }

    try:
        async with async_session_maker() as session:
            run = OpportunityDiscoveryRun(
                status="completed",
                captured_at=captured_at,
                started_at=captured_at,
                completed_at=captured_at,
                heartbeat_at=captured_at,
            )
            session.add(run)
            await session.flush()
            run_id = run.id
            service = OpportunityDiscoveryService(session)
            candidate = await service._upsert_candidate(
                run=run,
                security=await session.get(Security, security.id),
                entity=await session.get(Entity, entity.id),
                profile=profile,
            )
            await session.flush()
            observation = (
                await session.execute(
                    select(OpportunityCandidateObservation).where(
                        OpportunityCandidateObservation.candidate_id == candidate.id
                    )
                )
            ).scalar_one()

            async def chart(ticker, **_kwargs):
                prices = (
                    [100.0, 100.0, 120.0, 30.0, 999.0]
                    if ticker == security.ticker
                    else [100.0, 100.0, 105.0, 106.0, 107.0]
                )
                dates = [5, 6, 12, 13, 14]
                return {
                    "series": [],
                    "adjusted_series": [
                        (datetime(2026, 1, day, 21, tzinfo=UTC), price)
                        for day, price in zip(dates, prices)
                    ],
                }

            monkeypatch.setattr(service.market_data, "fetch_chart_series", chart)

            pending = await service._evaluate_observation(
                observation,
                as_of=datetime(2026, 1, 10, 12, tzinfo=UTC),
            )
            assert pending == "pending"
            assert observation.candidate_start_time.date().isoformat() == "2026-01-06"
            assert observation.candidate_start_price == pytest.approx(100.0)
            assert observation.candidate_end_price is None

            evaluated = await service._evaluate_observation(
                observation,
                as_of=datetime(2026, 1, 14, 12, tzinfo=UTC),
            )
            assert evaluated == "evaluated"
            assert observation.candidate_end_time.date().isoformat() == "2026-01-12"
            assert observation.candidate_end_price == pytest.approx(120.0)
            assert observation.candidate_return_pct == pytest.approx(20.0)
            assert observation.benchmark_return_pct == pytest.approx(5.0)
            assert observation.excess_return_pct == pytest.approx(15.0)
            assert observation.cash_return_pct == 0.0
            assert observation.result_label == "supported"
            assert observation.evaluation_policy_json["market_control"]
            assert observation.evaluation_policy_json["cash_control_return_pct"] == 0.0
    finally:
        await _cleanup_catalog(
            entity_id=entity.id,
            security_id=security.id,
            member_id=member.id,
            run_ids=[run_id] if run_id else None,
        )


async def test_outcome_direction_control_can_support_or_challenge_either_side() -> None:
    label = OpportunityDiscoveryService._outcome_label

    assert label(expected_direction="outperform", excess_return_pct=1.0) == "supported"
    assert (
        label(expected_direction="outperform", excess_return_pct=-1.0) == "challenged"
    )
    assert (
        label(expected_direction="underperform", excess_return_pct=-1.0) == "supported"
    )
    assert (
        label(expected_direction="underperform", excess_return_pct=1.0) == "challenged"
    )
    assert (
        label(expected_direction="unscored", excess_return_pct=1.0)
        == "direction_unrecorded"
    )


async def test_settled_price_filter_rejects_current_and_future_dates() -> None:
    as_of = datetime(2026, 1, 14, 12, tzinfo=UTC)
    prices = OpportunityDiscoveryService._settled_prices(
        {
            "adjusted_series": [
                (datetime(2026, 1, 13, 21, tzinfo=UTC), 100.0),
                (datetime(2026, 1, 14, 21, tzinfo=UTC), 200.0),
                (datetime(2026, 1, 15, 21, tzinfo=UTC), 300.0),
            ]
        },
        as_of=as_of,
    )

    assert [value.isoformat() for value in prices] == ["2026-01-13"]


async def test_shadow_discovery_requires_known_non_market_evidence() -> None:
    assert "assumptions" in SHADOW_DISCOVERY_SCHEMA["required"]
    profile = {
        "should_launch": True,
        "name": "Evidence boundary",
        "signal_stage": "early",
        "why_now": "A source-backed change occurred.",
        "priced_in_assessment": "uncertain",
        "investable_thesis": "Test thesis",
        "portfolio_transmission": "Paper-only route",
        "expected_edge": "Potential expectation lag",
        "expected_relative_direction": "outperform",
        "policy": "Paper only",
        "operator_prompt": "Re-check evidence",
        "leading_indicators": ["Indicator"],
        "lagging_confirmations": ["Later earnings"],
        "evidence_refs": ["market:ONLY"],
        "evidence_to_check": ["Official source"],
        "falsification_tests": ["Indicator reverses"],
        "risk_controls": ["No real trade"],
        "assumptions": [],
        "uncertainties": ["Pricing"],
    }
    actionable, reason = ShadowService._discovery_profile_is_actionable(
        profile,
        available_evidence_refs={"market:ONLY"},
        portfolio_value=10_000,
    )
    assert actionable is False
    assert (
        reason == "shadow_discovery_requires_source_backed_evidence_beyond_market_tape"
    )


async def test_shadow_handoff_does_not_create_a_real_position() -> None:
    entity, security, member = await _seed_universe_member()
    run_ids = []
    experiment_id = None
    family_id = None
    try:
        async with async_session_maker() as session:
            run = OpportunityDiscoveryRun(
                status="completed",
                captured_at=datetime.now(UTC),
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
                heartbeat_at=datetime.now(UTC),
            )
            session.add(run)
            await session.flush()
            profile = {
                "name": "Paper-only handoff",
                "family_key": f"paper-only-{uuid4()}",
                "priority_score": 0.8,
                "signal_stage": "early",
                "why_now": "Source-backed test",
                "investable_thesis": "A falsifiable paper hypothesis.",
                "portfolio_transmission": "Use only the simulated account.",
                "expected_edge": "Potential delayed expectation response.",
                "expected_relative_direction": "outperform",
                "falsification_tests": ["Source signal reverses."],
                "assumptions": [],
                "uncertainties": ["Price may already reflect it."],
                "evidence_refs": ["evidence:test"],
                "evidence_snapshot": [
                    {"ref": "evidence:test", "ticker": security.ticker}
                ],
                "policy": "Paper account only.",
                "operator_prompt": "Do not affect the real portfolio.",
                "horizon": "adaptive",
                "horizon_days": 14,
                "trigger_reason": "Test handoff",
            }
            candidate = await OpportunityDiscoveryService(session)._upsert_candidate(
                run=run,
                security=await session.get(Security, security.id),
                entity=await session.get(Entity, entity.id),
                profile=profile,
            )
            await session.commit()
            run_ids.append(run.id)

            serialized, experiment_id = await OpportunityDiscoveryService(
                session
            ).shadow_test_candidate(
                candidate.id,
                OpportunityShadowTestRequest(
                    account_basis="cash_only",
                    starting_cash=1_000,
                ),
            )
            experiment = await session.get(ShadowExperiment, experiment_id)
            family_id = experiment.family_id
            position_count = await session.scalar(
                select(func.count())
                .select_from(Position)
                .where(Position.security_id == security.id)
            )

            assert serialized["status"] == "shadow_tested"
            assert position_count == 0
            assert experiment.initial_portfolio_state_json["experiment_context"][
                "subject_refs"
            ] == [
                {
                    "subject_type": "entity",
                    "subject_id": str(entity.id),
                    "security_id": str(security.id),
                }
            ]
    finally:
        async with async_session_maker() as session:
            if run_ids:
                await session.execute(
                    delete(OpportunityCandidate).where(
                        OpportunityCandidate.run_id.in_(run_ids)
                    )
                )
            if experiment_id is not None:
                await session.execute(
                    delete(ShadowExperiment).where(ShadowExperiment.id == experiment_id)
                )
            if family_id is not None:
                await session.execute(
                    delete(ExperimentFamilyState).where(
                        ExperimentFamilyState.id == family_id
                    )
                )
            if run_ids:
                await session.execute(
                    delete(OpportunityDiscoveryRun).where(
                        OpportunityDiscoveryRun.id.in_(run_ids)
                    )
                )
            await session.commit()
        await _cleanup_catalog(
            entity_id=entity.id,
            security_id=security.id,
            member_id=member.id,
        )
