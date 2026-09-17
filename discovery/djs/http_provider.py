"""Real public-web DJ search provider (independent live discovery).

The radio pipeline's ``SeedListProvider``/``StaticListProvider`` read the
``stations`` key of a seed file, so feeding them a DJ seed file (which uses
the ``djs`` key) yields zero candidates. That mismatch silently starves the
DJ discovery engine. Instead of papering over it, this module provides a
live DJ counterpart that performs REAL public-web discovery through a
configured search provider HTTP API — emitting the same ``Candidate``
contract the DJ engine consumes, with provenance (``source`` =
``djs_http_search:<host>``), geography carried through, and never any
fabricated contact channel.

Authentication
--------------
The provider is credential-gated. It reads, in order:

1. explicit ``base_url``/``api_key`` arguments (tests / process wiring), or
2. environment variables ``DJS_SEARCH_BASE_URL`` and ``DJS_SEARCH_API_KEY``
   (production, documented in ``.env.example``).

When no base URL is configured the provider reports ``configured == False``
and ``search()`` raises ``DiscoveryProviderNotConfigured``. The application
must then return the honest "DJ discovery provider is not configured"
response — it must NEVER fabricate live discovery results, and the
deterministic seed provider remains a TEST-ONLY building block, never a
stand-in for real discovery.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlencode, urlsplit

from discovery.models import (
    Candidate,
    DiscoveryRequest,
    SourceType,
)

logger = logging.getLogger("mie.discovery.djs.http")

DJS_SEARCH_BASE_URL_ENV = "DJS_SEARCH_BASE_URL"
DJS_SEARCH_API_KEY_ENV = "DJS_SEARCH_API_KEY"
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_USER_AGENT = "MusicIntelligenceEngine/0.5 (+independent DJ discovery)"


class DiscoveryProviderNotConfigured(Exception):
    """Raised when discovery is invoked without a configured live provider."""


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _clean_title(name: Any, url: str) -> str:
    title = " ".join(str(name or "").split())
    return title or url


def _clean_geo(value: Any) -> str | None:
    return _clean(value)


class HttpSearchDjsProvider:
    """Seeks DJ candidates through a configured public search HTTP API.

    ``search()`` issues one HTTP request per query against
    ``{base_url}?q=<query>&limit=<limit>`` with ``Authorization: Bearer``.
    The response must be ``{"results": [{"name"/"title", "url"/"website",
    ...}, ...]}``. Each result is mapped to a ``Candidate`` whose provenance
    records the live source (``source = djs_http_search:<host>``) and whose
    geography is carried from the result only when actually present.

    ``fetcher`` is an optional injectable HTTP client exposing
    ``fetch(url, *, timeout=None) -> FetchResult`` (the same seam the radio
    engine uses); a stdlib default is constructed when omitted so this can
    run in production.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        fetcher=None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = (base_url or os.environ.get(DJS_SEARCH_BASE_URL_ENV)
                         or "").strip()
        self.api_key = api_key or os.environ.get(DJS_SEARCH_API_KEY_ENV) or ""
        self.timeout = timeout
        if fetcher is None:
            from crawler.http import StdlibHttpFetcher
            fetcher = StdlibHttpFetcher(
                timeout_seconds=timeout, max_bytes=2_000_000,
                user_agent=DEFAULT_USER_AGENT, respect_robots=False,
            )
        self._fetcher = fetcher
        self._host = self._derive_host()

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def _derive_host(self) -> str:
        if not self.base_url:
            return "unconfigured"
        try:
            return urlsplit(self.base_url).netloc or "search"
        except ValueError:
            return "search"

    def _candidate_from_result(self, entry: dict, request: DiscoveryRequest,
                               query: str) -> Candidate | None:
        if not isinstance(entry, dict):
            return None
        url = str(entry.get("url") or entry.get("website") or "").strip()
        if not url:
            return None
        source_url = str(entry.get("source_url")
                         or entry.get("booking_url")
                         or entry.get("profile_url") or "").strip() or url
        return Candidate(
            title=_clean_title(entry.get("name") or entry.get("title"), url),
            url=url,
            snippet=_clean(entry.get("snippet") or entry.get("description"))
            or "",
            source=f"djs_http_search:{self._host}",
            source_type=SourceType.SEARCH_SOURCE,
            country=_clean_geo(entry.get("country")),
            state_or_region=_clean_geo(entry.get("state_or_region")
                                       or entry.get("state_or_region")),
            city=_clean_geo(entry.get("city")),
            source_url=source_url,
        )

    def search(
        self, request: DiscoveryRequest, queries: Sequence[str],
    ) -> list[Candidate]:
        if not self.configured:
            raise DiscoveryProviderNotConfigured(
                "DJ discovery provider is not configured: set "
                f"{DJS_SEARCH_BASE_URL_ENV} (and {DJS_SEARCH_API_KEY_ENV} if "
                "the provider requires a key) in .env — see .env.example")

        out: list[Candidate] = []
        for query in queries:
            if len(out) >= request.limit:
                break
            sep = "&" if "?" in self.base_url else "?"
            url = f"{self.base_url}{sep}{urlencode({'q': query})}"
            headers = {
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept": "application/json",
            }
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            try:
                result = self._fetcher.fetch(url, timeout=self.timeout)
            except Exception as exc:
                logger.warning("djs_http_search query failed: %s", exc)
                continue
            if not getattr(result, "ok", False) or not getattr(
                    result, "body", None):
                continue
            try:
                payload = json.loads(result.body)
            except (ValueError, TypeError):
                continue
            entries = payload.get("results")
            if entries is None and isinstance(payload, dict):
                entries = payload.get("djs")
            if not isinstance(entries, list):
                continue
            for entry in entries:
                candidate = self._candidate_from_result(
                    entry, request, query)
                if candidate is None:
                    continue
                out.append(candidate)
                if len(out) >= request.limit:
                    break
        return out


def load_http_search_entries(
    base_url: str | None = None, api_key: str | None = None, *,
    fetcher=None,
) -> HttpSearchDjsProvider:
    """Provider factory honouring env overrides; cron-usable."""
    return HttpSearchDjsProvider(
        base_url=base_url, api_key=api_key, fetcher=fetcher)
