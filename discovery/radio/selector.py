"""Deterministic, honest station-discovery provider selection.

Mirrors ``discovery.djs.selector``: selection is a pure function of
*configuration*, never of a seed list. The selected provider speaks the same
``Candidate`` / ``DiscoveryRequest`` contract the radio engine consumes, so
swapping configuration never changes a route signature, a model, or a
response body.

Selection (deterministic, first match wins):

1. ``SERPAPI_API_KEY`` configured (optionally ``SERPAPI_BASE_URL``) ->
   ``SerpApiSearchRadioProvider`` (real SerpAPI Google search). Provenance
   records the live source honestly: ``source = serpapi_google``.

2. Otherwise -> the same provider with an empty API key, so ``configured ==
   False``; the runner raises ``DiscoveryProviderNotConfigured`` and the API
   answers 503 (``station_discovery_provider_not_configured``). Nothing is
   ever fabricated: no station, no contact, no email/phone/social, no
   geography. The deterministic seed provider stays TEST-ONLY.

The selector performs NO network call and requires NO real key in tests; the
fetcher seam stays injectable.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from discovery.djs.serpapi_provider import (
    SERPAPI_API_KEY_ENV,
    SERPAPI_BASE_URL_ENV,
)
from discovery.radio.serpapi_provider import (
    SerpApiSearchRadioProvider,
    load_serpapi_search_entries,
)

logger = logging.getLogger("mie.discovery.radio.selector")


def select_radio_discovery_provider(*, fetcher: Any = None) -> Any:
    """Honest producer selection: SerpAPI -> unconfigured (never a seed)."""
    api_key = os.environ.get(SERPAPI_API_KEY_ENV, "").strip()
    base_url = os.environ.get(SERPAPI_BASE_URL_ENV, "").strip()
    if api_key:
        provider = load_serpapi_search_entries(
            base_url=base_url or None, api_key=api_key, fetcher=fetcher)
        logger.info("select_radio_discovery_provider -> serpapi_google "
                    "(SerpApiSearchRadioProvider)")
        return provider
    provider = SerpApiSearchRadioProvider(
        base_url="", api_key="", fetcher=fetcher)
    logger.info("select_radio_discovery_provider -> unconfigured "
                "(SerpApiSearchRadioProvider); configured=%s",
                bool(getattr(provider, "configured", False)))
    return provider
