"""Automation-facing DJ discovery job (Phase 11).

Thin adapter that reuses EVERY existing DJ discovery building block —
provider selector, ``DjsDiscoveryEngine``, the deterministic query builder,
and ``ingest_dj_discovery`` — so the automation path and the interactive
path stay one code path. Only DJ-specific wiring lives here; the generic
envelope validation / run ledger lives in ``discovery.jobs``.

Secrets stay where they live today: the SerpAPI key travels only inside the
provider's outbound query string and is never written to a report, a log
line, or a stored ``config``.
"""

from __future__ import annotations

from typing import Any

from discovery.djs.http_provider import DiscoveryProviderNotConfigured
from discovery.djs.pipeline import DjsDiscoveryEngine
from discovery.djs.selector import select_djs_discovery_provider
from discovery.models import DiscoveryRequest

from djs.service import ingest_dj_discovery


def _provider_label(provider: Any) -> str:
    """Honest, side-effect-free provider label for the run ledger."""
    cls = type(provider).__name__
    if cls == "SerpApiSearchDjsProvider":
        return "serpapi_google"
    if cls == "HttpSearchDjsProvider":
        host = getattr(provider, "_host", None)
        return f"djs_http_search:{host}" if host else "http_search"
    return cls or "unknown"


def run_dj_discovery_job(repository, config: dict, *, fetcher: Any = None) -> dict:
    """Execute one DJ discovery job; returns the run counters.

    ``config`` holds ONLY the shared ``DiscoveryRequest`` fields (the
    dispatcher strips the ``organization_type``/``patterns`` envelope). When
    no live provider is configured the honest
    :class:`DiscoveryProviderNotConfigured` is raised — nothing is ever
    fabricated and nothing is ingested.
    """
    provider = select_djs_discovery_provider(fetcher=fetcher)
    if not provider.configured:
        raise DiscoveryProviderNotConfigured(
            "DJ discovery provider is not configured: set SERPAPI_API_KEY "
            "(or DJS_SEARCH_BASE_URL / DJS_SEARCH_API_KEY) in .env — see "
            ".env.example")

    request = DiscoveryRequest.from_dict(config)
    engine = DjsDiscoveryEngine(provider, fetcher=fetcher)
    result = engine.run(request)
    ingest = ingest_dj_discovery(repository, result.records)
    return {
        "provider": _provider_label(provider),
        "queries_run": len(result.queries),
        "candidates_found": len(result.records),
        "records_ingested": ingest["created"] + ingest["merged"],
        "duplicates": ingest["duplicates_removed"] + ingest["merged"],
        "failures": len(result.failures) + len(ingest["failures"]),
    }