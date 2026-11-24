import asyncio
import hashlib
import io
import json
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from http import HTTPStatus
from typing import Optional
from urllib.parse import parse_qs, urljoin, urlsplit
from uuid import UUID

from fastapi import UploadFile
from pypdfium2 import PdfiumError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.config import settings
from investos.core.dates import parse_explicit_calendar_datetime, parse_iso_datetime
from investos.core.pdf_text import read_pdf_text
from investos.core.storage import LocalStorage
from investos.core.uploads import UploadTooLargeError, read_upload_limited
from investos.core.url_security import (
    PublicDocument,
    UrlFetchNetworkError,
    fetch_public_document,
)
from investos.models.evidence import RawEvidence
from investos.models.source import Source
from investos.schemas.evidence import RawEvidenceCreate
from investos.services.corroboration import near_duplicate_signature
from investos.services.evidence_processing import EvidenceProcessingService
from investos.workers.extraction import ExtractionWorker

DEFAULT_SOURCE_NAME = "Manual Research Inbox"
EDGAR_HOSTS = {"sec.gov", "www.sec.gov"}
EDGAR_INDEX_PATH = re.compile(
    r"/Archives/edgar/data/(?P<cik>[0-9]+)/(?:(?P<directory>[0-9]{18})/)?"
    r"(?P<accession>[0-9]{10}-[0-9]{2}-[0-9]{6})-index\.html?"
)


class SourceContentUnavailable(ValueError):
    """The located source has no usable representation for this reader."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class FetchedUrlDocument:
    url: str
    canonical_url: str
    title: str | None
    content: str
    public_time: datetime | None = None
    source_item_type: str | None = None
    capture_metadata: dict = field(
        default_factory=lambda: {"content_type": "text/html"}
    )


class IngestionService:
    def __init__(self, session: AsyncSession, storage: LocalStorage | None = None):
        self.session = session
        self.storage = storage or LocalStorage()

    async def ingest_file(
        self,
        file: UploadFile,
        title: Optional[str] = None,
        source_id: Optional[UUID] = None,
        source_item_type: str = "manual_upload",
        url: Optional[str] = None,
        author: Optional[str] = None,
        public_time=None,
        content_type: Optional[str] = None,
        metadata_json: Optional[dict] = None,
        process_now: bool = True,
    ) -> RawEvidence:
        content_bytes = await read_upload_limited(
            file,
            max_bytes=settings.INGESTION_MAX_UPLOAD_MB * 1024 * 1024,
        )
        resolved_content_type = (
            content_type or file.content_type or "application/octet-stream"
        )
        storage_path = await self.storage.put_object(
            io.BytesIO(content_bytes),
            filename=file.filename or "evidence.bin",
            content_type=resolved_content_type,
        )
        source = await self._get_or_create_source(source_id)
        text_signature = None
        signature_token_count = 0
        if resolved_content_type.startswith("text/"):
            decoded = content_bytes.decode("utf-8", errors="ignore")
            text_signature, signature_token_count = near_duplicate_signature(decoded)

        evidence = RawEvidence(
            title=title or file.filename,
            source_id=source.id,
            source_item_type=source_item_type,
            raw_content_ref=storage_path,
            content_hash=hashlib.sha256(content_bytes).hexdigest(),
            url=url,
            author=author,
            public_time=public_time,
            metadata_json={
                **(metadata_json or {}),
                "filename": file.filename,
                "content_type": resolved_content_type,
                "size_bytes": len(content_bytes),
                "near_duplicate_signature": text_signature,
                "signature_token_count": signature_token_count,
                "lineage_signature_status": (
                    "ready" if text_signature else "insufficient_text"
                ),
                "lineage_signature_version": 1,
            },
        )
        self.session.add(evidence)
        await self.session.flush()
        await EvidenceProcessingService(self.session).ensure_for_evidence(evidence)
        await self.session.commit()
        await self.session.refresh(evidence)

        if process_now:
            await ExtractionWorker(
                self.session,
                storage=self.storage,
            ).process_evidence(evidence.id)
            await self.session.refresh(evidence)

        return evidence

    async def ingest_text(
        self,
        payload: RawEvidenceCreate,
        process_now: bool = True,
        *,
        fetched_document: bool = False,
    ) -> RawEvidence:
        if (
            not fetched_document
            and len(payload.content) > settings.INGESTION_MAX_NOTE_CHARS
        ):
            raise ValueError("The note exceeds the configured size limit.")
        filename = f"{(payload.title or 'manual_note').replace(' ', '_')[:40]}.txt"
        upload = UploadFile(
            file=io.BytesIO(payload.content.encode("utf-8")), filename=filename
        )
        return await self.ingest_file(
            file=upload,
            title=payload.title,
            source_id=payload.source_id,
            source_item_type=payload.source_item_type,
            url=payload.url,
            author=payload.author,
            public_time=payload.public_time,
            content_type=(payload.metadata_json or {}).get(
                "content_type", "text/plain"
            ),
            metadata_json=payload.metadata_json,
            process_now=process_now,
        )

    async def ingest_url(
        self,
        url: str,
        title: Optional[str] = None,
        source_id: Optional[UUID] = None,
        source_item_type: str = "web_research",
        author: Optional[str] = None,
        metadata_json: Optional[dict] = None,
        process_now: bool = True,
    ) -> RawEvidence:
        document = await self.fetch_url_document(url)
        resolved_title = title or document.title or url

        source = await self._get_or_create_source(source_id)
        return await self.ingest_text(
            RawEvidenceCreate(
                title=resolved_title,
                source_id=source.id,
                source_item_type=document.source_item_type or source_item_type,
                url=url,
                author=author,
                public_time=document.public_time,
                metadata_json={
                    **(metadata_json or {}),
                    **document.capture_metadata,
                    "fetched_url": url,
                    "canonical_source_url": document.canonical_url,
                    "publication_time_source": (
                        "document_metadata" if document.public_time else None
                    ),
                },
                content=document.content,
            ),
            process_now=process_now,
            fetched_document=True,
        )

    async def fetch_url_document(self, url: str) -> FetchedUrlDocument:
        # The video service also uses ingestion to persist its own passes.
        from investos.services.youtube import YouTubeService

        if video_id := YouTubeService.video_id_for_url(url):
            return await YouTubeService.fetch_caption_document(url, video_id)
        try:
            response = await self._fetch_public_document(url)
        except UrlFetchNetworkError as exc:
            index = _edgar_index_match(url)
            if exc.status_code != HTTPStatus.NOT_FOUND or index is None:
                raise
            accession = index["accession"]
            submission_url = (
                urlsplit(url)
                ._replace(
                    path=f"/Archives/edgar/data/{index['cik']}/{accession}.txt",
                    query="",
                    fragment="",
                )
                .geturl()
            )
            response = await self._fetch_public_document(
                submission_url, max_megabytes=settings.INGESTION_MAX_UPLOAD_MB
            )
            document = await asyncio.to_thread(
                self._parse_edgar_submission, response, accession
            )
            return replace(document, url=url)
        if video_id := YouTubeService.video_id_for_url(response.url):
            return await YouTubeService.fetch_caption_document(url, video_id)
        edgar_index = _edgar_index_match(response.url)
        if edgar_index:
            primary_url = await asyncio.to_thread(
                _edgar_primary_document_url, response.url, response.text()
            )
            response = await self._fetch_public_document(
                primary_url,
                max_megabytes=settings.INGESTION_MAX_UPLOAD_MB,
            )
        document = await asyncio.to_thread(self._parse_public_document, response)
        return replace(
            document,
            url=url,
            canonical_url=response.url if edgar_index else document.canonical_url,
            source_item_type="filing" if edgar_index else document.source_item_type,
        )

    def _parse_edgar_submission(
        self, response: PublicDocument, accession: str
    ) -> FetchedUrlDocument:
        header, *documents = re.split(r"(?im)^<DOCUMENT>[ \t]*\r?$", response.text())
        if urlsplit(response.url).hostname not in EDGAR_HOSTS or not re.search(
            rf"(?im)^[ \t]*(?:ACCESSION NUMBER:|<ACCESSION-NUMBER>)[ \t]*{re.escape(accession)}[ \t]*\r?$",
            header,
        ):
            raise SourceContentUnavailable(
                "The EDGAR complete submission did not identify the requested accession."
            )
        primary = []
        for section in documents:
            fields, _, body = section.partition("<TEXT>")
            if re.search(r"(?im)^<SEQUENCE>[ \t]*0*1[ \t]*\r?$", fields):
                primary.append((fields, body))
        if len(primary) != 1:
            raise SourceContentUnavailable(
                "The EDGAR complete submission did not identify one primary document."
            )
        fields, body = primary[0]
        filename = re.search(
            r"(?im)^<FILENAME>[ \t]*([\w.-]+\.(?:html?|txt|xml))[ \t]*\r?$", fields
        )
        content, text_end, tail = body.partition("</TEXT>")
        if (
            filename is None
            or not text_end
            or not tail.lstrip().startswith("</DOCUMENT>")
        ):
            raise SourceContentUnavailable(
                "The EDGAR primary document has no supported text representation."
            )
        document = self._parse_url_document(response.url, content)
        return replace(
            document,
            canonical_url=response.url,
            source_item_type="filing",
            capture_metadata={
                **document.capture_metadata,
                "representation": "edgar_primary_text",
                "source_media_type": response.media_type,
                "edgar_primary_filename": filename[1],
            },
        )

    def _parse_public_document(self, response: PublicDocument) -> FetchedUrlDocument:
        if response.media_type != "application/pdf":
            return self._parse_url_document(response.url, response.text())
        try:
            max_bytes = settings.INGESTION_MAX_UPLOAD_MB * 1024 * 1024
            content, pdf = read_pdf_text(response.body, max_chars=max_bytes)
        except PdfiumError as exc:
            raise SourceContentUnavailable("The PDF could not be read.") from exc
        if pdf["warnings"] or len(content.encode("utf-8")) > max_bytes:
            raise UploadTooLargeError(
                "The PDF text exceeds the configured storage size limit."
            )
        if not pdf["pages_with_text"]:
            raise SourceContentUnavailable(
                "The PDF has no readable text layer. Its pages require visual reading."
            )
        if pdf["pages_with_text"] < pdf["page_count"]:
            pdf["warnings"].append(
                "Some PDF pages had no readable text layer. Graphics and scans were not read."
            )
        return FetchedUrlDocument(
            url=response.url,
            canonical_url=response.url,
            title=None,
            content=content,
            capture_metadata={
                "content_type": "text/plain",
                "source_media_type": "application/pdf",
                "representation": "pdf_text",
                "source_coverage": pdf,
            },
        )

    @staticmethod
    def _parse_url_document(url: str, html: str) -> FetchedUrlDocument:
        text_content = _html_to_text(html)
        if not text_content.strip():
            raise ValueError("Fetched URL did not contain extractable text content.")
        if (
            len(text_content.encode("utf-8"))
            > settings.INGESTION_MAX_UPLOAD_MB * 1024 * 1024
        ):
            raise UploadTooLargeError(
                "The extracted document exceeds the configured storage size limit."
            )
        return FetchedUrlDocument(
            url=url,
            canonical_url=_extract_canonical_url(html, url),
            title=_extract_title(html),
            content=text_content,
            public_time=_extract_public_time(html),
        )

    async def _fetch_public_document(
        self, url: str, *, max_megabytes: int | None = None
    ) -> PublicDocument:
        return await fetch_public_document(
            url,
            timeout_seconds=settings.URL_FETCH_TIMEOUT_SECONDS,
            max_redirects=settings.URL_FETCH_MAX_REDIRECTS,
            max_bytes=(max_megabytes or settings.URL_FETCH_MAX_RESPONSE_MB)
            * 1024
            * 1024,
            max_pdf_bytes=settings.INGESTION_MAX_UPLOAD_MB * 1024 * 1024,
            allowed_ports=settings.URL_FETCH_ALLOWED_PORT_SET,
        )

    async def _get_or_create_source(self, source_id: Optional[UUID]) -> Source:
        if source_id:
            source = (
                await self.session.execute(select(Source).where(Source.id == source_id))
            ).scalar_one_or_none()
            if source:
                return source

        source = (
            await self.session.execute(
                select(Source).where(Source.name == DEFAULT_SOURCE_NAME).limit(1)
            )
        ).scalar_one_or_none()
        if source:
            if source.is_trusted:
                source.apply_learned_trust(
                    False,
                    reason=(
                        "Fallback manual uploads and notes are user-provided context, "
                        "not independent corroboration."
                    ),
                )
            return source

        source = Source(
            name=DEFAULT_SOURCE_NAME,
            source_type="manual",
            description="Fallback manual source for uploads and notes.",
            is_trusted=False,
        )
        self.session.add(source)
        await self.session.flush()
        return source


TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
WHITESPACE_RE = re.compile(r"\s+")
PUBLICATION_META_KEYS = {
    "article:published_time",
    "date",
    "datecreated",
    "datepublished",
    "dc.date",
    "dcterms.date",
    "og:published_time",
    "parsely-pub-date",
    "pubdate",
    "publishdate",
    "sailthru.date",
}


class _EdgarDocumentIndex(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_document_table = False
        self.first_cell_open = False
        self.sequence = ""
        self.row_href = None
        self.primary_href = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "table":
            self.in_document_table = (
                attributes.get("summary") == "Document Format Files"
            )
        if not self.in_document_table or self.primary_href is not None:
            return
        if tag == "tr":
            self.sequence = ""
            self.row_href = None
        elif tag == "td" and not self.sequence:
            self.first_cell_open = True
        elif tag == "a" and self.row_href is None:
            self.row_href = attributes.get("href")

    def handle_data(self, data):
        if self.first_cell_open:
            self.sequence += data

    def handle_endtag(self, tag):
        if tag == "td":
            self.first_cell_open = False
        elif tag == "tr" and self.in_document_table and self.sequence.strip() == "1":
            self.primary_href = self.row_href
        elif tag == "table":
            self.in_document_table = False


def _edgar_index_match(url: str) -> re.Match[str] | None:
    target = urlsplit(url)
    if target.hostname not in EDGAR_HOSTS:
        return None
    match = EDGAR_INDEX_PATH.fullmatch(target.path)
    if match and match["directory"] not in (None, match["accession"].replace("-", "")):
        return None
    return match


def _edgar_primary_document_url(index_url: str, html: str) -> str:
    index = urlsplit(index_url)
    parser = _EdgarDocumentIndex()
    parser.feed(html)
    target = urlsplit(urljoin(index_url, parser.primary_href or ""))
    if target.path == "/ix" and target.netloc == index.netloc:
        target = urlsplit(
            urljoin(index_url, parse_qs(target.query).get("doc", [""])[0])
        )
    accession = index.path.rsplit("/", 1)[-1].split("-index.", 1)[0].replace("-", "")
    # Joint filings may list a different CIK and an SEC XSL rendering directory.
    document = re.fullmatch(
        rf"/Archives/edgar/data/\d+/{re.escape(accession)}/(?:xsl[\w.-]+/)?([\w.-]+\.(?:html?|txt|xml|pdf))",
        target.path,
        flags=re.IGNORECASE,
    )
    if (
        not parser.primary_href
        or target.scheme != index.scheme
        or target.netloc != index.netloc
        or document is None
        or re.search(r"-index\.html?$", document[1], flags=re.IGNORECASE)
    ):
        raise ValueError(
            "The EDGAR index did not identify a supported primary filing document."
        )
    return target._replace(query="", fragment="").geturl()


class _CanonicalUrlParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.canonical_url: str | None = None
        self.open_graph_url: str | None = None
        self.public_time_candidates: list[str] = []
        self._json_ld_parts: list[str] | None = None
        self.json_ld_documents: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {str(key).casefold(): value for key, value in attrs}
        if tag.casefold() == "link":
            rel = str(values.get("rel") or "").casefold().split()
            if "canonical" in rel and values.get("href") and not self.canonical_url:
                self.canonical_url = str(values["href"])
        elif tag.casefold() == "meta":
            property_name = str(
                values.get("property") or values.get("name") or ""
            ).casefold()
            if (
                property_name == "og:url"
                and values.get("content")
                and not self.open_graph_url
            ):
                self.open_graph_url = str(values["content"])
            date_key = str(
                values.get("property")
                or values.get("name")
                or values.get("itemprop")
                or ""
            ).casefold()
            if date_key in PUBLICATION_META_KEYS and values.get("content"):
                self.public_time_candidates.append(str(values["content"]))
        elif tag.casefold() == "time":
            itemprop = str(values.get("itemprop") or "").casefold()
            if itemprop in {"datecreated", "datepublished"} and values.get("datetime"):
                self.public_time_candidates.append(str(values["datetime"]))
        elif (
            tag.casefold() == "script"
            and str(values.get("type") or "").casefold() == "application/ld+json"
        ):
            self._json_ld_parts = []

    def handle_data(self, data: str) -> None:
        if self._json_ld_parts is not None:
            self._json_ld_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self._json_ld_parts is not None:
            self.json_ld_documents.append("".join(self._json_ld_parts))
            self._json_ld_parts = None


def _extract_canonical_url(html: str, base_url: str) -> str:
    parser = _CanonicalUrlParser()
    try:
        parser.feed(html)
    except (TypeError, ValueError):
        return base_url
    declared = parser.canonical_url or parser.open_graph_url or base_url
    try:
        resolved = urljoin(base_url, declared)
        parsed = urlsplit(resolved)
    except ValueError:
        return base_url
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return base_url
    return resolved


def _extract_public_time(html: str, *, now: datetime | None = None) -> datetime | None:
    parser = _CanonicalUrlParser()
    try:
        parser.feed(html)
    except (TypeError, ValueError):
        return None

    candidates = list(parser.public_time_candidates)
    for document in parser.json_ld_documents:
        try:
            payload = json.loads(document)
        except (TypeError, ValueError):
            continue
        candidates.extend(_json_publication_dates(payload))

    reference_time = now or datetime.now(UTC)
    latest = reference_time + timedelta(days=1)
    for value in candidates:
        parsed = parse_iso_datetime(value) or parse_explicit_calendar_datetime(
            value,
            reference_time=reference_time,
            latest=latest,
        )
        if parsed is not None and parsed <= latest:
            return parsed
    return None


def _json_publication_dates(value: object) -> list[str]:
    if isinstance(value, list):
        dates: list[str] = []
        for item in value:
            dates.extend(_json_publication_dates(item))
        return dates
    if not isinstance(value, dict):
        return []
    dates = []
    for key, item in value.items():
        if str(key).casefold() in {"datecreated", "datepublished"} and isinstance(
            item, str
        ):
            dates.append(item)
        elif isinstance(item, (dict, list)):
            dates.extend(_json_publication_dates(item))
    return dates


def _extract_title(html: str) -> str | None:
    match = TITLE_RE.search(html)
    if not match:
        return None
    return WHITESPACE_RE.sub(" ", match.group(1)).strip()


class _TextParser(HTMLParser):
    """Use HTML's raw-text handling instead of backtracking over script bodies."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden_tag: str | None = None
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden_tag = tag

    def handle_endtag(self, tag):
        if tag == self.hidden_tag:
            self.hidden_tag = None

    def handle_data(self, data):
        if self.hidden_tag is None:
            self.parts.append(data)


def _html_to_text(html: str) -> str:
    parser = _TextParser()
    parser.feed(html)
    parser.close()
    return WHITESPACE_RE.sub(" ", " ".join(parser.parts)).strip()
