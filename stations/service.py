"""Station intelligence service.

Adapter-agnostic helpers over a repository (``PersistenceService`` /
``PostgresStorage``), mirroring ``djs.service``. Like the rest of the
engine, nothing here crawls, verifies, or invents facts.

``ingest_station_discovery`` is the single intake path for discovered
``RadioIntelligenceRecord`` dicts:

- The batch is persisted through the ONE existing repository method
  (``ingest_intelligence``), which validates, normalizes, and upserts on the
  shared ``identity_key`` — no second station schema, no parallel storage.
- That upsert is fill-only/additive and idempotent: re-running discovery for
  the same station never duplicates it and never erases stored facts.
- Records that fail validation are reported, never dropped silently.
"""

from __future__ import annotations

from discovery.radio.schema import ORGANIZATION_TYPE
from enrichment.dedupe import identity_key as station_identity_key

DEFAULT_SOURCE = "station_discovery"

__all__ = ["DEFAULT_SOURCE", "ingest_station_discovery"]


def _identity_string(record: dict) -> str | None:
    """Repository identity key for a record, or None when undeterminable."""
    try:
        kind, value = station_identity_key(record)
    except Exception:
        return None
    if not kind or not value:
        return None
    return f"{kind}:{value}"


def _with_org_type(record: dict) -> dict:
    if record.get("organization_type"):
        return record
    return {**record, "organization_type": ORGANIZATION_TYPE}


def ingest_station_discovery(repository, records, *,
                             source: str = DEFAULT_SOURCE) -> dict:
    """Store source-backed station intelligence (create / merge / dedupe).

    Returns::

        {"created": n, "merged": n, "duplicates_removed": n,
         "records_accepted": n, "batches": [ingestion report, ...],
         "failures": [{"stage", "error_kind", "message", ...}, ...]}

    ``merged`` counts records whose identity already existed in storage
    (re-observed, upserted in place); ``created`` counts net-new stations.
    The stored ingestion report(s) are echoed for auditability.
    """
    cleaned = [r for r in (records or []) if isinstance(r, dict)]
    normalized = [_with_org_type(r) for r in cleaned]
    if not normalized:
        return {
            "created": 0, "merged": 0, "duplicates_removed": 0,
            "records_accepted": 0, "batches": [], "failures": [],
        }

    existing = 0
    for record in normalized:
        key = _identity_string(record)
        if key and repository.get_station(key) is not None:
            existing += 1

    report = repository.ingest_intelligence(normalized, source=source)
    accepted = int(getattr(report, "records_accepted", 0) or 0)
    merged = min(existing, accepted)
    batches = [report.to_dict()] if hasattr(report, "to_dict") else [report]
    failures = [dict(f) for f in (getattr(report, "failures", None) or [])]

    return {
        "created": max(0, accepted - merged),
        "merged": merged,
        "duplicates_removed": 0,
        "records_accepted": accepted,
        "batches": batches,
        "failures": failures,
    }
