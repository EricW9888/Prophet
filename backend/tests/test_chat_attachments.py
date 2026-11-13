import asyncio
import base64
import io
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from docx import Document
from fastapi import UploadFile
from openpyxl import Workbook
from PIL import Image, ImageDraw, ImageFont
from reportlab.pdfgen import canvas
from sqlalchemy import func, select

from investos.config import settings
from investos.core import llm
from investos.core.provider_health import LLMProviderCooldownError, ProviderHealthStore
from investos.core.storage import LocalStorage
from investos.db import get_session
from investos.main import app
from investos.models.attachment import ChatAttachment
from investos.models.evidence import RawEvidence
from investos.models.portfolio import Transaction
from investos.models.watcher import ActiveWatcher
from investos.schemas.agent import AgentTurnRequest
from investos.services.agent import AgentService
from investos.services.chat_attachments import (
    ChatAttachmentService,
    _image_png,
    _read_document,
)
from investos.services.runtime_settings import RuntimeSettingsStore
from tests.test_investor_workflows import workflow_session


def image_bytes():
    image = Image.new("RGB", (640, 300), "white")
    ImageDraw.Draw(image).multiline_text(
        (24, 24),
        "EXMPL: 2 shares\nPrice: $25\nTotal: $50",
        fill="black",
        font=ImageFont.load_default(size=32),
        spacing=12,
    )
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def pdf_bytes(pages=2):
    output = io.BytesIO()
    writer = canvas.Canvas(output)
    for page in range(1, pages + 1):
        writer.drawString(
            72, 720, f"Synthetic page {page}: EXMPL holds 2 shares at USD 25."
        )
        writer.showPage()
    writer.save()
    return output.getvalue()


async def upload(service, name, data):
    return await service.upload(UploadFile(filename=name, file=io.BytesIO(data)))


def test_real_pdf_text_is_page_qualified_and_images_are_decoded():
    mime, text, meta = _read_document("synthetic.pdf", pdf_bytes())
    assert mime == "application/pdf"
    assert "[Page 1]" in text and "[Page 2]" in text
    assert "EXMPL holds 2 shares at USD 25" in text
    assert meta["page_count"] == meta["visual_count"] == 2
    mime, text, meta = _read_document("clipboard.png", image_bytes())
    assert mime == "image/png" and text == "" and meta["visual_count"] == 1
    with pytest.raises(Exception):
        _read_document("forged.png", b"not an image")


def test_office_documents_preserve_tables_cells_and_formulas_without_evaluating_them():
    document = Document()
    document.add_paragraph("Synthetic account evidence")
    document.add_table(rows=1, cols=2).rows[0].cells[0].text = "EXMPL"
    output = io.BytesIO()
    document.save(output)
    _, text, _ = _read_document("synthetic.docx", output.getvalue())
    assert (
        "Synthetic account evidence" in text and "[Table 1]" in text and "EXMPL" in text
    )
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Holdings"
    sheet.append(["EXMPL", 2, 25, "=B1*C1"])
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    _, text, meta = _read_document("synthetic.xlsx", output.getvalue())
    assert "[Sheet: Holdings]" in text and "B1: 2" in text and "D1: =B1*C1" in text
    assert any("without executing" in warning for warning in meta["warnings"])


async def test_private_upload_survives_reload_and_never_enters_research_intake(
    workflow_session, tmp_path
):
    storage = LocalStorage(str(tmp_path))
    service = ChatAttachmentService(workflow_session, storage)
    before = await workflow_session.scalar(select(func.count(RawEvidence.id)))
    original = image_bytes()
    row = await upload(service, "clipboard.png", original)
    await workflow_session.refresh(row)
    reloaded = await ChatAttachmentService(workflow_session, storage).get(row.id)
    assert reloaded.name == "clipboard.png" and reloaded.size_bytes == len(original)
    assert await storage.get_object(reloaded.storage_path) == original
    assert await workflow_session.scalar(select(func.count(RawEvidence.id))) == before


async def test_attachment_routes_retain_original_bytes_and_serve_private_content(
    workflow_session, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "investos.services.chat_attachments.LocalStorage",
        lambda: LocalStorage(str(tmp_path)),
    )
    from investos.services.live_jobs import LiveJobTracker

    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(app.state, "live_jobs", LiveJobTracker(), raising=False)

    async def failed_model(*args, **kwargs):
        raise RuntimeError("Synthetic provider outage")

    monkeypatch.setattr(AgentService, "handle_turn", failed_model)

    async def session_override():
        yield workflow_session

    app.dependency_overrides[get_session] = session_override
    try:
        original = image_bytes()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
        ) as client:
            response = await client.post(
                "/api/agent/attachments",
                files={"file": ("clipboard.png", original, "application/octet-stream")},
            )
            assert response.status_code == 200, response.text
            receipt = response.json()
            assert (
                receipt["content_type"] == "image/png"
                and receipt["visual_pages_read"] == 0
            )
            content = await client.get(
                f"/api/agent/attachments/{receipt['id']}/content"
            )
            assert content.status_code == 200 and content.content == original
            assert content.headers["cache-control"] == "no-store"
            assert content.headers["x-content-type-options"] == "nosniff"
            job = await client.post(
                "/api/agent/turn-jobs",
                json={
                    "message": "Read the attached image",
                    "attachment_ids": [receipt["id"]],
                    "use_session_attachments": False,
                },
            )
            assert job.status_code == 200, job.text
            assert job.json()["request_attachments"][0]["id"] == receipt["id"]
            await asyncio.sleep(0)
            failed = await client.get(f"/api/agent/turn-jobs/{job.json()['job_id']}")
            assert failed.json()["status"] == "error"
            assert failed.json()["request_attachments"][0]["name"] == "clipboard.png"
            assert failed.json()["use_session_attachments"] is False
            malformed = await client.post(
                "/api/agent/attachments",
                files={"file": ("bad.png", b"not-image", "image/png")},
            )
            assert malformed.status_code == 400
    finally:
        app.dependency_overrides.pop(get_session, None)


async def test_pdf_visual_reading_resumes_after_provider_failure_without_repeating_pages(
    workflow_session, tmp_path, monkeypatch
):
    service = ChatAttachmentService(workflow_session, LocalStorage(str(tmp_path)))
    row = await upload(service, "five-pages.pdf", pdf_bytes(5))
    monkeypatch.setattr(ProviderHealthStore, "require_available", lambda *args: 0)
    monkeypatch.setattr(
        ProviderHealthStore,
        "read",
        lambda *args: type("Healthy", (), {"state": "healthy"})(),
    )
    runtime = RuntimeSettingsStore.load()
    runtime.llm.provider = "nvidia_nim"
    monkeypatch.setattr(RuntimeSettingsStore, "load", lambda: runtime)
    first = {
        "pages": [
            {"page": page, "observations": f"Page {page}: 2 shares at $25"}
            for page in range(1, 5)
        ]
    }
    request = AsyncMock(
        side_effect=[first, LLMProviderCooldownError("synthetic quota")]
    )
    monkeypatch.setattr("investos.services.chat_attachments.call_llm_json", request)
    with pytest.raises(LLMProviderCooldownError):
        await service.read_for_question([row])
    await workflow_session.refresh(row)
    assert set(row.representation_json["visual_cache"]["pages"]) == {"1", "2", "3", "4"}
    first_images = request.await_args_list[0].kwargs["images"]
    assert len(first_images) == 4
    with Image.open(
        io.BytesIO(base64.b64decode(first_images[0].split(",", 1)[1]))
    ) as image:
        assert max(image.size) <= 1536
    resume = AsyncMock(
        return_value={"pages": [{"page": 5, "observations": "Page 5: 2 shares at $25"}]}
    )
    monkeypatch.setattr("investos.services.chat_attachments.call_llm_json", resume)
    files = await service.read_for_question([row])
    assert [item["page"] for item in files[0]["visual_observations"]] == [1, 2, 3, 4, 5]
    assert len(resume.await_args.kwargs["images"]) == 1
    await workflow_session.refresh(row)
    assert len(row.representation_json["visual_cache"]["pages"]) == 5


async def test_chat_files_follow_the_conversation_without_creating_trades_or_watches(
    workflow_session, tmp_path, monkeypatch
):
    session = workflow_session
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    # The LocalStorage default is fixed at import; inject the private test store.
    storage = LocalStorage(str(tmp_path))
    monkeypatch.setattr(
        "investos.services.chat_attachments.LocalStorage", lambda: storage
    )
    first = await upload(
        ChatAttachmentService(session), "first.csv", b"ticker,shares,price\nEXMPL,2,25"
    )
    model = AsyncMock(
        return_value={
            "answer": "The supplied first.csv contains EXMPL, 2 shares at 25. No broker records changed.",
            "rationale": "Read the provided rows only.",
        }
    )
    monkeypatch.setattr("investos.services.agent.call_llm_json", model)
    agent = AgentService(session)
    actions = AsyncMock(
        side_effect=AssertionError("File input must not execute portfolio actions")
    )
    monkeypatch.setattr(agent, "_execute_actions", actions)
    counts = [
        await session.scalar(select(func.count(table.id)))
        for table in (Transaction, ActiveWatcher)
    ]
    response = await agent.handle_turn(
        AgentTurnRequest(
            message="What does this file show?",
            attachment_ids=[first.id],
            auto_execute=True,
        )
    )
    assert (
        response.process_mode == "attached_files"
        and response.attachments[0].id == first.id
    )
    follow = await agent.handle_turn(
        AgentTurnRequest(message="What is the total?", session_id=response.session_id)
    )
    assert follow.attachments[0].id == first.id
    assert json.loads(model.await_args.kwargs["user_prompt"])["conversation_context"]
    history = await agent.conversation_history(response.session_id, None, "portfolio")
    assert history.entries[0].attachments[0].id == first.id
    assert [
        await session.scalar(select(func.count(table.id)))
        for table in (Transaction, ActiveWatcher)
    ] == counts
    actions.assert_not_awaited()


async def test_attachment_size_and_missing_ids_fail_before_model_use(
    workflow_session, tmp_path, monkeypatch
):
    from uuid import uuid4

    service = ChatAttachmentService(workflow_session, LocalStorage(str(tmp_path)))
    with pytest.raises(ValueError, match="unavailable"):
        await service.resolve([uuid4()])
    with pytest.raises(ValueError):
        await upload(service, "forged.pdf", b"%PDF-not-valid")
    monkeypatch.setattr(settings, "CHAT_ATTACHMENT_MAX_FILE_MB", 1)
    with pytest.raises(ValueError, match="size limit"):
        await upload(service, "huge.txt", b"x" * (1024 * 1024 + 1))


async def test_real_nvidia_adapter_transmits_images_instead_of_text_only_fallback(
    monkeypatch,
):
    from investos.services.runtime_settings import RuntimeSettings

    runtime = RuntimeSettings()
    runtime.llm.api_key = "synthetic-key"
    monkeypatch.setattr(RuntimeSettingsStore, "load", lambda: runtime)
    captured = []

    async def transport(**kwargs):
        captured.append(kwargs["payload"])
        return '{"read":"EXMPL"}'

    monkeypatch.setattr(llm, "_nvidia_chat_json_text", transport)
    image = "data:image/png;base64," + base64.b64encode(image_bytes()).decode("ascii")
    result = await llm._call_nvidia_json(
        system_prompt="Read the image",
        user_prompt="What ticker is shown?",
        schema={
            "type": "object",
            "properties": {"read": {"type": "string"}},
            "required": ["read"],
        },
        model="nvidia/nemotron-nano-12b-v2-vl",
        timeout_seconds=10,
        images=[image],
    )
    assert result["read"] == "EXMPL"
    assert captured[0]["messages"][1]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": image},
    }
