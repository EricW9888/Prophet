#!/usr/bin/env python3
"""Re-read retained sources; default previews use the configured hosted model."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from investos.core import provider_health  # noqa: E402
from investos.core.llm import compact_exception_message  # noqa: E402
from investos.db import async_session_maker  # noqa: E402
from investos.models.evidence import RawEvidence, SourceItem  # noqa: E402
from investos.workers.extraction import ExtractionWorker  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Preview source-backed relationship recovery using the configured model. --apply retains assertions; original evidence and other extraction objects remain intact.",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--evidence-id", type=UUID)
    parser.add_argument("--scan-limit", type=int, default=100)
    parser.add_argument("--max-model-calls", type=int, default=3)
    parser.add_argument("--retry-completed", action="store_true")
    args = parser.parse_args()
    if args.scan_limit < 1 or args.max_model_calls < 1:
        parser.error("Scan and model-call budgets must be positive")
    async with async_session_maker() as session:
        query = select(RawEvidence.id).join(
            SourceItem, SourceItem.raw_evidence_id == RawEvidence.id
        )
        if args.evidence_id:
            query = query.where(RawEvidence.id == args.evidence_id)
        ids = list(
            await session.scalars(
                query.order_by(RawEvidence.created_at.desc()).limit(args.scan_limit)
            )
        )
    results = []
    model_requests = 0
    completed_model_requests = 0
    for evidence_id in ids:
        if model_requests >= args.max_model_calls:
            break
        async with async_session_maker() as session:
            try:
                result = await ExtractionWorker(session).reindex_source_relationships(
                    evidence_id,
                    apply=args.apply,
                    retry_completed=args.retry_completed,
                )
            except Exception as exc:
                results.append(
                    {
                        "evidence_id": str(evidence_id),
                        "status": (
                            "deferred"
                            if isinstance(exc, provider_health.LLMProviderCooldownError)
                            else "error"
                        ),
                        "error": compact_exception_message(exc),
                    }
                )
                model_requests += 1
                break
        model_requests += int(result["model_calls"])
        completed_model_requests += int(result["model_calls"])
        results.append(result)
    print(
        json.dumps(
            {
                "dry_run": not args.apply,
                "attempted_model_requests": model_requests,
                "completed_model_requests": completed_model_requests,
                "results": results,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
