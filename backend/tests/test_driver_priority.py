"""The coverage audit weighs questions by where the company's revenue comes from."""

import json
from datetime import date
from unittest.mock import AsyncMock

from sqlalchemy import select

from investos.models.business_map import BusinessDriver
from investos.models.coverage import UnresolvedQuestion
from investos.workers.coverage import COVERAGE_SCHEMA, CoverageWorker
from tests.test_question_lifecycle import (  # noqa: F401
    prompt,
    question_session,
    tracked_scope,
)


def driver(entity_id, kind, member, label, revenue, total=100.0):
    return BusinessDriver(
        entity_id=entity_id,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        kind=kind,
        member=member,
        label=label,
        revenue=revenue,
        share=revenue / total,
        total_revenue=total,
        filing_url="https://www.sec.gov/Archives/edgar/data/1/0001-25-000001.htm",
    )


async def test_audit_sees_revenue_drivers_and_records_each_questions_driver(
    question_session, monkeypatch
):
    session = question_session
    coverage, ids = await tracked_scope(session)
    entity_id = ids["entity"]
    session.add_all(
        [
            driver(entity_id, "segment", "SmallMember", "Licensing", 8.0),
            driver(entity_id, "segment", "BigMember", "Data center", 72.0),
            driver(entity_id, "segment", "MidMember", "Gaming", 20.0),
        ]
    )
    await session.flush()
    audit = AsyncMock(
        return_value={
            "overall_coverage_score": 40,
            "missing_classes": [],
            "unresolved_questions": [
                prompt(business_driver="Data center", existing_question_id=None),
                prompt(
                    question_text="Is licensing revenue about to grow?",
                    mechanism="new licensing agreements",
                    business_driver="Licensing",
                    existing_question_id=None,
                ),
            ],
        }
    )
    monkeypatch.setattr("investos.workers.coverage.call_llm_json", audit)

    await CoverageWorker(session).audit_subject_coverage(
        entity_id, "entity", "Synthetic Memory Company"
    )

    sent = json.loads(audit.await_args.kwargs["user_prompt"])
    assert sent["business_drivers"] == {
        "fiscal_year_end": "2025-12-31",
        "drivers": [
            {"kind": "segment", "label": "Data center", "share_of_revenue": "72%"},
            {"kind": "segment", "label": "Gaming", "share_of_revenue": "20%"},
            {"kind": "segment", "label": "Licensing", "share_of_revenue": "8%"},
        ],
    }
    assert "Keep exploring" in audit.await_args.kwargs["system_prompt"]
    assert (
        "business_driver"
        in COVERAGE_SCHEMA["properties"]["unresolved_questions"]["items"]["required"]
    )

    questions = (
        await session.scalars(
            select(UnresolvedQuestion).where(
                UnresolvedQuestion.coverage_map_id == coverage.id
            )
        )
    ).all()
    assert sorted(q.uncertainty_json.get("business_driver") for q in questions) == [
        "Data center",
        "Licensing",
    ]
    # The driver sits beside the question's identity, so deduplication is unchanged.
    keys = {q.semantic_key for q in questions}
    audit.return_value["unresolved_questions"][0]["business_driver"] = None
    await CoverageWorker(session).audit_subject_coverage(
        entity_id, "entity", "Synthetic Memory Company"
    )
    again = (
        await session.scalars(
            select(UnresolvedQuestion).where(
                UnresolvedQuestion.coverage_map_id == coverage.id,
                UnresolvedQuestion.status != "superseded",
            )
        )
    ).all()
    assert {q.semantic_key for q in again} == keys


async def test_subjects_without_a_revenue_map_are_audited_as_before(
    question_session, monkeypatch
):
    session = question_session
    _, ids = await tracked_scope(session)
    entity_id = ids["entity"]
    audit = AsyncMock(
        return_value={
            "overall_coverage_score": 40,
            "missing_classes": [],
            "unresolved_questions": [prompt()],
        }
    )
    monkeypatch.setattr("investos.workers.coverage.call_llm_json", audit)
    await CoverageWorker(session).audit_subject_coverage(
        entity_id, "entity", "Synthetic Memory Company"
    )
    assert "business_drivers" not in json.loads(audit.await_args.kwargs["user_prompt"])
