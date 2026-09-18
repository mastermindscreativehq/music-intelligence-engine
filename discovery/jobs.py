"""Reusable, automation-facing discovery job dispatcher (Phase 11).

n8n (or any scheduler) POSTs ONE structured job; this module validates the
envelope, dispatches to the organization-type runner (``discovery/<type>/``),
and persists the run to the repository's ``discovery_jobs`` ledger so every
automated run answers: when it ran, on what configuration, through which
provider, how many queries/candidates/records/duplicates/failures, and
whether it completed.

Job envelope (JSON object):

    {
      "organization_type": "dj",       # required; 'dj' | 'radio' | 'station'
      "patterns": [...],               # RESERVED, optional (per-type)
      ...DiscoveryRequest fields...    # query, genre, country,
                                       #   state_or_region, city, language,
                                       #   limit (station_type)
    }

The shared ``DiscoveryRequest`` contract is reused verbatim: no second,
incompatible request model is invented. Per-type query construction stays in
the existing deterministic builders (e.g. ``discovery.djs.pipeline``);
``patterns`` is accepted, validated, and recorded for audit as a reserved
extension lane — today's DJ runner keeps using the deterministic builder,
so a job can never inject fabricated query intent.

Rules that must NEVER be broken here:

- No fabricated results, geography, emails, phones, handles, or station
  affiliations — the underlying pipeline only stores observed facts.
- Provider failures are recorded as failures, never invented as results.
- The SerpAPI key is never written to a report, a stored config, or a log.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable

from discovery import events as ev
from discovery.djs.http_provider import DiscoveryProviderNotConfigured
from discovery.models import DiscoveryRequest, utc_now_iso

logger = logging.getLogger("mie.discovery.jobs")

# Organization types the dispatcher can run today. Adding a future type is a
# new ``discovery/<type>/jobs.py`` runner registered below — the API surface,
# the run ledger, and the envelope do not change.
#
# ``station`` is an accepted alias for ``radio``: station deep-research is the
# radio target type (``discovery.radio``); both names select the same runner.
SUPPORTED_ORGANIZATION_TYPES = ("dj", "radio", "station")

# Fields that belong to the automation envelope, not to DiscoveryRequest.
_ENVELOPE_FIELDS = frozenset(("organization_type", "patterns"))

_PATTERN_MAX_LEN = 200
_PATTERN_MAX_ITEMS = 20

_RUNNERS: dict[str, Callable[..., dict]] = {}


def _load_runners() -> dict[str, Callable[..., dict]]:
    """Lazily import the per-type runners (keeps imports stdlib-only)."""
    if not _RUNNERS:
        from discovery.djs.jobs import run_dj_discovery_job
        from discovery.radio.jobs import run_radio_discovery_job
        _RUNNERS["dj"] = run_dj_discovery_job
        _RUNNERS["radio"] = run_radio_discovery_job
        _RUNNERS["station"] = run_radio_discovery_job
    return _RUNNERS


def _validate_patterns(raw: Any) -> list[str] | None:
    """Validate the reserved ``patterns`` extension lane (or None)."""
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError("'patterns' must be a list of strings")
    cleaned: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("'patterns' entries must be non-empty strings")
        if len(item) > _PATTERN_MAX_LEN:
            raise ValueError(
                f"'patterns' entries must be <= {_PATTERN_MAX_LEN} characters")
        text = " ".join(item.split())
        if text and text not in cleaned:
            cleaned.append(text)
        if len(cleaned) > _PATTERN_MAX_ITEMS:
            raise ValueError(
                f"'patterns' must contain at most {_PATTERN_MAX_ITEMS} entries")
    return cleaned or None


def _build_config(payload: Any) -> tuple[str, dict, list[str] | None]:
    """Split + validate the job envelope into (org_type, config, patterns).

    ``config`` contains ONLY shared ``DiscoveryRequest`` fields; the shared
    contract's ``from_dict`` runs here so malformed jobs fail fast (400)
    before any provider or repository call.
    """
    if not isinstance(payload, dict):
        raise ValueError("job body must be a JSON object")
    org_type = payload.get("organization_type")
    if not isinstance(org_type, str) or not org_type.strip():
        raise ValueError(
            "'organization_type' is required and must be a non-empty string")
    org_type = org_type.strip().lower()
    if org_type not in SUPPORTED_ORGANIZATION_TYPES:
        raise ValueError(
            f"unsupported organization_type {org_type!r}; supported: "
            + ", ".join(SUPPORTED_ORGANIZATION_TYPES))

    request_fields = {k: v for k, v in payload.items()
                      if k not in _ENVELOPE_FIELDS}
    pattern_list = _validate_patterns(payload.get("patterns"))
    request = DiscoveryRequest.from_dict(request_fields)  # raises on bad input
    config = request.to_dict()
    if pattern_list:
        config["patterns"] = pattern_list  # reserved; stored for audit only
    return org_type, config, pattern_list


def _failed_report(run_id: str, org_type: str, config: dict, started: str,
                   status: str, error: str) -> dict:
    return {
        "run_id": run_id,
        "organization_type": org_type,
        "config": config,
        "provider": None,
        "queries_run": 0,
        "candidates_found": 0,
        "records_ingested": 0,
        "duplicates": 0,
        "failures": 0,
        "status": status,
        "error": error,
        "started_at": started,
        "completed_at": utc_now_iso(),
    }


def run_discovery_job(repository, payload: dict, *, fetcher: Any = None) -> dict:
    """Validate + run one automation discovery job; returns its report.

    The report is the machine-readable job result the endpoint returns and
    persists (``record_discovery_job``). Not-configured providers and
    unexpected failures are recorded honestly and re-raised so the caller
    answers a deterministic error code (503 / 500) — never a fabricated run.
    """
    org_type, config, patterns = _build_config(payload)
    run_id = "job_" + uuid.uuid4().hex[:24]
    started = utc_now_iso()
    ev.log_event(logger, "discovery_job_started", run_id=run_id,
                 organization_type=org_type, provider_note=None,
                 reserved_patterns=bool(patterns))

    runner = _load_runners()[org_type]
    try:
        result = runner(repository, dict(config), fetcher=fetcher)
    except DiscoveryProviderNotConfigured as exc:
        report = _failed_report(
            run_id, org_type, config, started, "not_configured", str(exc))
        repository.record_discovery_job(report)
        ev.log_event(logger, "discovery_job_failed", run_id=run_id,
                     status="not_configured", reason=str(exc))
        raise
    except Exception as exc:  # never swallow an unexpected failure
        message = f"{type(exc).__name__}: {exc}"
        report = _failed_report(
            run_id, org_type, config, started, "failed", message)
        try:
            repository.record_discovery_job(report)
        except Exception:
            logger.exception("discovery_job ledger write failed run=%s",
                             run_id)
        ev.log_event(logger, "discovery_job_failed", run_id=run_id,
                     status="failed", reason=message)
        raise

    failures = int(result.get("failures") or 0)
    report = {
        "run_id": run_id,
        "organization_type": org_type,
        "config": config,
        "provider": result.get("provider"),
        "queries_run": int(result.get("queries_run") or 0),
        "candidates_found": int(result.get("candidates_found") or 0),
        "records_ingested": int(result.get("records_ingested") or 0),
        "duplicates": int(result.get("duplicates") or 0),
        "failures": failures,
        "status": "completed_with_failures" if failures else "completed",
        "error": None,
        "started_at": started,
        "completed_at": utc_now_iso(),
    }
    repository.record_discovery_job(report)
    ev.log_event(
        logger, "discovery_job_completed", run_id=run_id,
        organization_type=org_type, status=report["status"],
        provider=report["provider"], queries=report["queries_run"],
        candidates=report["candidates_found"],
        ingested=report["records_ingested"],
        duplicates=report["duplicates"], failures=report["failures"])
    return report