from __future__ import annotations

import pytest
import sqlalchemy as sa

from investos.db import engine

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_migrated_schema_owns_discovery_and_trust_provenance() -> None:
    def inspect_schema(connection):
        inspector = sa.inspect(connection)
        source_columns = {column["name"] for column in inspector.get_columns("sources")}
        discovery_columns = {
            column["name"]
            for column in inspector.get_columns("research_discovery_observations")
        }
        discovery_indexes = {
            index["name"]
            for index in inspector.get_indexes("research_discovery_observations")
        }
        return source_columns, discovery_columns, discovery_indexes

    async with engine.connect() as connection:
        source_columns, discovery_columns, discovery_indexes = (
            await connection.run_sync(inspect_schema)
        )

    assert {
        "trust_origin",
        "trust_review_status",
        "trust_review_reason",
        "trust_reviewed_at",
    } <= source_columns
    assert {
        "provider",
        "query",
        "effective_query",
        "url",
        "snippet",
        "content_kind",
        "outcome",
        "evidence_id",
        "observed_at",
    } <= discovery_columns
    assert "ix_research_discovery_observations_observed_at" in discovery_indexes
    assert "ix_research_discovery_observations_outcome" in discovery_indexes
