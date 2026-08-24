import hashlib
import io
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin, urlsplit
from uuid import UUID

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.config import settings
from investos.core.storage import LocalStorage
from investos.core.uploads import read_upload_limited
from investos.core.url_security import fetch_public_text
from investos.models.evidence import RawEvidence
from investos.models.source import Source
from investos.schemas.evidence import RawEvidenceCreate
from investos.services.corroboration import near_duplicate_signature
from investos.workers.extraction import ExtractionWorker

DEFAULT_SOURCE_NAME = "Manual Research Inbox"


@dataclass(frozen=True)
class FetchedUrlDocument:
    url: str
    canonical_url: str
    title: str | None
    content: str


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
        await self.session.commit()
        await self.session.refresh(evidence)

        if process_now:
            await ExtractionWorker(self.session).process_evidence(evidence.id)
            await self.session.refresh(evidence)

        return evidence

    async def ingest_text(
        self,
        payload: RawEvidenceCreate,
        process_now: bool = True,
    ) -> RawEvidence:
        if len(payload.content) > settings.INGESTION_MAX_NOTE_CHARS:
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
                source_item_type=source_item_type,
                url=url,
                author=author,
                metadata_json={
                    **(metadata_json or {}),
                    "content_type": "text/html",
                    "fetched_url": url,
                    "canonical_source_url": document.canonical_url,
                },
                content=document.content[:20000],
            ),
            process_now=process_now,
        )

    async def fetch_url_document(self, url: str) -> FetchedUrlDocument:
        html = await self._fetch_url_text(url)
        text_content = _html_to_text(html)
        if not text_content.strip():
            raise ValueError("Fetched URL did not contain extractable text content.")
        return FetchedUrlDocument(
            url=url,
            canonical_url=_extract_canonical_url(html, url),
            title=_extract_title(html),
            content=text_content,
        )

    async def _fetch_url_text(self, url: str) -> str:
        return await fetch_public_text(
            url,
            timeout_seconds=settings.URL_FETCH_TIMEOUT_SECONDS,
            max_redirects=settings.URL_FETCH_MAX_REDIRECTS,
            max_bytes=settings.URL_FETCH_MAX_RESPONSE_MB * 1024 * 1024,
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
                source.is_trusted = False
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
SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\\1>", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")


class _CanonicalUrlParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.canonical_url: str | None = None
        self.open_graph_url: str | None = None

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


def _extract_title(html: str) -> str | None:
    match = TITLE_RE.search(html)
    if not match:
        return None
    return WHITESPACE_RE.sub(" ", match.group(1)).strip()


def _html_to_text(html: str) -> str:
    without_scripts = SCRIPT_STYLE_RE.sub(" ", html)
    without_tags = TAG_RE.sub(" ", without_scripts)
    return WHITESPACE_RE.sub(" ", without_tags).strip()
