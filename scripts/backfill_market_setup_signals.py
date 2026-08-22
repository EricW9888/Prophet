#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from investos.db import async_session_maker  # noqa: E402
from investos.services.market_setup import MarketSetupSignalService  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed source-dated market setup signals from existing evidence."
    )
    parser.add_argument(
        "--apply", action="store_true", help="Create signals. Omit for dry-run."
    )
    parser.add_argument(
        "--limit", type=int, default=500, help="Maximum evidence rows to scan."
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.45,
        help="Minimum extractor confidence required to keep a candidate.",
    )
    parser.add_argument(
        "--include-conversation-turns",
        action="store_true",
        help="Include chat/reflection evidence. Default skips it to avoid self-pollution.",
    )
    args = parser.parse_args()

    async with async_session_maker() as session:
        result = await MarketSetupSignalService(
            session
        ).backfill_from_existing_evidence(
            apply=args.apply,
            limit=args.limit,
            min_confidence=args.min_confidence,
            include_conversation_turns=args.include_conversation_turns,
        )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
