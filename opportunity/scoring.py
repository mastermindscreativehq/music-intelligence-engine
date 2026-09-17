"""Phase 3: deterministic, explainable opportunity scoring.

Pure read-path derivation — no storage, no I/O, no AI. Given one release
(a track record) and one station's recorded intelligence, produce:

- a 0..100 weighted score,
- a HIGH / MEDIUM / LOW tier,
- a per-component breakdown and human-readable +/- reasons.

Every component is computed from values already on record. Missing evidence
lowers a score but is always named in the explanation — nothing is invented.

Weights (sum = 1.0) reflect how much each signal moves the total:

    route      0.22   verified submission / contact route
    contact    0.17   reachable music decision-maker
    genre      0.13   release<->station genre overlap
    record     0.13   station record completeness
    identity   0.09   verified station identity
    format     0.08   station programs music
    location   0.06   honest location evidence
    release    0.12   release ready for outreach

Tiers: HIGH >= 70, MEDIUM >= 40, LOW < 40. An existing outreach record for
the same release at the same station applies a bounded penalty (see
``ALREADY_CONTACTED_PENALTY``) — it never deletes or overwrites history.
"""

from __future__ import annotations

import re

from backend import outreach_intel as oi

TIER_HIGH = "HIGH"
TIER_MEDIUM = "MEDIUM"
TIER_LOW = "LOW"

HIGH_THRESHOLD = 70
MEDIUM_THRESHOLD = 40

# Existing outreach for the same release/station caps its opportunity value.
ALREADY_CONTACTED_PENALTY = 12

WEIGHTS = {
    "route": 0.22,
    "contact": 0.17,
    "genre": 0.13,
    "record": 0.13,
    "identity": 0.09,
    "format": 0.08,
    "location": 0.06,
    "release": 0.12,
}

COMPONENT_LABELS = {
    "route": "Submission route",
    "contact": "Music contact",
    "genre": "Genre relevance",
    "record": "Station record",
    "identity": "Station identity",
    "format": "Music format",
    "location": "Location evidence",
    "release": "Release readiness",
}

_ROUTE_PRIORITY_SCORE = {
    oi.P1_DIRECT_SUBMISSION: 1.0,
    oi.P2_MUSIC_CONTACT: 0.75,
    oi.P3_GENERAL_CONTACT: 0.5,
    oi.P4_DIRECTORY: 0.3,
}

_CONTACT_LEVEL_SCORE = {1: 1.0, 2: 0.85, 3: 0.6, 4: 0.45, 5: 0.4}


def _clamp(value, lo=0.0, hi=1.0):
    return max(lo, min(hi, value))


def _fmt(items):
    return ", ".join(str(i) for i in items)


def _as_list(value):
    """Normalize a genre/format field (list or delimited string) to a lower-
    cased, de-duplicated list preserving order."""
    if isinstance(value, str):
        value = re.split(r"[,;/]", value)
    if not isinstance(value, (list, tuple, set)):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip().lower()
        if text and text not in out:
            out.append(text)
    return out


def _component(key: str, score: float, note: str) -> dict:
    return {
        "key": key,
        "label": COMPONENT_LABELS[key],
        "score": round(_clamp(score), 4),
        "weight": WEIGHTS[key],
        "note": note,
    }


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------

def route_component(routes, best_route) -> dict:
    if best_route is None:
        if any(r.get("value") for r in (routes or [])):
            return _component(
                "route", 0.25,
                "A route was discovered but is not verified yet.")
        return _component(
            "route", 0.1, "No outreach route recorded for this station.")
    base = _ROUTE_PRIORITY_SCORE.get(best_route.get("priority"), 0.3)
    route_type = best_route.get("type")
    if route_type in ("submission_url", "submission_email"):
        base = 1.0
    elif route_type == "submission_page":
        base = 0.9
    label = best_route.get("title") or "route"
    return _component(
        "route", base,
        f"Verified route on record: {label} ({route_type}).")


def contact_component(contact, contact_route) -> dict:
    if contact is None:
        return _component(
            "contact", 0.1, "No music decision-maker contact on record.")
    relevance = oi.contact_relevance(contact)
    name = contact.get("name") or contact.get("role") or "music contact"
    if contact_route is None:
        return _component(
            "contact", 0.35,
            f"{name} is a {relevance['label'].lower()}-relevance music "
            "contact but has no verified channel yet.")
    level = contact_route.get("level", 6)
    level_score = _CONTACT_LEVEL_SCORE.get(level, 0.35)
    return _component(
        "contact", level_score,
        f"{name} ({relevance['label']} relevance) reachable via "
        f"{contact_route.get('label')}.")


def genre_component(release_genres, station_genres) -> dict:
    release = _as_list(release_genres)
    station = _as_list(station_genres)
    if not release:
        return _component(
            "genre", 0.4,
            "Release has no genre metadata on record; genre not scored.")
    if not station:
        return _component(
            "genre", 0.5,
            "Station genre not on record; cannot confirm a genre match.")
    shared = sorted(set(release) & set(station))
    if not shared:
        return _component(
            "genre", 0.0,
            f"No genre overlap between the release ({_fmt(release)}) and the "
            f"station ({_fmt(station)}).")
    ratio = len(shared) / len(set(release) | set(station))
    return _component("genre", _clamp(0.5 + ratio),
                      f"Genre match on {_fmt(shared)}.")


def format_component(formats) -> dict:
    fmt = _as_list(formats)
    if "music" in fmt:
        return _component("format", 1.0, "Station programs music.")
    if fmt:
        return _component(
            "format", 0.3,
            f"Station formats on record ({_fmt(fmt)}) do not explicitly "
            "include music.")
    return _component("format", 0.5, "Station format not on record.")


_RECORD_MUSIC_CLASSES = frozenset((
    "MUSIC_DIRECTOR", "PROGRAM_DIRECTOR", "DIRECT_MUSIC_CONTACT",
    "PROGRAMMING_CONTACT"))


def record_component(station, contacts, useful_pages, submission) -> dict:
    checks = (
        ("domain", bool(station.get("domain"))),
        ("website", bool(station.get("website"))),
        ("location", bool(station.get("city") or station.get("market_area"))),
        ("description", bool(station.get("description"))),
        ("genres", bool(_as_list(station.get("genres")))),
        ("formats", bool(_as_list(station.get("formats")))),
        ("social_urls", bool(station.get("social_urls"))),
        ("classification",
         ((station.get("classification_confidence")
           or station.get("confidence_score") or 0) >= 0.7)),
        ("music_contacts", any(
            oi.contact_route_class(c.get("role")) in _RECORD_MUSIC_CLASSES
            for c in (contacts or []))),
        ("useful_pages", bool(useful_pages)),
        ("submission", submission is not None),
    )
    have = sum(1 for _, ok in checks if ok)
    missing = [name for name, ok in checks if not ok]
    if missing:
        note = (f"{have}/{len(checks)} station facts on record "
                f"(missing: {_fmt(missing)}).")
    else:
        note = f"{have}/{len(checks)} station facts on record."
    return _component("record", have / len(checks), note)


def identity_component(station) -> dict:
    domain = station.get("domain")
    if not domain:
        return _component("identity", 0.4, "Station domain not recorded.")
    if oi.reserved_host(domain):
        return _component(
            "identity", 0.6,
            f"Station domain {domain} is an unverified placeholder.")
    status = str(station.get("status") or "").lower()
    if status in ("enriched", "verified", "verifying"):
        return _component(
            "identity", 1.0, f"Station identity verified at {domain}.")
    return _component(
        "identity", 0.8, f"Station domain recorded at {domain}.")


def location_component(station) -> dict:
    status = station.get("location_status")
    if status is None:
        status = ("available"
                  if (station.get("city") or station.get("market_area"))
                  else "unavailable")
    if status in ("available", "unverified"):
        place = ", ".join(
            str(x) for x in (station.get("city"),
                             station.get("state_or_region"),
                             station.get("country")) if x)
        if status == "available":
            return _component("location", 0.9,
                              f"Station location verified ({place}).")
        return _component("location", 0.7,
                          f"Station location on record ({place}) but not "
                          "independently verified.")
    return _component(
        "location", 0.5,
        "Station location not yet on record; not used to invent a match.")


def release_component(track) -> dict:
    status = str((track or {}).get("status") or "").lower()
    if status == "ready":
        return _component("release", 1.0, "Release is ready for outreach.")
    if status:
        return _component(
            "release", 0.3, f"Release status is {status!r}, not ready.")
    return _component("release", 0.3, "Release status not on record.")


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

def build_components(*, track, station, contacts, useful_pages, submission,
                     routes, best_route, music_contact,
                     contact_route) -> list[dict]:
    release_genres = None
    if isinstance(track, dict):
        release_genres = track.get("genres") or track.get("genre")
    return [
        route_component(routes, best_route),
        contact_component(music_contact, contact_route),
        genre_component(release_genres, station.get("genres")),
        record_component(station, contacts, useful_pages, submission),
        identity_component(station),
        format_component(station.get("formats")),
        location_component(station),
        release_component(track),
    ]


def tier_for(score: int) -> str:
    if score >= HIGH_THRESHOLD:
        return TIER_HIGH
    if score >= MEDIUM_THRESHOLD:
        return TIER_MEDIUM
    return TIER_LOW


def _reasons(components, already_contacted, contacted_at) -> list[dict]:
    reasons: list[dict] = []
    for component in sorted(components, key=lambda c: -c["weight"]):
        if component["score"] >= 0.75:
            sign = "+"
        elif component["score"] <= 0.35:
            sign = "-"
        else:
            sign = "i"
        reasons.append({"sign": sign, "text": component["note"]})
    if already_contacted:
        when = f" on {contacted_at}" if contacted_at else ""
        reasons.insert(0, {
            "sign": "!",
            "text": (f"Already contacted for this release at this station"
                     f"{when}; review outreach history before sending "
                     "again."),
        })
    return reasons


def score_opportunity(*, track, station, contacts, useful_pages, submission,
                      routes, best_route, music_contact=None,
                      contact_route=None, already_contacted=False,
                      contacted_at=None, history=None) -> dict:
    """Combine all components into one explainable opportunity score."""
    components = build_components(
        track=track, station=station, contacts=contacts,
        useful_pages=useful_pages, submission=submission, routes=routes,
        best_route=best_route, music_contact=music_contact,
        contact_route=contact_route)
    weighted = sum(c["score"] * c["weight"] for c in components)
    score = int(round(_clamp(weighted) * 100))
    penalty = 0
    if already_contacted:
        penalty = min(ALREADY_CONTACTED_PENALTY, score)
        score -= penalty
    return {
        "score": score,
        "tier": tier_for(score),
        "components": components,
        "reasons": _reasons(components, already_contacted, contacted_at),
        "penalty": penalty,
        "already_contacted": bool(already_contacted),
        "contacted_at": contacted_at,
        "history": list(history or []),
    }
