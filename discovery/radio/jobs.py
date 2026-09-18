"""Automation-facing station discovery job.

Thin adapter that reuses EVERY existing station building block — the
deterministic provider selector, ``RadioDiscoveryEngine`` (Phase 2),
``EnrichmentEngine`` (Phase 3), the deterministic query builder, and
``ingest_station_discovery`` — so the automation path and the interactive
path stay one code path. Only radio wiring lives here; the generic envelope
validation / run ledger lives in ``discovery.jobs``.

Flow: provider -> RadioDiscoveryEngine -> EnrichmentEngine -> ingest
(``repository.ingest_intelligence``). The enrichment stage turns discovered
station records into IntelligenceRecords carrying useful-page, submission,
contact and fetch provenance before the single existing persistence path
stores them.

Secrets stay where they live today: the SerpAPI key travels only inside the
provider's outbound query string and is never written to a report, a log
line, or a stored ``config``.
"""

from __future__ import annotations

from typing import Any

from discovery.djs.http_provider import DiscoveryProviderNotConfigured
from discovery.models import DiscoveryRequest
from discovery.radio.enrich_pipeline import EnrichmentEngine
from discovery.radio.pipeline import RadioDiscoveryEngine
from discovery.radio.selector import select_radio_discovery_provider

from stations.service import ingest_station_discovery


def _provider_label(provider: Any) -> str:
    """Honest, side-effect-free provider label for the run ledger."""
    cls = type(provider).__name__
    if cls in ("SerpApiSearchRadioProvider", "SerpApiSearchDjsProvider"):
        return "serpapi_google"
    return cls or "unknown"


def _build_enrichment(fetcher: Any) -> EnrichmentEngine:
    """Share an injected fetcher (tests/offline); go live in production."""
    if fetcher is not None:
        return EnrichmentEngine(fetcher=fetcher)
    engine = EnrichmentEngine()
    engine.set_live()
    return engine


def run_radio_discovery_job(repository, config: dict, *,
                            fetcher: Any = None) -> dict:
    """Execute one station discovery job; returns the run counters.

    ``config`` holds ONLY the shared ``DiscoveryRequest`` fields (the
    dispatcher strips the ``organization_type``/``patterns`` envelope). When
    no live provider is configured the honest
    :class:`DiscoveryProviderNotConfigured` is raised — nothing is ever
    fabricated and nothing is ingested.
    """
    provider = select_radio_discovery_provider(fetcher=fetcher)
    if not provider.configured:
        raise DiscoveryProviderNotConfigured(
            "station discovery provider is not configured: set "
            "SERPAPI_API_KEY (and optionally SERPAPI_BASE_URL) in .env — "
            "see .env.example")

    request = DiscoveryRequest.from_dict(config)
    engine = RadioDiscoveryEngine(provider, fetcher=fetcher)
    discovery = engine.run(request)

    enrichment = _build_enrichment(fetcher)
    enriched = enrichment.enrich_records(discovery.records)

    ingest = ingest_station_discovery(repository, enriched.records)
    return {
        "provider": _provider_label(provider),
        "queries_run": len(discovery.queries),
        "candidates_found": len(discovery.records),
        "records_ingested": int(ingest.get("records_accepted") or 0),
        "duplicates": int(ingest.get("duplicates_removed") or 0)
                      + int(ingest.get("merged") or 0),
        "failures": len(discovery.failures) + enriched.failure_count
                    + len(ingest.get("failures") or []),
    }
