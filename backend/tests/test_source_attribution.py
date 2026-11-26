"""Auto-discovered sources are publishers, not names guessed from titles (#302)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from investos.config import settings
from investos.models.source import Source
from investos.services.source_learning import SourceLearningService


@pytest.fixture
async def attribution_session():
    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, poolclass=NullPool)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            yield session
        if transaction.is_active:
            await transaction.rollback()
    await engine.dispose()


async def test_sources_are_attributed_to_the_publisher_domain(
    attribution_session,
) -> None:
    session = attribution_session
    service = SourceLearningService(session)
    tag = uuid4().hex[:10]
    edgar = f"sec-{tag}.example"
    analysis = f"analysis-{tag}.example"
    other = f"other-{tag}.example"

    async def resolve(url: str, title: str) -> Source:
        context = await service.get_or_create_source_for_url(url=url, title=title)
        return context.source

    # EDGAR-style titles lead with the form type, not the publisher.
    first_filing = await resolve(
        f"https://www.{edgar}/Archives/1/0001-index.htm",
        "424B2 - Synthetic Bank Notes LLC (0000000001) (Filer)",
    )
    second_filing = await resolve(
        f"https://www.{edgar}/Archives/2/0002-index.htm",
        "424B2 - Unrelated Issuer Inc (0000000002) (Filer)",
    )
    assert first_filing.name == edgar
    assert second_filing.id == first_filing.id

    # A page about a company, hosted elsewhere, belongs to its host.
    page = await resolve(
        f"https://{analysis}/etfs/fund/risk",
        f"Synthetic ETF Trust {tag} - fund risk analysis",
    )
    assert page.name == analysis

    # The same title prefix on two sites stays two publishers.
    elsewhere = await resolve(
        f"https://{other}/story", f"Synthetic ETF Trust {tag} - another article"
    )
    assert elsewhere.id != page.id
    assert elsewhere.name == other

    # Earlier rows named after a title prefix no longer absorb new pages.
    session.add(
        Source(
            name=f"Synthetic ETF Trust {tag}",
            source_type="web_research",
            is_trusted=False,
        )
    )
    await session.flush()
    third = await resolve(
        f"https://{analysis}/another-page", f"Synthetic ETF Trust {tag} - follow-up"
    )
    assert third.id == page.id

    # A legacy "www." name still matches the same publisher.
    legacy = Source(
        name=f"www.legacy-{tag}.example", source_type="news", is_trusted=False
    )
    session.add(legacy)
    await session.flush()
    legacy_match = await resolve(
        f"https://legacy-{tag}.example/a", "Headline - Section"
    )
    assert legacy_match.id == legacy.id
