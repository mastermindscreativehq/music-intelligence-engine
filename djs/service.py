"""DJ intelligence service (Phase 4).

Pure, adapter-agnostic helpers over a repository (PersistenceService /
PostgresStorage), mirroring the station/outreach service pattern. Like the
rest of the engine, nothing here crawls, verifies, or invents facts:

- A DJ identity is stored only when supplied (name required; every other
  field optional). A DJ is an INDEPENDENT music professional: ``station_key``
  / ``station_name`` are optional metadata, never a requirement, and never a
  discovery source.
- Every channel (contact / submission / social) is stored as a row that
  requires a value AND an http(s) ``source_url``: provenance is mandatory so
  an address or page is never fabricated.
- Location fields are stored verbatim when provided; the read path reports
  them honestly (unavailable when absent) and never guesses.
- Discovery intake (``ingest_dj_discovery``) merges repeated sightings of
  the same DJ on strong identity evidence (see ``djs.dedupe``) instead of
  creating duplicates.

Outreach identity: a DJ's outreach records use ``identity_key ==
"dj:<dj_id>"`` and ``target_type == "dj"``, which keeps DJ records isolated
from station opportunity history (opportunity/service.py skips non-station
targets).
"""

from __future__ import annotations

import uuid

from discovery.models import utc_now_iso

from djs.dedupe import (deduplicate_djs, dj_fingerprints, merge_dj_records)

# Channel vocabulary stored in ``dj_channels``. A "reach" label is derived
# read-path (never guessed) from the channel name so the console can
# distinguish a verified direct DJ contact, an official submission route, a
# generic contact route, and social routes.
DJ_CHANNEL_TYPES = (
    "email", "website", "instagram", "x", "facebook", "youtube",
    "submission_email", "submission_page", "contact_page",
)

# ("direct", "submission", "webform", "social")
DJ_CHANNEL_ROUTE_CLASS = {
    "email": "direct",
    "submission_email": "submission",
    "submission_page": "submission_webform",
    "contact_page": "contact_webform",
    "website": "website",
    "instagram": "social",
    "x": "social",
    "facebook": "social",
    "youtube": "social",
}

# Honest reach label per channel (presentation only; derived from storage).
DJ_CHANNEL_ROUTE_LABEL = {
    "email": "verified direct DJ contact",
    "submission_email": "official submission contact",
    "submission_page": "official submission route",
    "contact_page": "generic contact route",
    "website": "website",
    "instagram": "social route",
    "x": "social route",
    "facebook": "social route",
    "youtube": "social route",
}

__all__ = [
    "DJ_CHANNEL_TYPES", "DJ_CHANNEL_ROUTE_CLASS", "DJ_CHANNEL_ROUTE_LABEL",
    "create_dj", "dj_detail", "dj_outreach", "get_dj", "list_djs",
    "dj_identity_key", "ingest_dj_discovery",
]


def dj_identity_key(dj_id: str) -> str:
    """Outreach identity for a DJ record ('dj:<id>')."""
    return f"dj:{dj_id}"


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_list(value, field: str, *, max_items: int = 200) -> list[str] | None:
    if value in (None, ""):
        return None
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"'{field}' must be a list")
    cleaned = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"'{field}' entries must be strings")
        text = item.strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned or None


def _validate_channels(channels) -> list[dict]:
    if channels in (None, ""):
        return []
    if not isinstance(channels, (list, tuple)):
        raise ValueError("'channels' must be a list")
    seen = set()
    cleaned: list[dict] = []
    for entry in channels:
        if not isinstance(entry, dict):
            raise ValueError("each channel must be an object")
        channel = _clean(entry.get("channel"))
        if channel not in DJ_CHANNEL_TYPES:
            raise ValueError(f"unsupported channel {channel!r}")
        value = _clean(entry.get("value"))
        if not value:
            raise ValueError(f"channel {channel!r} requires a value")
        source_url = _clean(entry.get("source_url"))
        if not source_url or not (
                source_url.startswith("http://")
                or source_url.startswith("https://")):
            raise ValueError(
                f"channel {channel!r} requires a source http(s) url "
                "(provenance is never optional)")
        verified_at = _clean(entry.get("verified_at"))
        key = (channel, value)
        if key in seen:
            raise ValueError(f"duplicate channel {channel!r} value")
        seen.add(key)
        cleaned.append({
            "channel": channel,
            "value": value,
            "source_url": source_url,
            "verified_at": verified_at,
        })
    return cleaned


def _normalize_dj_payload(payload: dict, now: str = None) -> tuple[dict, list[dict]]:
    """Normalize source-backed DJ evidence into a record + channel list.

    Raises ``ValueError`` on any provenance violation: missing name, missing
    channel source url, unsupported channel, or non-http source urls.
    """
    name = _clean(payload.get("name"))
    if not name:
        raise ValueError("'name' is required")

    genres = _clean_list(payload.get("genres"), "genres")
    formats = _clean_list(payload.get("formats"), "formats")
    source_urls = _clean_list(payload.get("source_urls"), "source_urls")
    if source_urls:
        for url in source_urls:
            if not (url.startswith("http://") or url.startswith("https://")):
                raise ValueError("'source_urls' entries must be http(s) urls")
    verification = payload.get("verification")
    if verification is not None and not isinstance(verification, dict):
        raise ValueError("'verification' must be an object")
    channels = _validate_channels(payload.get("channels"))

    ts = now or utc_now_iso()
    record = {
        "name": name,
        "stage_name": _clean(payload.get("stage_name")),
        "role": _clean(payload.get("role")),
        "program": _clean(payload.get("program")),
        "station_key": _clean(payload.get("station_key")),
        "station_name": _clean(payload.get("station_name")),
        "platform": _clean(payload.get("platform")),
        "country": _clean(payload.get("country")),
        "state_or_region": _clean(payload.get("state_or_region")),
        "city": _clean(payload.get("city")),
        "genres": genres or [],
        "formats": formats or [],
        "source_urls": source_urls or [],
        "verification": verification,
        "discovered_at": _clean(payload.get("discovered_at")) or ts,
        "last_observed_at": _clean(payload.get("last_observed_at")) or ts,
    }
    return record, channels


def create_dj(repository, *, payload: dict, now: str = None) -> dict:
    """Create one DJ profile from operator-supplied evidence.

    ``payload`` holds the DJ identity, optional genre/format/location lists,
    source URLs, and the source-backed channel list. Nothing is sent,
    crawled, or invented here.
    """
    record, channels = _normalize_dj_payload(payload, now)
    record["dj_id"] = "dj_" + uuid.uuid4().hex[:24]
    repository.save_dj(record, channels)
    return get_dj(repository, record["dj_id"])


def get_dj(repository, dj_id: str) -> dict | None:
    return repository.get_dj(dj_id)


def list_djs(repository, *, limit: int = 50, offset: int = 0,
             q: str | None = None, genre: str | None = None,
             country: str | None = None, location: str | None = None,
             station: str | None = None, dj_type: str | None = None,
             platform: str | None = None, has_contact: bool | None = None,
             sort: str | None = None, order: str | None = None
             ) -> tuple[list[dict], int]:
    """Filtered DJ listing; returns (rows, total).

    ``station`` filters on the DJ's OPTIONAL station affiliation only —
    affiliation is metadata, never a discovery source. ``dj_type`` matches
    the stored role label (e.g. ``club_dj``); ``has_contact`` requires (or
    excludes) DJs with at least one stored channel.
    """
    return repository.list_djs(
        limit=limit, offset=offset, q=q, genre=genre, country=country,
        location=location, station=station, dj_type=dj_type,
        platform=platform, has_contact=has_contact, sort=sort, order=order)


def ingest_dj_discovery(repository, records: list[dict], *,
                        now: str = None) -> dict:
    """Store source-backed DJ discovery results (create / merge / dedupe).

    The full batch is deduplicated against itself by strong identity
    evidence first (``djs.dedupe.deduplicate_djs`` — same email, canonical
    website, verified social handle, or strong name+city), then each merged
    record is matched against already-stored DJs. A match merges (provenance
    unioned, earliest discovery wins), otherwise a new profile is created.
    Records that fail validation are reported, never dropped silently.

    Returns::

        {"created": n, "merged": n, "duplicates_removed": n,
         "records": [dj detail, ...], "failures": [{"entry": ..., "error": ...}]}
    """
    ts = now or utc_now_iso()
    merged_records, duplicates_removed = deduplicate_djs(list(records))
    index = _existing_fingerprints(repository)

    created = 0
    merged = 0
    stored: list[dict] = []
    failures: list[dict] = []
    for raw in merged_records:
        if not isinstance(raw, dict):
            failures.append({"entry": raw, "error": "entry must be an object"})
            continue
        try:
            record, channels = _normalize_dj_payload(raw, ts)
        except ValueError as exc:
            failures.append({"entry": raw, "error": str(exc)})
            continue
        fingerprints = set(dj_fingerprints({**record, "channels": channels}))
        match_id = _match_existing(index, fingerprints)
        if match_id is None:
            dj_id = "dj_" + uuid.uuid4().hex[:24]
            record["dj_id"] = dj_id
            repository.save_dj(record, channels)
            index[dj_id] = fingerprints
            created += 1
            stored.append(get_dj(repository, dj_id))
        else:
            existing = repository.get_dj(match_id)
            if existing is None:
                failures.append({"entry": raw, "error": "existing match vanished"})
                continue
            dj_id = _merge_to_existing(repository, existing, record, channels)
            index[dj_id] = fingerprints
            merged += 1
            stored.append(get_dj(repository, dj_id))
    return {
        "created": created,
        "merged": merged,
        "duplicates_removed": duplicates_removed,
        "records": stored,
        "failures": failures,
    }


def _existing_fingerprints(repository) -> dict[str, set[str]]:
    """Map every stored dj_id -> its identity fingerprint set."""
    rows, _total = repository.list_djs(limit=10000, offset=0)
    index: dict[str, set[str]] = {}
    for row in rows:
        channels = [dict(c) for c in repository.get_dj_channels(row["dj_id"])]
        index[row["dj_id"]] = set(
            dj_fingerprints({**row, "channels": channels}))
    return index


def _match_existing(index: dict[str, set[str]],
                    fingerprints: set[str]) -> str | None:
    """Return the stored dj_id sharing any identity fingerprint, or None."""
    for dj_id, existing in index.items():
        if existing & fingerprints:
            return dj_id
    return None


def _merge_to_existing(repository, existing: dict, record: dict,
                       channels: list[dict]) -> str:
    """Merge discovery evidence into an existing DJ profile in place."""
    base = dict(existing)
    base["channels"] = [dict(c) for c in
                        repository.get_dj_channels(existing["dj_id"])]
    merge_dj_records(base, {**record, "channels": channels})
    repository.save_dj(base, base["channels"])
    return base["dj_id"]


def delete_dj(repository, dj_id: str) -> str | None:
    """Delete one DJ profile; returns the deleted id or None.

    Only the DJ profile and its source-backed channels are removed. Any
    outreach records created for the DJ (target_type 'dj') are preserved.
    """
    return repository.delete_dj(dj_id)


def dj_outreach(repository, dj_id: str) -> list[dict]:
    """Outreach records created for one DJ, newest first.

    Records are matched by their outreach identity key (``dj:<dj_id>``);
    identity keys never collide with station keys.
    """
    key = dj_identity_key(dj_id)
    rows, _total = repository.list_outreach(limit=500, offset=0)
    matched = [r for r in rows if r.get("identity_key") == key]
    matched.sort(key=lambda r: (r.get("created_at") or "", r.get("outreach_id") or ""),
                 reverse=True)
    return matched


def dj_detail(repository, dj_id: str) -> dict | None:
    """Full DJ view (identity + channels + outreach history) or None.

    The shape is composed here so routes and tests share one source; the
    projection itself lives in backend.contracts (read-path honesty rules).
    """
    row = repository.get_dj(dj_id)
    if row is None:
        return None
    from backend.contracts import dj_detail as _dj_detail_view
    return _dj_detail_view(
        row, repository.get_dj_channels(dj_id), dj_outreach(repository, dj_id))