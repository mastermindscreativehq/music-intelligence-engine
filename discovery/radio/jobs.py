"""Automation-facing station discovery job.

Thin adapter that reuses EVERY existing station building block — the
deterministic provider selector, ``RadioDiscoveryEngine`` (Phase 2),
``EnrichmentEngine`` (Phase 3), the deterministic query builder, and
``ingest_station_discovery`` — so the automation path and the interactive
path stay one code path. Only radio wiring lives here; the generic envelope
validation / run ledger lives in ``discovery.jobs``.

Flow: provider -> RadioDiscoveryEngine -> (post-fetch hard gate: only
records the site evidence does NOT reject proceed) -> EnrichmentEngine ->
ingest (``repository.ingest_intelligence``). The enrichment stage turns
discovered station records into IntelligenceRecords carrying useful-page,
submission, contact and fetch provenance — plus an evidence-backed
``outreach_readiness`` projection — before the single existing persistence
path stores them. The hard gate is verdict =/= rejected: ``rejected``
records are reported and never persisted, while ``qualified`` AND
``needs_review`` records are enriched, readiness-marked (outreach_ready /
needs_review + reason), and persisted. ``needs_review`` rows remain hidden
from normal listings by the existing quarantine read filter yet stay
reviewable — they are never silently treated as outreach-ready.

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
from discovery.radio.qualify import QUALIFIED, REJECTED
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

    # The post-fetch hard gate decides what is stored: only records whose
    # site carried verifiable station evidence reach enrichment + ingest.
    # "verified" here means the record is NOT rejected — a needs_review
    # record still has evidence to review (its readiness projection carries
    # the reason) and must be persisted so it is reviewable, never silently
    # treated as qualified. Rejected rows are reported and never persisted.
    qualified: list[dict] = []
    needs_review: list[dict] = []
    rejected: list[dict] = []
    for record in discovery.records:
        qualification = (record.get("raw_metadata") or {}).get("qualification")
        verdict = qualification.get("verdict") \
            if isinstance(qualification, dict) else None
        if verdict == REJECTED:
            rejected.append({
                "url": record.get("website") or record.get("name"),
                "name": record.get("name"),
                "verdict": verdict,
                "kind": (qualification or {}).get("kind")
                if isinstance(qualification, dict) else "",
                "reason": (qualification or {}).get("reason", "")
                if isinstance(qualification, dict) else "",
                "readiness": "rejected",
            })
            continue
        if verdict == QUALIFIED:
            qualified.append(record)
        else:
            needs_review.append(record)

    enrichment = _build_enrichment(fetcher)
    enriched = enrichment.enrich_records(qualified + needs_review)

    ingest = ingest_station_discovery(repository, enriched.records)
    return {
        "provider": _provider_label(provider),
        "queries_run": len(discovery.queries),
        "candidates_found": len(discovery.records),
        "candidates_qualified": len(qualified),
        "needs_review_stored": len(needs_review),
        "quarantined": len(rejected),
        "records_ingested": int(ingest.get("records_accepted") or 0),
        "duplicates": int(ingest.get("duplicates_removed") or 0)
                      + int(ingest.get("merged") or 0),
        "failures": len(discovery.failures) + enriched.failure_count
                    + len(ingest.get("failures") or []),
    }
