#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select  # noqa: E402

from investos.db import async_session_maker  # noqa: E402
from investos.models.conclusion import ConclusionState  # noqa: E402
from investos.models.coverage import CoverageMap, UnresolvedQuestion  # noqa: E402
from investos.models.entity import Entity, Security  # noqa: E402
from investos.models.evidence import RawEvidence, SourceItem  # noqa: E402
from investos.models.fundamental import FundamentalMetric  # noqa: E402
from investos.models.graph import Edge  # noqa: E402
from investos.models.knowledge import Claim, Event, Fact  # noqa: E402
from investos.models.lesson import Lesson  # noqa: E402
from investos.models.market_setup import MarketSetupSignal  # noqa: E402
from investos.models.portfolio import Position  # noqa: E402
from investos.models.profile import Profile  # noqa: E402
from investos.models.review import ReviewQueueItem  # noqa: E402
from investos.models.shadow import ExperimentResult, ShadowExperiment  # noqa: E402
from investos.models.source import Source  # noqa: E402
from investos.models.theme import Theme  # noqa: E402

NODE_MODELS = {
    "fact": Fact,
    "claim": Claim,
    "event": Event,
    "entity": Entity,
    "security": Security,
    "theme": Theme,
    "source_item": SourceItem,
    "raw_evidence": RawEvidence,
    "source": Source,
    "profile": Profile,
    "position": Position,
    "coverage_map": CoverageMap,
    "unresolved_question": UnresolvedQuestion,
    "conclusion": ConclusionState,
    "lesson": Lesson,
    "fundamental_metric": FundamentalMetric,
    "market_setup_signal": MarketSetupSignal,
    "review_item": ReviewQueueItem,
    "shadow_experiment": ShadowExperiment,
    "experiment_result": ExperimentResult,
}


def natural_edge_key(row: dict[str, str]) -> tuple[str, str, str, str, str]:
    return (
        row["source_type"],
        row["source_id"],
        row["target_type"],
        row["target_id"],
        row["relationship_type"],
    )


def load_backup_edges(path: Path) -> list[dict[str, str]]:
    if shutil.which("pg_restore") is None:
        raise RuntimeError("pg_restore_not_found")
    completed = subprocess.run(
        ["pg_restore", "--data-only", "-t", "edges", "--file", "-", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    rows: list[dict[str, str]] = []
    in_copy = False
    for line in completed.stdout.splitlines():
        if line.startswith("COPY public.edges"):
            in_copy = True
            continue
        if not in_copy:
            continue
        if line == r"\.":
            break
        parts = line.split("\t")
        if len(parts) < 10:
            continue
        rows.append(
            {
                "id": parts[0],
                "source_type": parts[1],
                "source_id": parts[2],
                "target_type": parts[3],
                "target_id": parts[4],
                "relationship_type": parts[5],
            }
        )
    return rows


async def audit_dump(path: Path) -> dict[str, Any]:
    backup_rows = load_backup_edges(path)
    backup_keys = {natural_edge_key(row) for row in backup_rows}

    async with async_session_maker() as session:
        current_edges = (await session.execute(select(Edge))).scalars().all()
        current_keys = {
            (
                edge.source_type,
                str(edge.source_id),
                edge.target_type,
                str(edge.target_id),
                edge.relationship_type,
            )
            for edge in current_edges
        }
        missing_rows = [
            row for row in backup_rows if natural_edge_key(row) not in current_keys
        ]
        needed_by_type: dict[str, set[str]] = {}
        for row in missing_rows:
            needed_by_type.setdefault(row["source_type"], set()).add(row["source_id"])
            needed_by_type.setdefault(row["target_type"], set()).add(row["target_id"])

        existing_by_type: dict[str, set[str]] = {}
        for node_type, ids in needed_by_type.items():
            model = NODE_MODELS.get(node_type)
            if model is None:
                existing_by_type[node_type] = set()
                continue
            found = (
                (await session.execute(select(model.id).where(model.id.in_(list(ids)))))
                .scalars()
                .all()
            )
            existing_by_type[node_type] = {str(item) for item in found}

    recoverable = 0
    recoverable_by_relationship: Counter[str] = Counter()
    blocked_by_reason: Counter[str] = Counter()
    for row in missing_rows:
        source_exists = row["source_id"] in existing_by_type.get(
            row["source_type"], set()
        )
        target_exists = row["target_id"] in existing_by_type.get(
            row["target_type"], set()
        )
        if source_exists and target_exists:
            recoverable += 1
            recoverable_by_relationship[row["relationship_type"]] += 1
        else:
            if not source_exists:
                blocked_by_reason[f"missing_source:{row['source_type']}"] += 1
            if not target_exists:
                blocked_by_reason[f"missing_target:{row['target_type']}"] += 1

    return {
        "backup_path": str(path),
        "current_edges": len(current_keys),
        "backup_edges": len(backup_rows),
        "missing_backup_edge_keys": len(backup_keys - current_keys),
        "current_extra_edge_keys": len(current_keys - backup_keys),
        "recoverable_missing_edges": recoverable,
        "blocked_missing_edges": len(missing_rows) - recoverable,
        "recoverable_by_relationship": dict(recoverable_by_relationship.most_common()),
        "blocked_by_reason": dict(blocked_by_reason.most_common()),
    }


def latest_dump() -> Path:
    dumps = sorted((REPO_ROOT / "backups").glob("investos_local_*.dump"))
    if not dumps:
        raise RuntimeError("no_backup_dumps_found")
    return dumps[-1]


async def main() -> int:
    parser = argparse.ArgumentParser(description="Dry-run graph edge recovery audit.")
    parser.add_argument(
        "--dump",
        type=Path,
        default=None,
        help="Backup dump to compare. Defaults to latest backup.",
    )
    args = parser.parse_args()
    path = (args.dump or latest_dump()).resolve()
    result = await audit_dump(path)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
