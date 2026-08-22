#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from investos.db import async_session_maker  # noqa: E402
from investos.services.investment_object_backfill import (  # noqa: E402
    InvestmentObjectBackfillService,
)


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Reindex saved evidence into source-dated fundamental metrics and "
            "market-setup signals. Runs as a dry run unless --apply is provided."
        )
    )
    parser.add_argument(
        "--apply", action="store_true", help="Persist qualified objects."
    )
    parser.add_argument("--scan-limit", type=int, default=300)
    parser.add_argument("--max-model-calls", type=int, default=10)
    parser.add_argument("--min-confidence", type=float, default=0.75)
    parser.add_argument(
        "--all-known-subjects",
        action="store_true",
        help="Include known securities outside the portfolio/watch workspace.",
    )
    parser.add_argument(
        "--include-conversation-turns",
        action="store_true",
        help="Include chat/reflection evidence. Disabled by default to avoid self-pollution.",
    )
    parser.add_argument(
        "--retry-completed",
        action="store_true",
        help="Retry evidence already checkpointed by this extractor version.",
    )
    parser.add_argument(
        "--evidence-id",
        type=UUID,
        help="Limit the pass to one reviewed evidence record.",
    )
    args = parser.parse_args()

    async with async_session_maker() as session:
        result = await InvestmentObjectBackfillService(session).run(
            apply=args.apply,
            scan_limit=args.scan_limit,
            max_model_calls=args.max_model_calls,
            min_confidence=args.min_confidence,
            portfolio_only=not args.all_known_subjects,
            include_conversation_turns=args.include_conversation_turns,
            retry_completed=args.retry_completed,
            evidence_id=args.evidence_id,
        )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
