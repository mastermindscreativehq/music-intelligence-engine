"""Phase 2 canonical station contract: universal location + honest status.

Additive layer over the existing station model — the stored station row is
already the canonical identity / location / profile store. This module
provides the normalized, evidence-consistent location object and its honest
status so every surface (list, detail, intelligence) presents location the
same way.

Location is ONLY assembled from values that exist on the record: city,
state_or_region, country, then market_area as a locality fallback. Nothing is
fabricated; when no value exists the status is ``unavailable`` and the
location block carries only its status.
"""

from __future__ import annotations

LOCATION_AVAILABLE = "available"
LOCATION_UNVERIFIED = "unverified"
LOCATION_UNAVAILABLE = "unavailable"

# Order matters for display: most specific to coarsest.
_LOCATION_FIELDS = ("city", "state_or_region", "country", "market_area")


def derive_location_status(
    country: str | None = None,
    state_or_region: str | None = None,
    city: str | None = None,
    market_area: str | None = None,
    verified_at: str | None = None,
) -> str:
    """Honest location status derived ONLY from recorded evidence.

    - ``unavailable`` when no location value is recorded (missing, not
      guessed);
    - ``unverified`` when values exist but were not independently verified;
    - ``available`` when values exist and carry a verified timestamp.
    """
    if not any(value for value in (country, state_or_region, city,
                                   market_area)):
        return LOCATION_UNAVAILABLE
    if verified_at:
        return LOCATION_AVAILABLE
    return LOCATION_UNVERIFIED


def canonical_location(station: dict) -> dict:
    """Normalized location block for a stored station record.

    Only present fields are included. ``status`` is always present so callers
    can distinguish "recorded but unverified" from "nothing on record".
    """
    if not isinstance(station, dict):
        return {"status": LOCATION_UNAVAILABLE}
    location = {
        field: station.get(field)
        for field in _LOCATION_FIELDS
        if isinstance(station.get(field), str) and station[field].strip()
    }
    location["status"] = derive_location_status(
        station.get("country"),
        station.get("state_or_region"),
        station.get("city"),
        station.get("market_area"),
        station.get("last_verified_at"),
    )
    return location


def location_evidence(station: dict) -> list[dict]:
    """Evidence-backed location provenance recorded for a station.

    Each entry is a fact-like map ({value, field, source_url, source_type,
    method}) anchored to the source (seed entry, discovery request, or an
    official station page). Empty when no location was recorded.
    """
    if not isinstance(station, dict):
        return []
    raw_metadata = station.get("raw_metadata") or {}
    evidence = raw_metadata.get("location_evidence")
    return [dict(entry) for entry in evidence] \
        if isinstance(evidence, list) else []