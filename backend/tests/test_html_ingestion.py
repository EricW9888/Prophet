import asyncio
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from investos.api.routes.ingestion import ingest_url
from investos.schemas.evidence import UrlEvidenceCreate
from investos.services.ingestion import (
    FetchedUrlDocument,
    IngestionService,
    _html_to_text,
)
from investos.services.source import SourceService


def test_page_text_excludes_executable_code_and_decodes_entities():
    html = '<html><style>hidden { color: red }</style><script>if (a < b) { window.fake = "invented claim"; }</script><main><p>Revenue &amp; margins</p><p>Actual report.</p></main></html>'
    assert _html_to_text(html) == "Revenue & margins Actual report."
    # A missing closing raw-text tag must not leak executable content either.
    assert _html_to_text("Visible<script>" + "<script>" * 10000) == "Visible"


async def test_url_entry_point_retains_assisted_origin_in_source_receipts(monkeypatch):
    ingest = AsyncMock(return_value=object())
    monkeypatch.setattr(IngestionService, "ingest_url", ingest)
    await ingest_url(UrlEvidenceCreate(url="https://example.test/source"), None)
    arguments = ingest.call_args.kwargs
    origin = SourceService._evidence_origin_summary(
        SimpleNamespace(
            metadata_json=arguments["metadata_json"],
            source_item_type=arguments["source_item_type"],
            url=arguments["url"],
        ),
        SimpleNamespace(source_type="web_research"),
    )
    assert origin["origin_kind"] == "manual"
    assert origin["origin_label"] == "Submitted URL"
    assert "not independently discovered" in origin["origin_detail"]


async def test_real_url_document_parsing_does_not_block_event_loop(monkeypatch):
    service = IngestionService(None)
    service._fetch_url_text = AsyncMock(return_value="<p>Actual report</p>")
    original = service._parse_url_document
    entered = threading.Event()
    release = threading.Event()

    def slow_parse(url, html):
        entered.set()
        assert release.wait(2)
        return original(url, html)

    monkeypatch.setattr(service, "_parse_url_document", slow_parse)
    task = asyncio.create_task(service.fetch_url_document("https://example.test/a"))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        # This coroutine can run while the actual parsing path is occupied.
        await asyncio.sleep(0)
    finally:
        release.set()
    result = await task
    assert result.content == "Actual report"


@pytest.mark.parametrize("same_filing", [True, False])
async def test_edgar_index_fetches_sequence_one_from_the_same_filing(same_filing):
    directory = "/Archives/edgar/data/123456/000012345626000001/"
    index = f"https://www.sec.gov{directory}0000123456-26-000001-index.htm"
    document = f"https://www.sec.gov{directory}operating-update.htm"
    href = (
        f"/ix?doc={directory}operating-update.htm"
        if same_filing
        else "/ix?doc=https://example.test/unrelated.htm"
    )
    html = f"""<table summary="Document Format Files">
       <tr><th>Seq</th><th>Description</th><th>Document</th></tr>
       <tr><td>1</td><td>Current report</td><td><a href="{href}">Main document</a></td></tr>
       <tr><td>2</td><td>Exhibit</td><td><a href="exhibit.htm">Exhibit</a></td></tr>
       </table>"""
    service = IngestionService(None)
    service._fetch_url_text = AsyncMock(
        side_effect=[html, "<p>The commission declined the application.</p>"]
    )
    if not same_filing:
        with pytest.raises(ValueError, match="supported primary filing"):
            await service.fetch_url_document(index)
        service._fetch_url_text.assert_awaited_once_with(index)
        return
    result = await service.fetch_url_document(index)
    assert result.url == index
    assert result.canonical_url == document
    assert result.content == "The commission declined the application."
    assert [call.args[0] for call in service._fetch_url_text.await_args_list] == [
        index,
        document,
    ]


async def test_url_intake_retains_material_after_previous_character_cutoff():
    service = IngestionService(None)
    content = (
        "Administrative disclosure.\n" * 1200
        + "Correction: the operating license has not been granted."
    )
    published = datetime(2026, 11, 1, tzinfo=UTC)
    service.fetch_url_document = AsyncMock(
        return_value=FetchedUrlDocument(
            url="https://example.test/report",
            canonical_url="https://example.test/report",
            title="Synthetic report",
            content=content,
            public_time=published,
        )
    )
    service._get_or_create_source = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
    service.ingest_text = AsyncMock(return_value=object())
    await service.ingest_url("https://example.test/report", process_now=False)
    payload = service.ingest_text.await_args.args[0]
    assert payload.content == content
    assert payload.public_time == published
    assert service.ingest_text.await_args.kwargs["process_now"] is False
