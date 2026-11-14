import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

from investos.api.routes.ingestion import ingest_url
from investos.schemas.evidence import UrlEvidenceCreate
from investos.services.ingestion import IngestionService, _html_to_text
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
