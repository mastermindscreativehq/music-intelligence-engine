"""Phase 3: opportunity computation orchestration.

Compute deterministic, explainable opportunities for a given release across
all active stations. Business logic lives here; nothing in this module writes
storage, sends, or auto-creates outreach records.

Caller responsibilities:
    - Fetch the track via ``repository.get_track()`` and pass it in.
    - Envelope and error mapping live in the route handler.
"""

from __future__ import annotations

import re

from backend.contracts import station_summary
from backend import outreach_intel as oi
from discovery.radio.contract import derive_location_status
from opportunity import scoring

MAX_STATIONS = 2000
MAX_OUTREACH_SCAN = 2000

ROUTE_VIEW_FIELDS = (
    "priority", "class", "type", "title", "value", "detail",
    "source_url", "evidence", "verification_state", "evidence_state",
    "confidence", "actionability",
)

_CONTACT_VIEW_FIELDS = (
    "contact_uid", "name", "role", "email", "source_url",
)

_ROUTE_KIND_FROM_TYPE = {
    "submission_url": "webform",
    "submission_page": "webform",
    "submission_email": "email",
    "contact_email": "email",
    "contact_page": "contact",
    "contact_phone": "phone",
    "named_contact": "contact",
    "directory_page": "dj",
    "info_page": "webform",
}


def _useful_pages(station):
    raw = station.get("raw_metadata") or {}
    return [p for p in (raw.get("useful_pages") or []) if isinstance(p, dict)]


def _music_contact(contacts):
    candidates = []
    for c in (contacts or []):
        role = str(c.get("role") or "").strip().lower()
        if role in oi.SUBMISSION_ROUTE_INTEREST_ROLES or \
                c.get("preferred_for_submissions"):
            candidates.append(c)
    if not candidates:
        return None

    def _key(c):
        rel = oi.contact_relevance(c)
        has_channel = 1 if (c.get("email") or c.get("phone")) else 0
        return (rel["score"], has_channel)

    return max(candidates, key=_key)


def _email_value(route):
    value = str(route.get("value") or "")
    if value.lower().startswith("mailto:"):
        return value[7:] or None
    return None


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")[:24]


def _recipient(station, route, contact):
    if route is None:
        return None
    route_type = route.get("type") or ""
    kind = (route.get("kind")
            or _ROUTE_KIND_FROM_TYPE.get(route_type, "webform"))
    email = None
    submission_url = None

    if kind == "email":
        email = _email_value(route)
        if not email and contact:
            email = contact.get("email")
        if not email:
            return None
    elif kind in ("webform", "contact", "dj"):
        value = route.get("value") or ""
        submission_url = value if value.startswith("http") else None
        if not submission_url:
            return None
    else:
        return None

    contact_uid = None
    if contact:
        contact_uid = contact.get("contact_uid") or contact.get("id")
    if not contact_uid:
        slug = _slug(
            station.get("domain") or route.get("title") or "station")
        contact_uid = "wf_" + slug

    return {
        "contact_uid": str(contact_uid),
        "identity_key": station.get("identity_key"),
        "station_name": station.get("name"),
        "name": ((contact.get("name") if contact else None)
                 or route.get("title")
                 or (station.get("name") + " submission")),
        "role": ((contact.get("role") if contact else None)
                 or route.get("class")
                 or "station"),
        "organization": station.get("name"),
        "email": email or "",
        "source_url": (route.get("source_url")
                       or (contact or {}).get("source_url")),
        "outreach_class": "email" if email else "webform",
        "submission_url": submission_url,
        "route_kind": kind,
        "route_label": (None if email
                        else (route.get("title")
                              or _ROUTE_KIND_FROM_TYPE.get(route_type, kind))),
    }


def _route_view(route):
    if route is None:
        return None
    return {k: route.get(k) for k in ROUTE_VIEW_FIELDS}


def _contact_view(contact):
    if contact is None:
        return None
    view = {k: contact.get(k) for k in _CONTACT_VIEW_FIELDS if k in contact}
    view["relevance"] = oi.contact_relevance(contact)
    return view


def _track_view(track):
    if track is None:
        return None
    return {
        "track_id": track.get("track_id"),
        "status": track.get("status"),
        "original_filename": track.get("original_filename"),
        "size_bytes": track.get("size_bytes"),
    }


# ---------------------------------------------------------------------------
# Single-station opportunity
# ---------------------------------------------------------------------------

def compute_opportunity(repository, track, station, history_by_key):
    key = station["identity_key"]
    contacts = _station_contacts(repository, station) or []
    submission = _station_submission(repository, station)
    useful_pages = _useful_pages(station)
    routes = oi.build_outreach_routes(useful_pages, submission, contacts)
    best_station_route = oi.best_outreach_route(routes)
    music_contact = _music_contact(contacts)
    contact_route = (oi.best_contact_outreach_route(music_contact, routes)
                     if music_contact else None)
    recommended_route = contact_route or best_station_route
    recipient = _recipient(station, recommended_route, music_contact)
    reachable = bool(
        recipient
        and recommended_route
        and recommended_route.get("verification_state")
        in (oi.VERIFIED, oi.ACTIONABLE))
    hist = [r for r in history_by_key.get(key, [])
            if r.get("track_id") == track.get("track_id")]
    already = bool(hist)
    contacted_at = hist[0]["created_at"] if hist else None
    location_status = derive_location_status(
        station.get("country"), station.get("state_or_region"),
        station.get("city"), station.get("market_area"),
        station.get("last_verified_at"))
    scoring_result = scoring.score_opportunity(
        track=track, station=station, contacts=contacts,
        useful_pages=useful_pages, submission=submission,
        routes=routes, best_route=best_station_route,
        music_contact=music_contact, contact_route=contact_route,
        already_contacted=already, contacted_at=contacted_at,
        history=hist)
    return {
        "station": station_summary(station),
        "score": scoring_result["score"],
        "tier": scoring_result["tier"],
        "reasoning": {
            "components": scoring_result["components"],
            "reasons": scoring_result["reasons"],
            "penalty": scoring_result["penalty"],
        },
        "recommended_route": _route_view(recommended_route),
        "recommended_contact": _contact_view(music_contact),
        "route_type": (recommended_route.get("type")
                       if recommended_route else None),
        "route_verified": bool(
            recommended_route
            and recommended_route.get("verification_state")
            in (oi.VERIFIED, oi.ACTIONABLE)),
        "recipient": recipient,
        "reachable": reachable,
        "outreach_status": "contacted" if already else "new",
        "already_contacted": already,
        "history": scoring_result["history"],
        "location_status": location_status,
    }


# ---------------------------------------------------------------------------
# Repository shims (accept PersistenceService or any duck-typed repository)
# ---------------------------------------------------------------------------

def _station_contacts(repository, station):
    if repository is not None and hasattr(repository, "get_station_contacts"):
        return repository.get_station_contacts(station["identity_key"])
    return []


def _station_submission(repository, station):
    if repository is not None and hasattr(repository, "get_submission"):
        return repository.get_submission(station["identity_key"])
    return None


# ---------------------------------------------------------------------------
# Filtering / sorting
# ---------------------------------------------------------------------------

def _passes(opp, *, tier, genre, country, station_type,
            has_contact, has_route, outreach_status):
    if tier:
        wanted = {t.lower() for t in (tier if isinstance(tier, (list, tuple, set))
                                       else [tier])}
        if opp["tier"].lower() not in wanted:
            return False
    station = opp["station"]
    if genre:
        genres = scoring._as_list(station.get("genres"))
        if genre.lower() not in genres:
            return False
    if country:
        if (station.get("country") or "").lower() != country.lower():
            return False
    if station_type:
        if (station.get("station_type") or "").lower() != station_type.lower():
            return False
    if has_contact is not None:
        if has_contact != bool(opp["recommended_contact"]):
            return False
    if has_route is not None:
        if has_route != opp["route_verified"]:
            return False
    if outreach_status:
        if opp["outreach_status"] != outreach_status:
            return False
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_opportunities(
    repository, track, *, limit=50, offset=0, tier=None, genre=None,
    country=None, station_type=None, has_contact=None, has_route=None,
    outreach_status=None, sort="score", order="desc",
) -> dict:
    """Ranked opportunities for *track* across all active stations."""
    stations, _total, _dev_excluded = repository.list_stations(
        limit=MAX_STATIONS, offset=0, exclude_dev=True)
    outreach_rows, _ = repository.list_outreach(
        limit=MAX_OUTREACH_SCAN, offset=0)
    history_by_key: dict[str, list[dict]] = {}
    for rec in outreach_rows:
        if rec.get("target_type") not in (None, "station"):
            continue   # DJ outreach never pollutes station opportunity history
        key = rec.get("identity_key") or ""
        history_by_key.setdefault(key, []).append(rec)

    results = []
    for station in stations:
        opp = compute_opportunity(repository, track, station, history_by_key)
        if _passes(
            opp, tier=tier, genre=genre, country=country,
            station_type=station_type, has_contact=has_contact,
            has_route=has_route, outreach_status=outreach_status,
        ):
            results.append(opp)

    if sort == "name":
        results.sort(
            key=lambda o: (o["station"].get("name") or "").lower(),
            reverse=(order != "asc"))
    elif sort == "relevance":
        results.sort(
            key=lambda o: (o["recommended_route"] or {}).get("priority", 99),
            reverse=(order == "desc"))
    else:
        results.sort(key=lambda o: o["score"],
                     reverse=(order != "asc"))

    total = len(results)
    page = results[offset:offset + limit]
    return {
        "track": _track_view(track),
        "opportunities": page,
        "total": total,
        "limit": limit,
        "offset": offset,
    }
