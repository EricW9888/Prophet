#!/usr/bin/env python3
"""Preview, then optionally apply, the #302 source re-attribution.

Without --apply this only reads, inside a read-only transaction. With --apply it
moves evidence from title-derived sources to publisher-domain sources and
withdraws learned trust from emptied ones. Operator trust and system sources are
never changed. Run --apply only after the owner approves the preview.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import text  # noqa: E402

from investos.db import async_session_maker  # noqa: E402
from investos.services.source_attribution_repair import (  # noqa: E402
    SourceAttributionRepair,
)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the re-attribution")
    parser.add_argument("--sample", type=int, default=40)
    args = parser.parse_args()
    async with async_session_maker() as session:
        if not args.apply:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            print(
                json.dumps(
                    await SourceAttributionRepair(session).plan(sample=args.sample),
                    indent=2,
                    default=str,
                )
            )
            await session.rollback()
            return
        result = await SourceAttributionRepair(session).apply()
        await session.commit()
        print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
