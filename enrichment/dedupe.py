"""Deterministic station deduplication.

Primary key: canonical registrable domain (crawler.urls.canonical_domain).
Fallback key (only when no usable website exists): slugified name + country
+ region. Name-only merging is deliberately forbidden — unrelated stations
in different cities frequently share names.

Merging never destroys data: source URLs, emails, contacts, and socials are
unioned; conflicting names become alternates; provenance is preserved.
Fuzzy matching is deferred by design.
"""

from __future__ import annotations

from crawler.urls import canonical_domain, slugify_name
from discovery.models import fact_observe_again


def identity_key(record: dict) -> tuple[str, str]:
    """Stable identity tuple for a normalized station record dict."""
    website = record.get("website") or ""
    if website:
        try:
            return ("domain", canonical_domain(website))
        except ValueError:
            pass
    name = str(record.get("name") or "").strip().lower()
    country = str(record.get("country") or "").strip().lower()
    region = str(record.get("state_or_region") or "").strip().lower()
    geo_slug = "-".join(
        part for part in (
            slugify_name(name),
            slugify_name(country),
            slugify_name(region),
        ) if part
    )
    return ("namegeo", geo_slug)


def merge_stations(primary: dict, duplicate: dict) -> dict:
    """Merge *duplicate* into *primary*, preserving all provenance."""
    def unique_extend(target: list, items) -> None:
        for item in items or []:
            if item not in target:
                target.append(item)

    # Names: keep primary, retain alternates (including the duplicate's own
    # alternates — merging never discards observed identity variants).
    alternates = list(primary.get("alternate_names") or [])
    for candidate_name in ([duplicate.get("name")] +
                           list(duplicate.get("alternate_names") or [])):
        if candidate_name and candidate_name != primary.get("name") \
                and candidate_name not in alternates:
            alternates.append(candidate_name)
    primary["alternate_names"] = alternates

    unique_extend(primary.get("source_urls") or [], duplicate.get("source_urls"))
    primary["source_urls"] = sorted(set(primary["source_urls"]),
                                    key=primary["source_urls"].index)

    # Emails / phones: Fact lists keyed by value; later sightings recorded.
    for fact_field in ("emails", "phone_numbers"):
        existing = {f["value"]: f for f in primary.get(fact_field) or []}
        for incoming in duplicate.get(fact_field) or []:
            current = existing.get(incoming["value"])
            if current is None:
                existing[incoming["value"]] = incoming
            else:
                fact_observe_again(current, incoming["source_url"])
                if not current.get("discovered_at") and incoming.get("discovered_at"):
                    current["discovered_at"] = incoming["discovered_at"]
        primary[fact_field] = list(existing.values())

    # Contacts: match on email when both have one, else keep separate.
    contacts_by_email = {
        c["email"]: c for c in primary.get("contacts") or [] if c.get("email")
    }
    for contact in duplicate.get("contacts") or []:
        email = contact.get("email")
        match = contacts_by_email.get(email) if email else None
        if match is None:
            primary.setdefault("contacts", []).append(contact)
        else:
            for prov in contact.get("provenance") or []:
                match.setdefault("provenance", []).append(prov)

    # Socials: first observation wins per platform.
    socials = dict(primary.get("social_urls") or {})
    for platform, url in (duplicate.get("social_urls") or {}).items():
        socials.setdefault(platform, url)
    primary["social_urls"] = socials

    # Page URLs: prefer the earliest non-null.
    for field_name in ("submission_url", "contact_url", "programming_url"):
        if primary.get(field_name) is None and duplicate.get(field_name) is not None:
            primary[field_name] = duplicate[field_name]

    # Location fill-in from duplicate when primary lacks it.
    for field_name in ("city", "state_or_region", "country",
                       "legal_name", "description", "language", "format"):
        if not primary.get(field_name) and duplicate.get(field_name):
            primary[field_name] = duplicate[field_name]

    # Classification: adopt stronger evidence if primary was unknown.
    if primary.get("station_type") in (None, "", "unknown") \
            and duplicate.get("station_type"):
        primary["station_type"] = duplicate["station_type"]
        primary["classification_confidence"] = duplicate.get(
            "classification_confidence")
        primary["classification_evidence"] = duplicate.get(
            "classification_evidence")

    # Timestamps: earliest discovery, latest observation.
    disc_a = primary.get("discovered_at") or ""
    disc_b = duplicate.get("discovered_at") or ""
    if disc_b and (not disc_a or disc_b < disc_a):
        primary["discovered_at"] = disc_b
    obs_a = primary.get("last_observed_at") or ""
    obs_b = duplicate.get("last_observed_at") or ""
    if obs_b > obs_a:
        primary["last_observed_at"] = obs_b

    return primary


def deduplicate_stations(records: list[dict]) -> tuple[list[dict], int]:
    """Merge records sharing an identity key.

    Returns (merged_records, duplicates_removed). Order of first appearance
    is preserved.
    """
    by_key: dict[tuple[str, str], dict] = {}
    ordered_keys: list[tuple[str, str]] = []
    duplicates = 0
    for record in records:
        key = identity_key(record)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = record
            ordered_keys.append(key)
        else:
            merge_stations(existing, record)
            duplicates += 1
    return [by_key[key] for key in ordered_keys], duplicates
