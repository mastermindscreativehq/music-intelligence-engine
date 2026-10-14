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
from urllib.parse import urlparse

from discovery.models import utc_now_iso

from submissions.links import SsrfBlocked, assert_public_url
from submissions.storage import LocalTrackStore, TrackStore, track_id_for
from submissions.validation import DEFAULT_MAX_BYTES, sanitize_filename, \
    validate_upload

TRACK_STATUSES = ("ready", "quarantined", "archived")
LISTEN_URL_SCHEMES = ("http", "https")
# Upper bound for operator-authored release text (title / artist / genre).
# Generous enough for real names while keeping a release row bounded.
MAX_RELEASE_TEXT = 300
# Mutable, operator-owned presentation fields. The outreach handoff and My
# Music both read these; the API PATCH accepts any subset of them.
EDITABLE_TRACK_FIELDS = ("title", "artist_name", "genre", "listen_url",
                         "song_story")

__all__ = [
    "DEFAULT_MAX_BYTES", "EDITABLE_TRACK_FIELDS", "MAX_RELEASE_TEXT",
    "TRACK_STATUSES", "TrackRejected", "TrackTooLarge", "create_release",
    "default_link_fetcher", "default_track_store", "get_track",
    "link_history", "list_tracks", "normalize_genre", "normalize_listen_url",
    "normalize_song_story", "run_link_checks", "station_submission",
    "track_id_for", "update_track_fields", "update_track_listen_url",
    "update_track_song_story", "upload_track",
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
                 max_bytes=DEFAULT_MAX_BYTES, owner_user_id=None):
    """Validate, store (when accepted), persist, and return a track row.

    - Accepted payloads are content-addressed into *store* and saved with
      status ``ready``. Re-uploading identical bytes is idempotent and
      never wipes an operator's saved listening URL or song story.
    - Oversize raises :class:`TrackTooLarge` (no DB row — the payload was
      refused before it could be characterized).
    - Wrong-content payloads raise :class:`TrackRejected` AFTER an
      auditability row is persisted with status ``quarantined``.

    ``owner_user_id`` stamps first-class ownership when an authenticated
    user is known; it is omitted (NULL) for anonymous/legacy uploads so the
    historical single-operator behavior is preserved, and it is never
    overwritten on a repeat upload by an anonymous caller.
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
        row = {
            "track_id": track_id,
            "sha256": digest,
            "original_filename": sanitize_filename(filename),
            "size_bytes": len(data),
            "content_type": "application/octet-stream",
            "status": "quarantined",
            "reject_reason": verdict["reason"],
        }
        if owner_user_id:
            row["owner_user_id"] = owner_user_id
        repository.save_track(row)
        raise TrackRejected(verdict["reason"])
    key = store.put(data)
    if key != track_id:                                   # defensive
        raise RuntimeError("storage key mismatch")
    row = {
        "track_id": track_id,
        "sha256": digest,
        "original_filename": sanitize_filename(filename),
        "size_bytes": len(data),
        "content_type": "audio/mpeg",
        "status": "ready",
    }
    if owner_user_id:
        row["owner_user_id"] = owner_user_id
    return repository.save_track(row)


def get_track(repository, track_id):
    return repository.get_track(track_id)


def normalize_listen_url(value):
    """Return the canonical listening URL, or None to clear it.

    Only absolute ``http``/``https`` URLs are accepted — a bare host, a
    scheme-relative path, or a non-web scheme is refused with ValueError so
    the operator is told to fix the link instead of us inventing one. An
    empty/whitespace value clears the field (honest absence).
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme not in LISTEN_URL_SCHEMES or not parsed.netloc:
        raise ValueError(
            "'listen_url' must be an absolute http(s) URL")
    return text


def update_track_listen_url(repository, track_id, listen_url):
    """Set/clear one release's official listening URL; None when unknown.

    Reuses the existing track upsert (``save_track``) so the row keeps its
    immutable fields and ``created_at`` while ``updated_at`` advances. The
    value is validated first, so a bad link is refused before any write.
    """
    row = repository.get_track(track_id)
    if row is None:
        return None
    updated = dict(row)
    updated["listen_url"] = normalize_listen_url(listen_url)
    updated["updated_at"] = utc_now_iso()
    return repository.save_track(updated)


def normalize_song_story(value):
    """Return the operator's song story / creative brief, or None to clear.

    Free text stored verbatim (whitespace-trimmed). A blank/whitespace value
    clears the field (honest absence). It is the operator's OWN account of
    the song — source material for outreach, never invented or inferred.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def update_track_song_story(repository, track_id, song_story):
    """Set/clear one release's song story; None when the track is unknown."""
    row = repository.get_track(track_id)
    if row is None:
        return None
    updated = dict(row)
    updated["song_story"] = normalize_song_story(song_story)
    updated["updated_at"] = utc_now_iso()
    return repository.save_track(updated)


def _normalize_release_text(value, field):
    """Required operator-authored release text (title / artist_name).

    Trimmed and bounded; a missing or blank value is refused so a release
    never loses its identity to an accidental empty submit.
    """
    text = "" if value is None else str(value).strip()
    if not text:
        raise ValueError(f"'{field}' is required")
    if len(text) > MAX_RELEASE_TEXT:
        raise ValueError(
            f"'{field}' must be at most {MAX_RELEASE_TEXT} characters")
    return text


def normalize_genre(value):
    """Optional release genre; trimmed, blank clears it (honest absence)."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > MAX_RELEASE_TEXT:
        raise ValueError(
            f"'genre' must be at most {MAX_RELEASE_TEXT} characters")
    return text


def create_release(repository, payload, owner_user_id=None):
    """Create (or re-use) a release from metadata alone — no binary upload.

    My Music's primary workflow: an operator records a release by its
    ``title``, ``artist_name`` and ``music_url`` (all required), with an
    optional ``storyline`` and ``genre``. The row is persisted through the
    SAME ``save_track`` upsert as an uploaded asset, so it is listed,
    edited, used by outreach and deleted identically. The opaque track id
    is derived from the release's canonical identity
    (title + artist + listening URL), so re-submitting an identical release
    is idempotent rather than a silent duplicate; ``sha256``/``size_bytes``
    stay NOT NULL by recording the digest and byte-length of that canonical
    identity (there is no audio blob to hash). All validation happens before
    any write, so a missing required field raises ValueError and stores
    nothing.
    """
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")
    title = _normalize_release_text(payload.get("title"), "title")
    artist_name = _normalize_release_text(
        payload.get("artist_name"), "artist_name")
    music_url = normalize_listen_url(payload.get("music_url"))
    if music_url is None:
        raise ValueError("'music_url' is required")
    storyline = normalize_song_story(payload.get("storyline"))
    genre = normalize_genre(payload.get("genre"))

    canonical = "\n".join((title, artist_name, music_url))
    encoded = canonical.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    row = {
        "track_id": track_id_for(digest),
        "sha256": digest,
        "original_filename": title,
        "size_bytes": len(encoded),
        "content_type": "application/vnd.mie.release+json",
        "status": "ready",
        "title": title,
        "artist_name": artist_name,
        "listen_url": music_url,
    }
    # Optional fields are only written when supplied, so re-submitting an
    # already-stored release never wipes a storyline/genre the operator saved
    # (the same carry-forward rule an identical-byte re-upload follows).
    # Clearing is available through the PATCH edit path.
    if genre is not None:
        row["genre"] = genre
    if storyline is not None:
        row["song_story"] = storyline
    if owner_user_id:
        row["owner_user_id"] = owner_user_id
    return repository.save_track(row)


def update_track_fields(repository, track_id, payload):
    """PATCH one release's operator-owned fields; None when unknown.

    Only keys present in *payload* are changed (an operator may supply any
    subset of title, artist_name, genre, listen_url, song_story); at least
    one editable key is required. Every value is validated before any write,
    so a bad link or a blank required title refuses the whole update and
    leaves the row untouched. Reuses ``save_track`` so immutable fields and
    ``created_at`` survive while ``updated_at`` advances.
    """
    row = repository.get_track(track_id)
    if row is None:
        return None
    editable = [key for key in EDITABLE_TRACK_FIELDS if key in payload]
    if not editable:
        raise ValueError(
            "body must contain one of: " + ", ".join(EDITABLE_TRACK_FIELDS))
    updated = dict(row)
    if "listen_url" in payload:
        updated["listen_url"] = normalize_listen_url(payload["listen_url"])
    if "song_story" in payload:
        updated["song_story"] = normalize_song_story(payload["song_story"])
    if "title" in payload:
        updated["title"] = _normalize_release_text(payload["title"], "title")
    if "artist_name" in payload:
        updated["artist_name"] = _normalize_release_text(
            payload["artist_name"], "artist_name")
    if "genre" in payload:
        updated["genre"] = normalize_genre(payload["genre"])
    updated["updated_at"] = utc_now_iso()
    return repository.save_track(updated)


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
