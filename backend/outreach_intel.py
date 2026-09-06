"""Outreach-intelligence derivation for a station (Phase 11 evidence model).

Everything in this module is a PURE read-path derivation: it never writes
storage, never fetches, and never invents evidence. It turns recorded
artifacts (useful pages, submission paths, contacts, fetches, facts) into a
small, honest intelligence picture:

- ``EVIDENCE STATES``  DISCOVERED / VERIFIED / ACTIONABLE / UNREACHABLE /
                       UNKNOWN — never collapsed into a boolean.
- ``OUTREACH ROUTES`` a ranked hierarchy (P1 direct music submission, P2
                       music contact, P3 general contact, P4 DJ/program
                       directory) where each entry keeps its own evidence.
- ``RECOMMENDATION``  one primary next step plus fallback and unknowns.
- ``STATION LEVEL``   RAW / VERIFIED / RESEARCHED / ACTIONABLE / LIMITED /
                       INSUFFICIENT_EVIDENCE — "enriched" is no longer a
                       state; a record is only ACTIONABLE when a verified
                       route actually exists.
"""

from __future__ import annotations

# Evidence states (verbatim vocabulary, one concept per state).
DISCOVERED = "DISCOVERED"
VERIFIED = "VERIFIED"
ACTIONABLE = "ACTIONABLE"
UNREACHABLE = "UNREACHABLE"
UNKNOWN = "UNKNOWN"

# Reserved-TLD names are development/test artifacts, not verified evidence.
# A domain or email under one of these suffixes is never treated as verified
# regardless of what a fixture claims — the pipeline cannot invent proof.
RESERVED_TLDS = (".example", ".test", ".invalid", ".localhost", ".local")


def reserved_host(host: str | None) -> bool:
    if not isinstance(host, str) or not host.strip():
        return False
    return host.strip().lower().endswith(RESERVED_TLDS)

# Outreach route classes + priorities.
P1_DIRECT_SUBMISSION = 1
P2_MUSIC_CONTACT = 2
P3_GENERAL_CONTACT = 3
P4_DIRECTORY = 4

ROUTE_CLASS = "ROUTE_CLASS"

# Contact route classification vocabulary (Fifth requirement).
CONTACT_ROUTE_CLASS_MAP = {
    "music_director": "MUSIC_DIRECTOR",
    "program_director": "PROGRAM_DIRECTOR",
    "music_programmer": "PROGRAMMING_CONTACT",
    "music_scheduler": "PROGRAMMING_CONTACT",
    "music_coordinator": "PROGRAMMING_CONTACT",
    "programming": "PROGRAMMING_CONTACT",
    "music_submission": "DIRECT_MUSIC_CONTACT",
    "host": "SHOW_HOST",
    "dj": "DJ",
    "general": "GENERAL_STATION_CONTACT",
    "unknown": "UNKNOWN_ROLE",
    "": "UNKNOWN_ROLE",
}
SUBMISSION_ROUTE_INTEREST_ROLES = frozenset((
    "music_director", "program_director", "music_submission",
    "music_programmer", "programming", "music_scheduler",
    "music_coordinator"))


def contact_route_class(role) -> str:
    return CONTACT_ROUTE_CLASS_MAP.get(str(role or "").strip(), "UNKNOWN_ROLE")


# Useful-page route semantics (Sixth requirement): why it was discovered, what
# kind of route it is, what evidence it holds, whether it is actionable now,
# and the concrete next step for the artist. The URL itself is never touched.
PAGE_ROUTE_SEMANTICS = {
    "send_music": {
        "class": "DIRECT_MUSIC_SUBMISSION",
        "type": "submission_page",
        "kind": "Submission route",
        "why": "Discovered as a station page that explicitly asks artists to "
               "send their music.",
        "actionability": "High",
        "next_step": ("Use this verified submission route and follow any "
                      "stated format/formatting requirements."),
    },
    "submission_guidelines": {
        "class": "DIRECT_MUSIC_SUBMISSION",
        "type": "submission_page",
        "kind": "Submission route",
        "why": "Discovered as the station's official music submission "
               "guidelines page.",
        "actionability": "High",
        "next_step": ("Follow the station's stated submission requirements "
                      "for the best chance of review."),
    },
    "contact": {
        "class": "GENERAL_CONTACT",
        "type": "contact_page",
        "kind": "General contact route",
        "why": "Discovered as the station's official contact page.",
        "actionability": "Medium",
        "next_step": ("Contact the station through this page; mention music "
                      "submission interest and request the right "
                      "person/department."),
    },
    "dj_directory": {
        "class": "DJ_PROGRAM_DIRECTORY",
        "type": "directory_page",
        "kind": "DJ / program directory",
        "why": "Discovered as a page that names station DJs or program "
               "personalities.",
        "actionability": "Medium",
        "next_step": ("Inspect named personalities for genre relevance and a "
                      "verified contact route before reaching out."),
    },
    "programming": {
        "class": "DJ_PROGRAM_DIRECTORY",
        "type": "directory_page",
        "kind": "Programming / schedule page",
        "why": "Discovered as the station's programming or schedule page.",
        "actionability": "Low",
        "next_step": ("Identify a relevant show or program director as a "
                      "candidate music contact."),
    },
    "about": {
        "class": "DJ_PROGRAM_DIRECTORY",
        "type": "info_page",
        "kind": "Station information",
        "why": "Discovered as official station information.",
        "actionability": "Low",
        "next_step": ("Reference only; not a direct outreach route."),
    },
    "other": {
        "class": "DJ_PROGRAM_DIRECTORY",
        "type": "info_page",
        "kind": "Discovered station page",
        "why": "Discovered as a station-level page.",
        "actionability": "Low",
        "next_step": ("Review for additional route evidence; not directly "
                      "actionable."),
    },
}


def page_evidence_state(page: dict) -> str:
    """Reachability evidence only: no guessing."""
    reachable = page.get("reachable")
    if reachable is True:
        return VERIFIED
    if reachable is False:
        return UNREACHABLE
    return DISCOVERED


def contact_evidence_state(contact: dict) -> str:
    """A published email is verified evidence; anything weaker stays honest."""
    email = contact.get("email")
    if email:
        host = str(email).rsplit("@", 1)[-1] if "@" in str(email) else ""
        return DISCOVERED if reserved_host(host) else VERIFIED
    if contact.get("phone"):
        return DISCOVERED      # observed channel, weaker route evidence
    return DISCOVERED          # named/role contact on record, no route yet


def _page_route(page: dict, priority: int, confidence: float) -> dict:
    semantics = PAGE_ROUTE_SEMANTICS.get(page.get("category"),
                                         PAGE_ROUTE_SEMANTICS["other"])
    state = page_evidence_state(page)
    actionable = page.get("category") in frozenset((
        "send_music", "submission_guidelines", "contact",
        "dj_directory", "programming"))
    verification = ACTIONABLE if (state == VERIFIED and actionable) else state
    return {
        "priority": priority,
        "class": semantics["class"],
        "type": semantics["type"],
        "title": page.get("label") or semantics["kind"],
        "value": page.get("url"),
        "source_url": page.get("source_url"),
        "source_page_title": semantics["kind"],
        "evidence": semantics["why"],
        "evidence_state": state,
        "verification_state": verification,
        "confidence": confidence,
        "why": semantics["why"],
        "next_step": semantics["next_step"],
        "actionability": semantics["actionability"],
        "category": page.get("category"),
        "observed_at": page.get("rechecked_at") or page.get("discovered_at"),
    }


def _contact_route(contact: dict, priority: int, confidence: float,
                   direct_music: bool) -> dict:
    role = str(contact.get("role") or "unknown").strip() or "unknown"
    state = contact_evidence_state(contact)
    route_class = contact_route_class(role)
    email = contact.get("email")
    phone = contact.get("phone")
    if email:
        value, route_type = f"mailto:{email}", "contact_email"
        title = contact.get("name") or (role.replace("_", " ") or "contact")
    elif phone:
        value, route_type = phone, "contact_phone"
        title = contact.get("name") or (role.replace("_", " ") or "contact")
    else:
        value, route_type = None, "named_contact"
        title = contact.get("name") or (role.replace("_", " ") or "contact")
    return {
        "priority": priority,
        "class": route_class,
        "type": route_type,
        "title": title,
        "value": value,
        "source_url": contact.get("source_url"),
        "source_page_title": "station website data",
        "evidence": (f"{title} listed as {role.replace('_', ' ')} on the "
                     "station site.") if contact.get("name") or role != \
                     "unknown" else "Observed contact on the station site.",
        "evidence_state": state,
        "verification_state": ACTIONABLE if state == VERIFIED else state,
        "confidence": confidence,
        "why": (f"{title} is a {role.replace('_', ' ')} role likely to "
                "decide on music outreach.") if direct_music else \
               ("Contact on record; role relevance to music outreach not "
                "directly stated."),
        "next_step": ("Email with a concise, formatted pitch if genre-"
                      "relevant.") if email else (
                          "Call/leave a clear message referencing music "
                          "submission.") if phone else (
                              "No verified channel; try the station contact "
                              "or social routes."),
        "actionability": "High" if email else "Low",
        "role_class": route_class,
        "contact_uid": contact.get("contact_uid") or contact.get("id"),
        "observed_at": contact.get("verified_at"),
    }


def build_outreach_routes(useful_pages: list[dict],
                          submission: dict | None,
                          contacts: list[dict]) -> list[dict]:
    """Ranked P1->P4 route list, each entry carrying its own evidence."""
    routes: list[dict] = []

    # P1 — verified direct music submission route.
    if submission and isinstance(submission.get("submission_url"), dict):
        fact = submission["submission_url"]
        value = fact.get("value")
        if value:
            state = VERIFIED if fact.get("verified") is True else DISCOVERED
            routes.append({
                "priority": P1_DIRECT_SUBMISSION,
                "class": "DIRECT_MUSIC_SUBMISSION", "type": "submission_url",
                "title": "Official music submission URL",
                "value": value, "source_url": fact.get("source_url"),
                "source_page_title": "station website data",
                "evidence": "Submission URL recorded from the station site.",
                "evidence_state": state,
                "verification_state": ACTIONABLE if state == VERIFIED
                else state,
                "confidence": 0.9,
                "why": "The station provides an official URL for music "
                       "submissions.",
                "next_step": "Use this URL to submit your music directly.",
                "actionability": "High", "category": "submission_route",
                "observed_at": fact.get("discovered_at"),
            })
    if submission and submission.get("submission_email"):
        value = submission["submission_email"]
        email_vouched = value \
            if not reserved_host(str(value).rsplit("@", 1)[-1]) else None
        routes.append({
            "priority": P1_DIRECT_SUBMISSION,
            "class": "DIRECT_MUSIC_SUBMISSION", "type": "submission_email",
            "title": "Official artist submission email",
            "value": f"mailto:{value}",
            "source_url": (submission.get("submission_url") or {}).get(
                "source_url") or "",
            "source_page_title": "station website data",
            "evidence": "Submission email recorded from the station site.",
            "evidence_state": VERIFIED if email_vouched else DISCOVERED,
            "verification_state": ACTIONABLE if email_vouched else DISCOVERED,
            "confidence": 0.85,
            "why": "The station publicly lists an address for music "
                   "submissions.",
            "next_step": "Email the submission address with a concise, "
                         "formatted pitch.",
            "actionability": "High", "category": "submission_route",
            "observed_at": (submission.get("submission_url") or {}).get(
                "discovered_at"),
        })

    # P1 — submission-classed useful pages (official, reachable).
    for page in (useful_pages or []):
        if page.get("category") in ("send_music", "submission_guidelines"):
            routes.append(_page_route(page, P1_DIRECT_SUBMISSION, 0.85))

    # P2 — verified music decision-maker contacts (email or published phone).
    for contact in (contacts or []):
        role = str(contact.get("role") or "unknown").strip() or "unknown"
        if role in SUBMISSION_ROUTE_INTEREST_ROLES and \
                (contact.get("email") or contact.get("phone")):
            routes.append(_contact_route(contact, P2_MUSIC_CONTACT, 0.72, True))

    # P3 — general contact route (contact page or general contact email).
    for page in (useful_pages or []):
        if page.get("category") == "contact":
            routes.append(_page_route(page, P3_GENERAL_CONTACT, 0.6))
    for contact in (contacts or []):
        if contact.get("role") == "general" and contact.get("email"):
            routes.append(_contact_route(contact, P3_GENERAL_CONTACT, 0.55,
                                         False))

    # P4 — DJ / program directory research routes.
    for page in (useful_pages or []):
        if page.get("category") in ("dj_directory", "programming", "about",
                                    "other"):
            if page.get("category") in ("dj_directory", "programming"):
                routes.append(_page_route(page, P4_DIRECTORY, 0.55))
            elif page.get("category") == "about":
                routes.append(_page_route(page, P4_DIRECTORY, 0.3))
            else:
                routes.append(_page_route(page, P4_DIRECTORY, 0.2))

    # Show-staff (host/dj) contacts without a route stay as named references.
    for contact in (contacts or []):
        if contact_route_class(contact.get("role")) in ("SHOW_HOST", "DJ") \
                and not (contact.get("email") or contact.get("phone")):
            routes.append(_contact_route(contact, P4_DIRECTORY, 0.35, False))

    routes.sort(key=lambda r: (r["priority"], -r["confidence"]))
    return routes


def _sorted_by_priority(routes: list[dict]) -> list[dict]:
    return sorted(routes,
                  key=lambda r: (r["priority"], -r["confidence"]))


def best_outreach_route(routes: list[dict]) -> dict | None:
    """Primary recommendation: highest-priority VERIFIED/ACTIONABLE route."""
    ranked = _sorted_by_priority(routes)
    for route in ranked:
        if route["verification_state"] == ACTIONABLE and route.get("value"):
            return route
    return None


def unknown_items(station: dict, routes: list[dict],
                  contacts: list[dict]) -> list[str]:
    """Matters the station document knows about but has no evidence for."""
    unknown: list[str] = []
    if not (station.get("city") or station.get("market_area")):
        unknown.append("station locality")
    page_classes = {r.get("class") for r in routes}
    if "DIRECT_MUSIC_SUBMISSION" not in page_classes:
        unknown.append("verified direct music submission route")
    direct_people = [c for c in (contacts or [])
                     if str(c.get("role") or "") in SUBMISSION_ROUTE_INTEREST_ROLES
                     and c.get("email")]
    if not direct_people:
        unknown.append("verified named music decision-maker")
    if not any(r.get("evidence_state") == VERIFIED for r in routes):
        unknown.append("verified contact information")
    return unknown


def primary_recommendation(station: dict, useful_pages: list[dict],
                           submission: dict | None,
                           contacts: list[dict]) -> dict:
    """One best outreach path with why/confidence/evidence/fallback/unknown."""
    routes = build_outreach_routes(useful_pages, submission, contacts)
    best = best_outreach_route(routes)
    fallback_candidates = [
        r for r in _sorted_by_priority(routes)
        if best is not None and r is not best and r.get("value")]
    fallback = fallback_candidates[0] if fallback_candidates else (
        routes[0] if routes else None)
    unknown = unknown_items(station, routes, contacts)
    if best is None:
        return {
            "route": None,
            "why": ("No verified outreach route is yet recorded for this "
                    "station."),
            "confidence": UNKNOWN,
            "evidence": [],
            "fallback": fallback,
            "unknown": unknown,
            "route_count": len(routes),
        }
    why = {
        P1_DIRECT_SUBMISSION: "The station explicitly provides a verified "
                              "route for music submissions.",
        P2_MUSIC_CONTACT: "A music decision-maker at the station is on "
                          "record with a verified contact route.",
        P3_GENERAL_CONTACT: "The station provides a verified general contact "
                            "route; ask to be pointed to the right person.",
        P4_DIRECTORY: "The station names programs/DJs; use this to find "
                      "genre-relevant people.",
    }.get(best["priority"], "Best verified route found.")
    confidence = best.get("confidence", 0.0)
    return {
        "route": best,
        "why": why,
        "confidence": ("High" if confidence >= 0.8
                       else "Medium" if confidence >= 0.5 else "Low"),
        "evidence": [best.get("evidence")],
        "fallback": fallback,
        "unknown": unknown,
        "route_count": len(routes),
    }


def station_intelligence_level(station: dict, useful_pages: list[dict],
                               submission: dict | None,
                               contacts: list[dict]) -> tuple[str, str]:
    """Honest station state. 'enriched' is no longer a level."""
    routes = build_outreach_routes(useful_pages, submission, contacts)
    has_verified_direct = any(
        r["priority"] in (P1_DIRECT_SUBMISSION, P2_MUSIC_CONTACT)
        and r["verification_state"] == ACTIONABLE and r.get("value")
        for r in routes)
    if has_verified_direct:
        return "ACTIONABLE", (
            "At least one verified direct music-submission or music-contact "
            "route exists.")
    has_verified_general = any(
        r["verification_state"] == ACTIONABLE and r.get("value")
        for r in routes)
    if has_verified_general:
        return "LIMITED", (
            "Researched, but only general contact/directory routes are "
            "verified — no direct music route yet.")
    investigated = bool(useful_pages)
    if investigated:
        return "INSUFFICIENT_EVIDENCE", (
            "Researched, but no verified outreach route surfaced yet.")
    if station.get("domain") and not reserved_host(station.get("domain")):
        return "VERIFIED", "Station identity recorded; pages not yet inspected."
    return "RAW", "Basic station record; identity not yet verified."