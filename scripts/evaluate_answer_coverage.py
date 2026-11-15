"""Opt-in synthetic review-to-answer evaluation with retained human rubrics.

This calls the real planner, independent review, answer binding and renderer.
It does not search, write conversations, or measure full research latency.
Model coverage labels are diagnostics, not correctness scores.
"""

import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from benchmark_structured_generation import argument_parser, isolated_run

from investos.config import settings
from investos.core.llm import call_llm_json
from investos.schemas.agent import AgentTurnRequest
from investos.services.agent import AgentService
from investos.services.corroboration import CorroborationService
from investos.services.reasoning import ReasoningService

CORPUS = (
    Path(__file__).resolve().parents[1]
    / "backend/tests/fixtures/answer_coverage_holdouts.json"
)


async def run(args):
    if args.timeout <= 0 or args.max_output_tokens <= 0:
        raise SystemExit("Timeout and output token budget must be positive.")
    if args.provider == "ollama" and not args.allow_local:
        raise SystemExit("Local execution requires --allow-local.")
    if args.allow_local:
        settings.LLM_ALLOW_LOCAL_PROVIDER = True
    settings.LLM_STRUCTURED_MAX_TOKENS = args.max_output_tokens
    cases = [
        row
        for row in json.loads(CORPUS.read_text(encoding="utf-8"))
        if not args.case or row["id"] in args.case
    ]
    output = Path(args.output)
    if output.exists():
        raise SystemExit("Choose a new output path; previous results are retained.")
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "provider": args.provider,
        "model": args.model,
        "started_at": datetime.now(UTC).isoformat(),
        "corpus_sha256": hashlib.sha256(CORPUS.read_bytes()).hexdigest(),
        "scope": "planner, independent review, answer binding and rendering only",
        "human_review_required": True,
        "results": [],
    }

    async def selected_provider(**kwargs):
        kwargs.update(model=args.model, provider_override=args.provider)
        kwargs["timeout_seconds"] = min(
            kwargs.get("timeout_seconds", args.timeout), args.timeout
        )
        return await call_llm_json(**kwargs)

    with (
        patch("investos.services.agent.call_llm_json", selected_provider),
        patch("investos.services.reasoning.call_llm_json", selected_provider),
        patch.object(
            ReasoningService, "_active_provider_unconfigured_label", lambda _: None
        ),
    ):
        for case in cases:
            started = time.perf_counter()
            row = {"case": case["id"], "rubric": case, "completed": False}
            try:
                evidence_id = str(
                    uuid5(NAMESPACE_URL, "prophet-evaluation:" + case["id"])
                )
                packet = {
                    "query_text": case["question"],
                    "subject_type": "theme",
                    "subject_name": case["subject"],
                    "direct_evidence": [
                        {
                            "id": evidence_id,
                            "type": "fact",
                            "text": case["statement"],
                            "source": {
                                "lineage_key": "publisher:notice.example",
                                "authority": "primary",
                                "evidence_title": "Synthetic notice",
                                "evidence_url": "https://notice.example/record",
                            },
                        }
                    ],
                }
                agent = AgentService(None)
                plan = await agent._plan_question_research(
                    message=case["question"],
                    subject_name=case["subject"],
                    subject_type="theme",
                    freshness_requirement={"requires_fresh_research": False},
                )
                packet["research_plan"] = plan
                review = await ReasoningService(
                    SimpleNamespace(add=lambda _: None)
                )._create_critique(
                    SimpleNamespace(id=uuid5(NAMESPACE_URL, evidence_id)),
                    packet,
                    {"stance": "uncertain"},
                    defer_on_provider_failure=True,
                )
                if not review:
                    raise RuntimeError("Independent review unavailable")
                result = {"stance": "uncertain", "confidence_band": "low"}
                CorroborationService().apply_independent_review(result, review)
                payload = AgentTurnRequest(message=case["question"], auto_execute=False)
                answers = await agent._answer_from_review(payload, plan, packet, review)
                if answers is not None:
                    review["information_need_reviews"] = answers[
                        "information_need_reviews"
                    ]
                agent._apply_independent_answerability_gate(
                    result, review, question_research_plan=plan, packet_context=packet
                )
                delivered = agent._fast_path_orchestration(payload, result)
                agent._audit_delivered_quantities(delivered, result)
                row.update(
                    completed=True,
                    plan=plan,
                    review=review,
                    result=result,
                    delivered=delivered,
                )
            except Exception as exc:
                row["error"] = type(exc).__name__
            row["stage_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
            report["results"].append(row)
            output.write_text(
                json.dumps(report, indent=2, default=str), encoding="utf-8"
            )
            print(
                json.dumps(
                    {key: row[key] for key in ("case", "completed", "stage_elapsed_ms")}
                ),
                flush=True,
            )
            if not row["completed"]:
                break


if __name__ == "__main__":
    parser = argument_parser(
        [row["id"] for row in json.loads(CORPUS.read_text(encoding="utf-8"))],
        description=__doc__,
    )
    asyncio.run(isolated_run(parser.parse_args(), runner=run))
