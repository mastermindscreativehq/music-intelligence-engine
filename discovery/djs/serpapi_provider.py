"""Dedicated DJ discovery through SerpAPI's Google organic search.

The generic ``discovery.djs.http_provider.HttpSearchSearchDjsProvider`` maps
a provider that answers ``Authorization: Bearer`` with a ``{"results": ...}``
body. SerpAPI authenticates instead through the ``api_key`` QUERY parameter
and returns Google organic hits under the ``organic_results`` key — feeding
the generic provider a SerpAPI response silently yields zero candidates
(payload has no ``results`` list). This module is the honest SerpAPI twin:
it emits the same ``Candidate`` contract the DJ engine consumes, speaks
SerpAPI's actual wire format (``engine=google&q=...&api_key=...``), records
honest provenance (``source = serpapi_google``), and NEVER fabricates a
result, a contact channel, or geography that the live response did not
actually carry.

Authentication (live, credential-gated)
---------------------------------------
Reads, in order:

1. explicit ``base_url``/``api_key`` arguments (tests / process wiring), or
2. environment variables ``SERPAPI_BASE_URL`` and ``SERPAPI_API_KEY``
   (production, documented in ``.env.example``).

When no SerpAPI key is configured the provider reports ``configured ==
False`` and ``search()`` raises ``DiscoveryProviderNotConfigured``. The
application MUST then answer the honest "DJ discovery provider is not
configured" response — it must NEVER fabricate live discovery results
(nothing is ever seeded from the deterministic seed list, which remains a
TEST-ONLY building block, never a stand-in for real discovery).

The API key travels only as the ``api_key`` query parameter of the outbound
request URL. It is never written into logs, exception messages, responses,
candidate fields, or any other output.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Sequence
from urllib.parse import urlencode, urlsplit

from discovery.models import (
    Candidate,
    DiscoveryRequest,
    SourceType,
)
from discovery.djs.http_provider import DiscoveryProviderNotConfigured

logger = logging.getLogger("mie.discovery.djs.serpapi")

SERPAPI_BASE_URL_ENV = "SERPAPI_BASE_URL"
SERPAPI_API_KEY_ENV = "SERPAPI_API_KEY"
DEFAULT_BASE_URL = "https://serpapi.com/search"
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_USER_AGENT = "MusicIntelligenceEngine/0.5 (+independent DJ discovery)"


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _clean_title(entry: Any, url: str) -> str:
    title = " ".join(str(entry or url).split())
    return title or url


def _clean_geo(value: Any) -> str | None:
    return _clean(value)


class SerpApiSearchDjsProvider:
    """REAL public-web DJ discovery through SerpAPI's Google search engine.

    ``search()`` issues one HTTP request per query against
    ``{base_url}?engine=google&q=<query>&api_key=<key>`` via the injected
    ``fetcher`` (the same seam the radio/DJ engine uses — an HTTP client
    exposing ``fetch(url, *, timeout=None) -> FetchResult``). SerpAPI's
    ``organic_results`` array is parsed; each organic result is mapped to a
    ``Candidate`` whose provenance records the live source (``source =
    serpapi_google``), whose snippet is carried only when present, and whose
    geography is carried ONLY when the organic result actually contains it —
    it is never derived from the query or the request, and never fabricated.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        fetcher=None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        resolved = base_url if base_url is not None else (
            os.environ.get(SERPAPI_BASE_URL_ENV) or DEFAULT_BASE_URL)
        self.base_url = resolved.strip()
        self._api_key = api_key or os.environ.get(SERPAPI_API_KEY_ENV) or ""
        self.timeout = timeout
        if fetcher is None:
            from crawler.http import StdlibHttpFetcher
            fetcher = StdlibHttpFetcher(
                timeout_seconds=timeout, max_bytes=1_500_000,
                user_agent=DEFAULT_USER_AGENT, respect_robots=False,
                allowed_content_types=("application/json",),
            )
        self._fetcher = fetcher
        self._host = self._derive_host()

    @property
    def configured(self) -> bool:
        """Live SerpAPI discovery requires BOTH a base URL and an API key."""
        return bool(self.base_url) and bool(self._api_key)

    def _derive_host(self) -> str:
        try:
            return urlsplit(self.base_url).netloc or "serpapi"
        except ValueError:
            return "serpapi"

    def _candidate_from_result(self, entry: dict, request: DiscoveryRequest,
                               query: str) -> Candidate | None:
        if not isinstance(entry, dict):
            return None
        url = str(entry.get("link") or entry.get("url") or "").strip()
        if not url:
            return None
        source = "serpapi_google"
        return Candidate(
            title=_clean_title(entry.get("title"), url),
            url=url,
            snippet=_clean(entry.get("snippet")) or "",
            source=source,
            source_type=SourceType.SEARCH_SOURCE,
            country=_clean_geo(entry.get("country")),
            state_or_region=_clean_geo(entry.get("state_or_region")),
            city=_clean_geo(entry.get("city")),
        )

    def search(
        self, request: DiscoveryRequest, queries: Sequence[str],
    ) -> list[Candidate]:
        if not self.configured:
            raise DiscoveryProviderNotConfigured(
                "DJ discovery provider is not configured: set "
                f"{SERPAPI_API_KEY_ENV} (and optionally {SERPAPI_BASE_URL_ENV})"
                " in .env — see .env.example")

        out: list[Candidate] = []
        seen_urls: set[str] = set()
        for query in queries:
            if len(out) >= request.limit:
                break
            params = {"engine": "google", "q": query,
                      "api_key": self._api_key}
            sep = "&" if "?" in self.base_url else "?"
            url = f"{self.base_url}{sep}{urlencode(params)}"
            try:
                result = self._fetcher.fetch(url, timeout=self.timeout)
            except Exception:
                logger.warning(
                    "serpapi_google transport failure: SerpAPI could not "
                    "be reached for query %r; treating DJ discovery as "
                    "not configured and NOT fabricating anything.", query)
                raise DiscoveryProviderNotConfigured(
                    "DJ discovery provider is not configured: SerpAPI is "
                    "not reachable from this host (no real discovery was "
                    "performed and nothing was fabricated).")
            if not getattr(result, "ok", False):
                logger.warning(
                    "serpapi_google transport failure: query %r did not "
                    "reach SerpAPI; no real discovery result was returned "
                    "and nothing was fabricated.", query)
                raise DiscoveryProviderNotConfigured(
                    "DJ discovery provider is not configured: SerpAPI is "
                    "not reachable from this host (no real discovery was "
                    "performed and nothing was fabricated).")
            if not getattr(result, "body", None):
                continue
            try:
                payload = json.loads(result.body)
            except (ValueError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            # SerpAPI reports provider errors as a JSON body, e.g.
            # ``{"error": "..."}`` — treat as a failed query, never fabricate.
            if payload.get("error"):
                continue
            entries = payload.get("organic_results")
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if len(out) >= request.limit:
                    break
                candidate = self._candidate_from_result(
                    entry, request, query)
                if candidate is None:
                    continue
                if candidate.url in seen_urls:
                    continue
                seen_urls.add(candidate.url)
                out.append(candidate)
        return out


def load_serpapi_search_entries(
    base_url: str | None = None, api_key: str | None = None, *,
    fetcher=None,
) -> SerpApiSearchDjsProvider:
    """Provider factory honouring env overrides; cron-usable."""
    return SerpApiSearchDjsProvider(
        base_url=base_url, api_key=api_key, fetcher=fetcher)
