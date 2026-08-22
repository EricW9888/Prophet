import uuid
from typing import AsyncGenerator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from investos.db import get_session
from investos.services.historical import HistoricalEpisodeService
from investos.services.reasoning import ReasoningService
from investos.services.retrieval import RetrievalService

router = APIRouter(prefix="/chat", tags=["chat"])


async def generate_chat_response(
    query: str, subject_id: uuid.UUID, subject_type: str, session: AsyncSession
) -> AsyncGenerator[str, None]:
    retrieval = RetrievalService(session)
    packet = await retrieval.retrieve_evidence(
        query=query,
        subject_id=subject_id,
        subject_type=subject_type,
        max_depth=5,
    )
    packet_context = await retrieval.hydrate_packet(packet)
    yield (
        "data: [SYSTEM] Retrieved "
        f"{len(packet_context['direct_evidence'])} direct items and "
        f"{len(packet_context['contradiction_evidence'])} contradiction items.\n\n"
    )

    run, result = await ReasoningService(session).run_analysis(
        packet.id,
        include_critique=False,
    )
    analogies = await HistoricalEpisodeService(session).find_analogies(
        " ".join([query, result.get("thesis_summary", "")])
    )
    analogy_text = HistoricalEpisodeService.as_context_text(analogies)

    message_lines = [
        result["thesis_summary"],
        f"Stance: {result['stance']} | Confidence: {result['confidence_band']}",
        f"Reasoning: {result['reasoning']}",
        "What would falsify: " + "; ".join(result.get("what_would_falsify", [])),
        "Citations: "
        + " ".join(
            f"[Node:{node_id[:8]}]"
            for node_id in result.get("supporting_evidence_ids", [])
        ),
    ]
    if analogy_text:
        message_lines.append(analogy_text)
    message = "\n".join(message_lines)
    for chunk in message.split(" "):
        yield f"data: {chunk} \n\n"
    yield f"data: [RUN:{run.id}]\n\n"
    yield "data: [DONE]\n\n"


@router.get("/stream")
async def chat_stream(
    query: str,
    subject_id: uuid.UUID,
    subject_type: str = "entity",
    session: AsyncSession = Depends(get_session),
):
    return StreamingResponse(
        generate_chat_response(query, subject_id, subject_type, session),
        media_type="text/event-stream",
    )
