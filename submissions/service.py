"""Phase 8 submission orchestration shared by every API server adapter.

Single business locus so the stdlib reference server, the single-origin
webapp, and the FastAPI application stay behaviorally identical: each
adapter is a thin translation of these functions into its own HTTP
dialect (mirroring how ``backend.routes`` + ``backend.contracts`` serve
the Phase 4-7 surface).

Functions here NEVER send anything and never expose storage paths; they
deal in opaque track ids, repository rows, and check records only.
"""

from __future__ import annotations

import hashlib
import os
import time

from discovery.models import utc_now_iso

from submissions.links import SsrfBlocked, assert_public_url
from submissions.storage import LocalTrackStore, TrackStore, track_id_for
from submissions.validation import DEFAULT_MAX_BYTES, sanitize_filename, \
    validate_upload

TRACK_STATUSES = ("ready", "quarantined", "archived")

__all__ = [
    "DEFAULT_MAX_BYTES", "TRACK_STATUSES", "TrackRejected",
    "TrackTooLarge", "default_link_fetcher", "default_track_store",
    "get_track", "link_history", "list_tracks", "run_link_checks",
    "station_submission", "track_id_for", "upload_track",
]


class TrackTooLarge(ValueError):
    """Payload exceeded the configured ceiling; nothing was stored."""


class TrackRejected(ValueError):
    """Payload failed validation; persisted as a quarantined audit row."""


def default_track_store() -> TrackStore:
    """Local store rooted at SUBMISSION_STORAGE_ROOT (or repo default)."""
    root = os.environ.get("SUBMISSION_STORAGE_ROOT")
    return LocalTrackStore(root) if root else LocalTrackStore()


def default_link_fetcher():
    """Polite fetcher reusing the crawler's env conventions."""
    from crawler.http import StdlibHttpFetcher
    return StdlibHttpFetcher(
        timeout_seconds=float(os.environ.get("CRAWLER_TIMEOUT_SECONDS")
                              or 15),
        rate_limit_seconds=float(
            os.environ.get("CRAWLER_RATE_LIMIT_SECONDS") or 1.0),
    )


# -- tracks -------------------------------------------------------------------

def upload_track(repository, store, data, filename=None,
                 max_bytes=DEFAULT_MAX_BYTES):
    """Validate, store (when accepted), persist, and return a track row.

    - Accepted payloads are content-addressed into *store* and saved with
      status ``ready``. Re-uploading identical bytes is idempotent.
    - Oversize raises :class:`TrackTooLarge` (no DB row — the payload was
      refused before it could be characterized).
    - Wrong-content payloads raise :class:`TrackRejected` AFTER an
      auditability row is persisted with status ``quarantined``.
    """
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise TrackRejected("empty upload")
    data = bytes(data)
    verdict = validate_upload(data, max_bytes=max_bytes)
    digest = hashlib.sha256(data).hexdigest()
    track_id = track_id_for(digest)
    if not verdict["accepted"]:
        if "exceeds" in (verdict["reason"] or ""):
            raise TrackTooLarge(verdict["reason"])
        repository.save_track({
            "track_id": track_id,
            "sha256": digest,
            "original_filename": sanitize_filename(filename),
            "size_bytes": len(data),
            "content_type": "application/octet-stream",
            "status": "quarantined",
            "reject_reason": verdict["reason"],
        })
        raise TrackRejected(verdict["reason"])
    key = store.put(data)
    if key != track_id:                                   # defensive
        raise RuntimeError("storage key mismatch")
    return repository.save_track({
        "track_id": track_id,
        "sha256": digest,
        "original_filename": sanitize_filename(filename),
        "size_bytes": len(data),
        "content_type": "audio/mpeg",
        "status": "ready",
    })


def get_track(repository, track_id):
    return repository.get_track(track_id)


def list_tracks(repository, limit=50, offset=0, status=None):
    if status is not None and status not in TRACK_STATUSES:
        raise ValueError(
            "'status' must be one of: " + ", ".join(TRACK_STATUSES))
    rows, total = repository.list_tracks(limit=limit, offset=offset,
                                         status=status)
    return rows, total


# -- link accessibility -------------------------------------------------------

def station_submission(repository, identity_key):
    """Stored submission path view; raises LookupError when unknown."""
    if repository.get_station(identity_key) is None:
        raise LookupError(f"unknown station {identity_key!r}")
    return {
        "identity_key": identity_key,
        "submission": repository.get_submission(identity_key),
    }


def last_checks_by_target(rows):
    """Newest check per (url, target_kind); input must be newest-first."""
    seen, latest = set(), []
    for row in rows:
        marker = (row["url"], row["target_kind"])
        if marker in seen:
            continue
        seen.add(marker)
        latest.append(row)
    return latest


def run_link_checks(repository, fetcher, identity_key, *,
                    allow_private=False, resolve=None):
    """Check every stored target now; append rows; return a summary view.

    ``allow_private`` exists ONLY for loopback test harnesses; production
    adapters leave it False so private addresses are refused without any
    network contact.
    """
    targets = extract_targets(repository, identity_key)
    checked = []
    for url, kind in targets:
        entry = _check_one(fetcher, url, kind, allow_private=allow_private,
                           resolve=resolve)
        repository.record_link_check(identity_key, entry)
        checked.append(entry)
    reachable = sum(1 for entry in checked if entry["ok"])
    return {
        "identity_key": identity_key,
        "targets": len(targets),
        "reachable": reachable,
        "checks": checked,
    }


def extract_targets(repository, identity_key):
    from submissions.links import extract_check_targets
    if repository.get_station(identity_key) is None:
        raise LookupError(f"unknown station {identity_key!r}")
    return extract_check_targets(repository.get_submission(identity_key))


def _check_one(fetcher, url, kind, *, allow_private, resolve):
    entry = {"url": url, "target_kind": kind, "ok": False, "status": None,
             "error_kind": None, "latency_ms": None,
             "checked_at": utc_now_iso()}
    if not allow_private:
        try:
            assert_public_url(url, resolve=resolve)
        except SsrfBlocked as exc:
            entry["error_kind"] = "ssrf_blocked"
            entry["error_message"] = str(exc)
            return entry
    started = time.monotonic()
    result = fetcher.fetch(url)
    entry["latency_ms"] = int((time.monotonic() - started) * 1000)
    entry["ok"] = bool(result.ok)
    entry["status"] = result.status
    entry["error_kind"] = result.error_kind
    return entry


def link_history(repository, identity_key, limit=50):
    """Newest-first accessibility history (LookupError when unknown)."""
    if repository.get_station(identity_key) is None:
        raise LookupError(f"unknown station {identity_key!r}")
    rows = repository.get_link_checks(identity_key, limit=limit)
    return {"identity_key": identity_key, "checks": rows,
            "last_by_target": last_checks_by_target(rows)}
