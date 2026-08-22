from typing import Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.coverage import UnresolvedQuestion
from investos.services.research import ResearchService


class ResearchWorker:
    """Autonomously seeks out information to fill known gaps in the Coverage Map."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.service = ResearchService(session)

    async def run_targeted_search(self, question: UnresolvedQuestion) -> Optional[UUID]:
        result = await self.service.run_targeted_question(question)
        return result.evidence_id

    async def run_ad_hoc_search(
        self,
        *,
        query: str,
        title: str,
        source_item_type: str = "web_research",
        metadata_json: dict | None = None,
    ) -> Optional[UUID]:
        result = await self.service.run_ad_hoc_request(
            query=query,
            title=title,
            source_item_type=source_item_type,
            metadata_json=metadata_json,
            process_after_ingest=True,
        )
        return result.evidence_id
