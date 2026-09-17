"""Deterministic deduplication for independent DJ records (DJ pipeline).

A DJ is an independent music professional. Discovery may meet the same DJ
on several public sources, so records are merged on STRONG identity
evidence only — never on name alone:

1. normalized email / submission_email channel value;
2. canonical website (channel ``website`` or a source URL's registrable
   domain);
3. verified social handle (instagram/x/facebook/youtube channel values,
   normalized to host+path or a plain handle);
4. strong name + city combination (name slug + city slug).

Merging never destroys provenance: source_urls and channels are unioned
(order-preserving, first sighting's ``source_url`` wins per value),
date/discovery timestamps are earliest-in / latest-out, and nothing is ever
fabricated here — absent evidence simply stays absent.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from crawler.urls import canonical_domain, slugify_name

_EMAIL_CHANNELS = frozenset(("email", "submission_email"))
_SOCIAL_CHANNELS = frozenset(("instagram", "x", "facebook", "youtube"))

__all__ = [
    "dj_fingerprints",
    "deduplicate_djs",
    "merge_dj_records",
    "records_share_identity",
]


def _clean(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_email(value: str) -> str:
    """Lower-cased, whitespace-free email used for identity matching."""
    return re.sub(r"\s+", "", str(value or "").lower().strip())


def _normalize_social(channel: str, value: str) -> str:
    """Canonical handle token for a social channel value.

    ``https://www.instagram.com/foo`` and ``@foo`` both normalize so the
    same person seen twice merges; ``https://tiktok.com/...`` never matches
    an x handle.
    """
    raw = str(value or "").strip().lower().rstrip("/")
    if not raw:
        return ""
    if raw.startswith(("http://", "https://")):
        try:
            parts = urlsplit(raw)
        except ValueError:
            return raw
        host = (parts.netloc or "").lower()
        if host.startswith("www."):
            host = host[len("www."):]
        path = parts.path.strip("/").split("/")[0] if parts.path else ""
        return f"{host}/{path}" if path else host
    token = raw.lstrip("@#").strip()
    return f"@{token}" if token else ""


def dj_fingerprints(record: dict) -> dict[str, str]:
    """Identity fingerprint keys for one DJ record + its channels.

    Returns a mapping of fingerprint-type -> canonical value. Two records
    are the same DJ when ANY fingerprint key is shared.
    """
    keys: dict[str, str] = {}
    seen: set[str] = set()

    def _add(kind: str, value: str) -> None:
        if not value:
            return
        key = kind + ":" + value
        if key in seen:
            return
        seen.add(key)
        keys[key] = value

    for url in record.get("source_urls") or []:
        if not isinstance(url, str):
            continue
        try:
            _add("website", canonical_domain(url))
        except (ValueError, TypeError):
            continue

    for channel in record.get("channels") or []:
        if not isinstance(channel, dict):
            continue
        name = str(channel.get("channel") or "").strip()
        value = str(channel.get("value") or "")
        if not value:
            continue
        if name in _EMAIL_CHANNELS:
            normalized = normalize_email(value)
            if normalized and "email:" + normalized not in seen:
                _add("email", normalized)
        elif name in _SOCIAL_CHANNELS:
            handle = _normalize_social(name, value)
            if handle:
                _add(f"social:{name}", handle)
        elif name == "website":
            try:
                _add("website", canonical_domain(value))
            except (ValueError, TypeError):
                continue

    name = _clean(record.get("name"))
    city = _clean(record.get("city"))
    if name and city:
        _add("namegeo",
             f"{slugify_name(name)}-{slugify_name(city)}")
    return keys


def _fingerprint_set(record: dict) -> set[str]:
    return set(dj_fingerprints(record))


def records_share_identity(a: dict, b: dict) -> bool:
    """True when the two DJ records share any strong identity evidence."""
    return bool(_fingerprint_set(a) & _fingerprint_set(b))


def _unique_extend(target: list, items) -> None:
    for item in items or []:
        if item not in target:
            target.append(item)


def _min_ts(*values: str | None) -> str | None:
    present = [v for v in values if v]
    return min(present) if present else None


def _max_ts(*values: str | None) -> str | None:
    present = [v for v in values if v]
    return max(present) if present else None


def merge_dj_records(primary: dict, duplicate: dict) -> dict:
    """Merge *duplicate* into *primary* in place; returns *primary*.

    Provenance is never destroyed: source_urls and channels are unioned,
    the first-observed location/role facts win where the primary lacks
    them, and timestamps are earliest-in / latest-out.
    """
    for field in ("name", "stage_name", "role", "program", "platform"):
        if not primary.get(field) and duplicate.get(field):
            primary[field] = duplicate[field]
    for field in ("country", "state_or_region", "city", "station_key",
                  "station_name"):
        if not primary.get(field) and duplicate.get(field):
            primary[field] = duplicate[field]

    _unique_extend(primary.setdefault("source_urls", []),
                   duplicate.get("source_urls") or [])
    _unique_extend(primary.setdefault("genres", []),
                   duplicate.get("genres") or [])
    _unique_extend(primary.setdefault("formats", []),
                   duplicate.get("formats") or [])

    primary["channels"] = _union_channels(
        primary.get("channels") or [], duplicate.get("channels") or [])

    verification = dict(primary.get("verification") or {})
    for key, value in (duplicate.get("verification") or {}).items():
        if key not in verification:
            verification[key] = value
    if verification:
        primary["verification"] = verification

    primary["discovered_at"] = _min_ts(primary.get("discovered_at"),
                                       duplicate.get("discovered_at"))
    primary["last_observed_at"] = _max_ts(primary.get("last_observed_at"),
                                          duplicate.get("last_observed_at"))
    return primary


def _union_channels(a: list[dict], b: list[dict]) -> list[dict]:
    """Union channel facts; first sighting's value/source_url wins.

    Email addresses are case-insensitive, so email and submission_email
    values are matched on their normalized form and a reappearing address
    (any casing) does not create a duplicate channel row.
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for entry in (list(a) + list(b)):
        if not isinstance(entry, dict):
            continue
        channel = str(entry.get("channel") or "")
        value = str(entry.get("value") or "")
        key_value = normalize_email(value) if channel in _EMAIL_CHANNELS \
            else value
        key = (channel, key_value)
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(entry))
    return out


def deduplicate_djs(records: list[dict]) -> tuple[list[dict], int]:
    """Merge records sharing any strong identity fingerprint.

    Returns (merged_records, duplicates_removed). First-appearance order is
    preserved and the first record of a group becomes the merge target.
    """
    merged: list[dict] = []
    duplicate_count = 0
    for record in records:
        match = next(
            (existing for existing in merged
             if records_share_identity(existing, record)),
            None)
        if match is None:
            merged.append(dict(record))
        else:
            merge_dj_records(match, record)
            duplicate_count += 1
    return merged, duplicate_count