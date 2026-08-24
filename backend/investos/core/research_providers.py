from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx


@dataclass(frozen=True)
class ResearchProviderCapability:
    provider: str
    label: str
    requires_api_key: bool
    requires_base_url: bool
    is_metered: bool

    def public_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "label": self.label,
            "requires_api_key": self.requires_api_key,
            "requires_base_url": self.requires_base_url,
            "is_metered": self.is_metered,
        }


RESEARCH_PROVIDER_CAPABILITIES: dict[str, ResearchProviderCapability] = {
    "searxng": ResearchProviderCapability(
        provider="searxng",
        label="SearXNG",
        requires_api_key=False,
        requires_base_url=True,
        is_metered=False,
    ),
    "tavily": ResearchProviderCapability(
        provider="tavily",
        label="Tavily",
        requires_api_key=True,
        requires_base_url=False,
        is_metered=True,
    ),
}


@dataclass
class ProviderSearchResponse:
    provider: str
    status: str
    query: str
    results: list[dict[str, Any]] = field(default_factory=list)
    request_id: str | None = None
    error: str | None = None
    estimated_credits: int = 0


def configured_research_providers(research: Any) -> list[str]:
    configured: list[str] = []
    for provider in getattr(research, "provider_order", []):
        if (
            provider == "searxng"
            and str(getattr(research, "searxng_base_url", "") or "").strip()
        ):
            configured.append(provider)
        elif (
            provider == "tavily" and str(getattr(research, "api_key", "") or "").strip()
        ):
            configured.append(provider)
    return configured


def normalize_search_results(
    provider: str, raw_results: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for raw in raw_results:
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("url") or "").strip()
        title = " ".join(str(raw.get("title") or "").split()).strip()
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if (
            parsed.scheme.casefold() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            continue
        hostname = parsed.hostname.rstrip(".").casefold()
        if hostname == "localhost" or hostname.endswith(".localhost"):
            continue
        try:
            literal_address = ipaddress.ip_address(hostname)
        except ValueError:
            literal_address = None
        if literal_address is not None and not literal_address.is_global:
            continue
        canonical_key = parsed._replace(fragment="").geturl()
        if canonical_key in seen_urls:
            continue
        seen_urls.add(canonical_key)

        raw_content = str(raw.get("raw_content") or "").strip()
        snippet = str(raw.get("content") or "").strip()
        normalized.append(
            {
                "title": title or parsed.hostname,
                "url": canonical_key,
                "content": raw_content or snippet,
                "raw_content": raw_content or None,
                "content_kind": "raw_content" if raw_content else "snippet",
                "score": raw.get("score"),
                "published_date": raw.get("published_date") or raw.get("publishedDate"),
                "discovery_provider": provider,
                "engines": raw.get("engines") or [],
            }
        )
    return normalized


async def search_research_provider(
    *,
    provider: str,
    client: httpx.AsyncClient,
    query: str,
    search_depth: str,
    include_raw_content: bool,
    searxng_base_url: str = "",
    tavily_api_key: str | None = None,
) -> ProviderSearchResponse:
    if provider == "searxng":
        endpoint = f"{searxng_base_url.rstrip('/')}/search"
        try:
            response = await client.get(
                endpoint,
                params={"q": query, "format": "json"},
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            return ProviderSearchResponse(
                provider=provider,
                status="research_failed",
                query=query,
                error=f"{type(exc).__name__}: {exc}",
            )
        if not isinstance(payload, dict) or not isinstance(
            payload.get("results", []), list
        ):
            return ProviderSearchResponse(
                provider=provider,
                status="research_failed",
                query=query,
                error="Provider response did not contain a result list.",
            )
        results = normalize_search_results(provider, payload.get("results") or [])
        return ProviderSearchResponse(
            provider=provider,
            status="ok" if results else "no_result",
            query=query,
            results=results,
        )

    if provider == "tavily":
        estimated_credits = 2 if search_depth == "advanced" else 1
        try:
            response = await client.post(
                "https://api.tavily.com/search",
                headers={"Authorization": f"Bearer {tavily_api_key or ''}"},
                json={
                    "query": query,
                    "search_depth": search_depth,
                    "include_raw_content": include_raw_content,
                },
            )
            if response.status_code == 429:
                return ProviderSearchResponse(
                    provider=provider,
                    status="rate_limited",
                    query=query,
                )
            if response.status_code == 432:
                return ProviderSearchResponse(
                    provider=provider,
                    status="research_provider_limit_exceeded",
                    query=query,
                )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            return ProviderSearchResponse(
                provider=provider,
                status="research_failed",
                query=query,
                error=f"{type(exc).__name__}: {exc}",
            )
        if not isinstance(payload, dict) or not isinstance(
            payload.get("results", []), list
        ):
            return ProviderSearchResponse(
                provider=provider,
                status="research_failed",
                query=query,
                error="Provider response did not contain a result list.",
            )
        results = normalize_search_results(provider, payload.get("results") or [])
        return ProviderSearchResponse(
            provider=provider,
            status="ok" if results else "no_result",
            query=query,
            results=results,
            request_id=payload.get("request_id"),
            estimated_credits=estimated_credits,
        )

    return ProviderSearchResponse(
        provider=provider,
        status="research_provider_not_configured",
        query=query,
        error="Unknown research provider.",
    )
