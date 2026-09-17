"""Deterministic, honest DJ-discovery provider selection (Phase 4c seam).

Contract: selection is a pure function of *configuration*, never of a seed
list. Both candidate providers speak the same ``Candidate`` /
``DiscoveryRequest`` contract the DJ engine consumes, so swapping the
selector never changes a route signature, a model, or a response body.

Selection (first match wins — deterministic, explicit, honest):

1. ``SERPAPI_API_KEY`` configured (optionally ``SERPAPI_BASE_URL``) ->
   the dedicated ``SerpApiSearchDjsProvider`` (real SerpAPI Google search).
   Provenance records the live source honestly: ``source =
   serpapi_google``. No contact channel, no geography, no station is ever
   fabricated; geography is carried ONLY when the organic result actually
   contains it.

2. Otherwise, when the preserved generic HTTP-search seam is configured
   (``DJS_SEARCH_BASE_URL`` or ``DJS_SEARCH_API_KEY``) -> the existing
   ``HttpSearchDjsProvider`` via ``load_http_search_entries`` — unchanged,
   tests preserved.

3. Otherwise -> an honest provider that reports ``configured == False``,
   reusing ``DiscoveryProviderNotConfigured``; the route answers 503
   (``dj_discovery_provider_not_configured``). Nothing is ever fabricated:
   no cause seed, no station derivation, no auto-outreach, no DJ, no
   contact channel, no email/phone/social handle/geography.

The selector performs NO network call and requires NO real key in tests;
the fetcher seam stays injectable in both branches.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from discovery.djs.http_provider import load_http_search_entries
from discovery.djs.serpapi_provider import (
    SERPAPI_API_KEY_ENV,
    SERPAPI_BASE_URL_ENV,
    SerpApiSearchDjsProvider,
    load_serpapi_search_entries,
)

logger = logging.getLogger("mie.discovery.djs.selector")


def select_djs_discovery_provider(*, fetcher: Any = None) -> Any:
    """Honest producer selection: SerpAPI -> generic HTTP -> unconfigured.

    ``fetcher`` is the injectable HTTP client seam the DJ engine uses; it is
    forwarded unchanged to whichever provider is selected so every provider
    stays testable without a real key or a live request.
    """
    api_key = os.environ.get(SERPAPI_API_KEY_ENV, "").strip()
    base_url = os.environ.get(SERPAPI_BASE_URL_ENV, "").strip()
    if api_key:
        provider = load_serpapi_search_entries(
            base_url=base_url or None, api_key=api_key, fetcher=fetcher)
        logger.info("select_djs_discovery_provider -> serpapi_google "
                    "(SerpApiSearchDjsProvider)")
        return provider
    provider = load_http_search_entries(fetcher=fetcher)
    logger.info("select_djs_discovery_provider -> http_search "
                "(HttpSearchDjsProvider); configured=%s",
                bool(getattr(provider, "configured", False)))
    return provider
