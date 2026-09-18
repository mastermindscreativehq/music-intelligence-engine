"""Radio-station discovery through SerpAPI's Google organic search.

Organization-type twin of ``discovery.djs.serpapi_provider``. The SerpAPI
wire format, response parsing, dedup-by-URL, and the hard no-fabrication
rules live in ONE place (``SerpApiSearchDjsProvider``); this module reuses
them verbatim and overrides only the honest labels/messages so station runs
never claim to be DJ runs.

Authentication is identical and credential-gated: ``SERPAPI_API_KEY`` (and
optionally ``SERPAPI_BASE_URL``). When unset, ``configured == False`` and
``search()`` raises ``DiscoveryProviderNotConfigured`` — the job then
answers the honest "station discovery provider is not configured" and
NEVER fabricates stations, contacts, or geography.

The API key travels only as the ``api_key`` query parameter; it is never
logged or stored.
"""

from __future__ import annotations

from discovery.djs.serpapi_provider import (
    DEFAULT_BASE_URL,
    SERPAPI_API_KEY_ENV,
    SERPAPI_BASE_URL_ENV,
    SerpApiSearchDjsProvider,
)

RADIO_PROVIDER_USER_AGENT = (
    "MusicIntelligenceEngine/0.5 (+independent station discovery)")

__all__ = [
    "DEFAULT_BASE_URL",
    "SERPAPI_API_KEY_ENV",
    "SERPAPI_BASE_URL_ENV",
    "RADIO_PROVIDER_USER_AGENT",
    "SerpApiSearchRadioProvider",
    "load_serpapi_search_entries",
]


class SerpApiSearchRadioProvider(SerpApiSearchDjsProvider):
    """REAL public-web station discovery through SerpAPI Google search.

    Same transport and parsing as the DJ provider; only the provider label
    and the not-configured/unreachable messages differ. Candidates carry
    honest provenance (``source = serpapi_google``) and geography ONLY when
    the organic result actually contained it.
    """

    provider_label = "radio_station"
    default_user_agent = RADIO_PROVIDER_USER_AGENT

    def _not_configured_message(self) -> str:
        return (
            "radio station discovery provider is not configured: set "
            f"{SERPAPI_API_KEY_ENV} (and optionally {SERPAPI_BASE_URL_ENV})"
            " in .env — see .env.example")

    def _unreachable_message(self) -> str:
        return (
            "radio station discovery provider is not configured: SerpAPI is "
            "not reachable from this host (no real discovery was "
            "performed and nothing was fabricated).")


def load_serpapi_search_entries(
    base_url: str | None = None,
    api_key: str | None = None,
    *,
    fetcher=None,
) -> SerpApiSearchRadioProvider:
    """Provider factory honouring env overrides; cron-usable."""
    return SerpApiSearchRadioProvider(
        base_url=base_url, api_key=api_key, fetcher=fetcher)
