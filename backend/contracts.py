"""API response contracts (Phase 4).

Explicit, stable response shapes for the radio-intelligence API. The
contract's job is to expose stored intelligence honestly:

- FACTS stay fact-shaped: values that carry provenance in storage keep it
  in responses (email/phone Fact dicts, submission URL/instructions facts).
- INFERENCES stay labeled: the submission methods bundle keeps
  ``"kind": "inference"`` and its reasons; nothing is re-labeled.
- UNKNOWN stays unknown: absent evidence is null, and each intelligence
  response includes an explicit ``epistemology`` section listing which
  documented fields are currently unknown.

No internal implementation details (SQL, file paths, env config) and no
credentials are ever included.
"""

from __future__ import annotations

from backend import outreach_intel
from discovery.radio.contract import derive_location_status

from djs.service import DJ_CHANNEL_ROUTE_CLASS, DJ_CHANNEL_ROUTE_LABEL

# Fields documented as meaningful-but-maybe-absent on a station. When the
# stored value is None they are reported in epistemology.unknown_fields —
# except country/state_or_region once locality (city/market) is established.
STATION_OPTIONAL_FIELDS = (
    "website", "domain", "country", "state_or_region", "city",
    "market_area", "language", "description", "last_verified_at",
)
CONTACT_OPTIONAL_FIELDS = ("name", "email", "phone", "source_url",
                           "verified_at")

STATION_SUMMARY_FIELDS = (
    "identity_key", "identity_kind", "name", "organization_type", "website",
    "domain", "country", "state_or_region", "city", "market_area",
    "station_type", "confidence_score", "status", "genres", "formats",
    "discovered_at", "last_observed_at",
)


def station_summary(row: dict) -> dict:
    """Compact projection for list responses."""
    summary = {key: row.get(key) for key in STATION_SUMMARY_FIELDS}
    summary["location_status"] = derive_location_status(
        row.get("country"), row.get("state_or_region"),
        row.get("city"), row.get("market_area"),
        row.get("last_verified_at"))
    summary["links"] = {
        "self": f"/api/v1/stations/{row['identity_key']}",
        "intelligence": f"/api/v1/stations/{row['identity_key']}/intelligence",
        "contacts": f"/api/v1/stations/{row['identity_key']}/contacts",
    }
    return summary


def station_detail(row: dict) -> dict:
    """Full stored station fields (JSON columns already decoded)."""
    detail = dict(row)   # every stored column is part of the contract
    detail["location_status"] = derive_location_status(
        row.get("country"), row.get("state_or_region"),
        row.get("city"), row.get("market_area"),
        row.get("last_verified_at"))
    detail["links"] = {
        "self": f"/api/v1/stations/{row['identity_key']}",
        "intelligence": f"/api/v1/stations/{row['identity_key']}/intelligence",
        "contacts": f"/api/v1/stations/{row['identity_key']}/contacts",
    }
    return detail


# -- Phase 4: DJ projections --------------------------------------------------
#
# Read-path derivation only: storage rows and channel facts are echoed
# verbatim; route labels are derived from the stored channel name so nothing
# is ever guessed. Location is reported honestly (`location_status`) and
# stays "unavailable" when no source-backed location was provided.

DJ_SUMMARY_FIELDS = (
    "dj_id", "name", "stage_name", "role", "program", "station_key",
    "station_name", "platform", "country", "state_or_region", "city",
    "genres", "formats", "discovered_at", "last_observed_at",
)

_SOCIAL_CHANNELS = frozenset(("instagram", "x", "facebook", "youtube"))
_EMAIL_CHANNELS = frozenset(("email", "submission_email"))
_WEBFORM_CHANNELS = frozenset(("submission_page", "contact_page"))


def dj_channel_view(channel: dict) -> dict:
    kind = "email" if channel["channel"] in _EMAIL_CHANNELS \
        else "webform" if channel["channel"] in _WEBFORM_CHANNELS \
        else "social" if channel["channel"] in _SOCIAL_CHANNELS \
        else "website"
    return {
        "channel": channel["channel"],
        "value": channel["value"],
        "source_url": channel["source_url"],
        "verified_at": channel.get("verified_at"),
        "kind": kind,
        "route_class": DJ_CHANNEL_ROUTE_CLASS.get(
            channel["channel"], "other"),
        "route_label": DJ_CHANNEL_ROUTE_LABEL.get(
            channel["channel"], channel["channel"]),
    }


def dj_summary(row: dict) -> dict:
    """Compact projection for list responses."""
    summary = {key: row.get(key) for key in DJ_SUMMARY_FIELDS}
    summary["location_status"] = derive_location_status(
        row.get("country"), row.get("state_or_region"),
        row.get("city"), None, None)
    summary["has_contact"] = bool(row.get("has_contact"))
    summary["links"] = {"self": f"/api/v1/djs/{row['dj_id']}"}
    return summary


def dj_detail(row: dict, channels: list[dict] | None = None,
              outreach: list[dict] | None = None) -> dict:
    """Full DJ view: identity, source-backed channels, outreach history."""
    detail = dict(row)   # every stored column is part of the contract
    detail["location_status"] = derive_location_status(
        row.get("country"), row.get("state_or_region"),
        row.get("city"), None, None)
    channel_views = [dj_channel_view(c) for c in (channels or [])]
    detail["channels"] = channel_views
    detail["submission_routes"] = [
        c for c in channel_views
        if c["channel"] in ("submission_email", "submission_page",
                            "contact_page")]
    detail["has_email"] = any(
        c["channel"] in _EMAIL_CHANNELS for c in channel_views)
    detail["has_submission_route"] = bool(detail["submission_routes"])
    detail["social_channels"] = [
        c for c in channel_views if c["kind"] == "social"]
    detail["outreach"] = outreach or []
    detail["links"] = {"self": f"/api/v1/djs/{row['dj_id']}"}
    return detail


def _unknown_fields(station: dict, emails: list[dict],
                    contacts: list[dict],
                    submission: dict | None) -> list[str]:
    unknown = []
    # Locality evidence (city/market) establishes geography; a merely
    # coarser-grained null (country/region) is then not "unknown".
    locality_known = bool(station.get("city") or station.get("market_area"))
    for field in STATION_OPTIONAL_FIELDS:
        if station.get(field) is not None:
            continue
        if field in ("country", "state_or_region") and locality_known:
            continue
        unknown.append(field)
    if not station.get("station_type") or \
            station.get("station_type") == "unknown":
        unknown.append("station_type")
    if not emails:
        unknown.append("emails")
    if not contacts:
        unknown.append("contacts")
    if submission is None:
        unknown.append("submission")
    elif submission.get("submission_email") is None:
        unknown.append("submission.submission_email")
    return sorted(unknown)


def _fact_count(station: dict, emails: list[dict], phones: list[dict],
                contacts: list[dict]) -> int:
    count = len(emails) + len(phones) + len(contacts)
    if station.get("source_urls"):
        count += 1   # source-url set is provenance-backed
    return count


def _raw_useful_pages(station: dict) -> list[dict]:
    return [
        p for p in (station.get("raw_metadata") or {}).get("useful_pages") or []
        if isinstance(p, dict)
    ]


def intelligence_payload(station: dict, emails: list[dict],
                         phones: list[dict], contacts: list[dict],
                         submission: dict | None,
                         fetches: list[dict] | None = None) -> dict:
    """Full intelligence view with an explicit FACT/INFERENCE/UNKNOWN map."""
    inferred: list[str] = []
    if submission and isinstance(submission.get("methods"), dict) \
            and submission["methods"].get("methods"):
        inferred.append("submission.methods")
    useful_pages = [_useful_page_view(p) for p in _raw_useful_pages(station)]
    submission_status, submission_status_reason, best_action = \
        submission_status_and_best_action(
            useful_pages, submission, contacts,
            investigated=bool(fetches or useful_pages))
    level, level_reason = outreach_intel.station_intelligence_level(
        station, _raw_useful_pages(station), submission, contacts)
    recommendation = outreach_intel.primary_recommendation(
        station, _raw_useful_pages(station), submission, contacts)
    routes = outreach_intel.build_outreach_routes(
        _raw_useful_pages(station), submission, contacts)
    contact_views = [dict(c) for c in contacts]
    _annotate_contact_routes(contact_views, routes)
    payload = {
        "station": station_detail(station),
        "intelligence_level": level,
        "intelligence_level_reason": level_reason,
        "emails": [dict(fact) for fact in emails],      # Fact dicts verbatim
        "phone_numbers": [dict(fact) for fact in phones],
        "contacts": contact_views,
        "submission": dict(submission) if submission else None,
        "submission_status": submission_status,
        "submission_status_reason": submission_status_reason,
        "best_action": best_action,
        "outreach_routes": routes,
        "outreach_recommendation": recommendation,
        "fetches": [dict(f) for f in (fetches or [])],
        "useful_pages": useful_pages,
        "epistemology": {
            "facts_count": _fact_count(station, emails, phones, contacts),
            "inferred_fields": inferred,
            "unknown_fields": _unknown_fields(station, emails, contacts,
                                              submission),
            "notes": [
                "Facts carry source/method/timestamp provenance.",
                "Inferences keep their 'kind': 'inference' label and "
                "reasons; storage never promotes them to facts.",
                "Unknown means no evidence was observed — never a guess.",
            ],
        },
    }
    return payload


def contacts_payload(station: dict, contacts: list[dict],
                     submission: dict | None) -> dict:
    raw_pages = _raw_useful_pages(station)
    useful_pages = [_useful_page_view(p) for p in raw_pages]
    submission_status, submission_status_reason, best_action = \
        submission_status_and_best_action(
            useful_pages, submission, contacts, investigated=bool(useful_pages))
    level, level_reason = outreach_intel.station_intelligence_level(
        station, raw_pages, submission, contacts)
    recommendation = outreach_intel.primary_recommendation(
        station, raw_pages, submission, contacts)
    routes = outreach_intel.build_outreach_routes(
        raw_pages, submission, contacts)
    contact_views = _contact_views(contacts)
    _annotate_contact_routes(contact_views, routes)
    payload = {
        "station_identity_key": station["identity_key"],
        "station_name": station["name"],
        "intelligence_level": level,
        "intelligence_level_reason": level_reason,
        "contacts": contact_views,
        "submission": dict(submission) if submission else None,
        "submission_status": submission_status,
        "submission_status_reason": submission_status_reason,
        "best_action": best_action,
        "outreach_routes": routes,
        "outreach_recommendation": recommendation,
        "preferred_submission_contacts": [
            {"contact_uid": c["contact_uid"], "role": c.get("role"),
             "email": c.get("email")}
            for c in contacts if c.get("preferred_for_submissions")
        ],
    }
    return payload


# -- Contact observation views (Contact Intelligence, Phase 9) ----------------
#
# Read-path derivation only: storage rows are never rewritten. Each contact
# is annotated with the method it was observed through, its raw and
# normalized value, and a strictly presence-derived identity state so the
# console can distinguish an anonymous observed value (e.g. a bare station
# phone number) from an attributed person or role-based contact without
# ever inventing names or roles.

_UNATTRIBUTED = "unattributed_observation"


def _normalized_phone(raw: str) -> str:
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return f"+1{digits}" if len(digits) == 10 else raw


def _contact_method(contact: dict) -> str | None:
    if contact.get("email"):
        return "email"
    if contact.get("phone"):
        return "phone"
    return None


def _identity_state(contact: dict) -> str:
    """Presence-derived only: no inference about who the contact is."""
    if str(contact.get("name") or "").strip():
        return "named"
    role = str(contact.get("role") or "").strip().lower()
    if role and role != "unknown":
        return "role_based"
    return _UNATTRIBUTED


def _annotate_contact(contact: dict) -> dict:
    view = dict(contact)
    method = _contact_method(contact)
    view["method"] = method
    if method == "email":
        raw = str(contact["email"])
        view["value_raw"] = raw
        view["value_normalized"] = raw.strip().lower()
    elif method == "phone":
        raw = str(contact["phone"])
        view["value_raw"] = raw
        view["value_normalized"] = _normalized_phone(raw)
    else:
        view["value_raw"] = None
        view["value_normalized"] = None
    view["identity_state"] = _identity_state(contact)
    view["observations"] = 1
    view["evidence_state"] = _contact_evidence_state(contact)
    view["role_reason"] = _contact_role_reason(contact)
    view["sources"] = _distinct_sources(contact)
    view["route_class"] = outreach_intel.contact_route_class(contact.get("role"))
    return view


def _merge_unattributed(views: list[dict]) -> list[dict]:
    """Collapse repeat observations of the same anonymous value.

    Applies ONLY to identity_state == unattributed_observation entries that
    share method + normalized value. Evidence is never dropped: provenance
    lists are unioned in first-seen order and ``observations`` records how
    many stored rows agree.
    """
    merged_by_key: dict[tuple[str, str], dict] = {}
    seen_by_key: dict[tuple[str, str], set] = {}
    output: list[dict] = []
    for view in views:
        if view.get("identity_state") != _UNATTRIBUTED \
                or view.get("method") is None:
            output.append(view)
            continue
        key = (view["method"], view["value_normalized"])
        existing = merged_by_key.get(key)
        if existing is None:
            merged_view = dict(view)
            merged_view["observations"] = 1
            seen = {
                _prov_token(prov)
                for prov in merged_view.get("provenance") or []}
            merged_by_key[key] = merged_view
            seen_by_key[key] = seen
            output.append(merged_view)
            continue
        existing["observations"] = existing.get("observations", 1) + 1
        for prov in view.get("provenance") or []:
            token = _prov_token(prov)
            if token not in seen_by_key[key]:
                seen_by_key[key].add(token)
                existing.setdefault("provenance", []).append(prov)
        if not existing.get("source_url") and view.get("source_url"):
            existing["source_url"] = view["source_url"]
    return output


def _prov_token(prov: object) -> str:
    if not isinstance(prov, dict):
        return repr(prov)
    return "\x1f".join((
        str(prov.get("value") or ""),
        str(prov.get("source_url") or ""),
        str(prov.get("method") or ""),
    ))


def _contact_views(contacts: list[dict]) -> list[dict]:
    return _merge_unattributed(
        [_annotate_contact(c) for c in contacts])


def _annotate_contact_routes(views: list[dict],
                             station_routes: list[dict]) -> None:
    """Attach per-contact reachability (read-path only, in place on copies).

    A contact and its routes stay separate concepts: ``relevance`` is the
    person's role weight, ``outreach_routes`` is every way to actually reach
    them (their own channel first, then verified station routes), and
    ``best_outreach_route`` is the single recommended one. ``can_add_to_campaign``
    is true exactly when a sendable route (verified email or http(s) station
    route) exists — a route-less person stays honest and markable, never
    falsely "reachable".
    """
    for view in views:
        view["relevance"] = outreach_intel.contact_relevance(view)
        view["outreach_routes"] = outreach_intel.contact_outreach_routes(
            view, station_routes)
        best = outreach_intel.best_contact_outreach_route(
            view, station_routes)
        view["best_outreach_route"] = best
        view["can_add_to_campaign"] = bool(
            best and best.get("value")
            and best.get("verification_state") in (outreach_intel.VERIFIED,
                                                   outreach_intel.ACTIONABLE))


# -- Evidence-state model (Phase 10 repairs) -------------------------------
#
# Read-path derivation ONLY: storage rows are never rewritten. Every artifact
# the console shows carries an explicit evidence state so it can be presented
# honestly instead of "verified or empty":
#
#   VERIFIED         a concrete observable was recorded (page reachable,
#                    email observed on an official page)
#   EVIDENCE-BACKED  a concrete channel is on record (phone), but not the
#                    strongest channel; or discovery combined with a
#                    submission-relevant page
#   DISCOVERED       the artifact exists in evidence but is unconfirmed
#                    (page found, reachability not verified; contact with no
#                    outreach channel)
#   UNKNOWN          nothing was observed (dedicated field absent)

def _useful_page_view(page: dict) -> dict:
    view = dict(page)
    reachable = page.get("reachable")
    view["evidence_state"] = "VERIFIED" if reachable is True else "DISCOVERED"
    if reachable is True:
        view["evidence_note"] = "verified reachable"
    elif reachable is None:
        view["evidence_note"] = "discovered link; reachability not yet confirmed"
    else:
        view["evidence_note"] = "discovered link; currently unreachable"
    semantics = outreach_intel.PAGE_ROUTE_SEMANTICS.get(
        page.get("category"), outreach_intel.PAGE_ROUTE_SEMANTICS["other"])
    view["kind"] = semantics["kind"]
    view["route_class"] = semantics["class"]
    view["why"] = semantics["why"]
    view["actionability"] = semantics["actionability"]
    view["next_step"] = semantics["next_step"]
    return view


def _contact_evidence_state(contact: dict) -> str:
    if contact.get("email"):
        return "VERIFIED"
    if contact.get("phone"):
        return "EVIDENCE-BACKED"
    return "DISCOVERED"


def _contact_role_reason(contact: dict) -> str:
    role = str(contact.get("role") or "").strip()
    if role and role != "unknown":
        return f"{role.replace('_', ' ')} - role label on station website"
    if str(contact.get("name") or "").strip():
        return "named contact from station website"
    return "observed contact from station website"


def _distinct_sources(contact: dict) -> list[str]:
    urls: list[str] = []
    for candidate in [contact.get("source_url")] + [
            p.get("source_url") for p in (contact.get("provenance") or [])
            if isinstance(p, dict)]:
        if isinstance(candidate, str) and candidate and candidate not in urls:
            urls.append(candidate)
    return urls


_SUBMISSION_STATUS_DIRECT = "DIRECT_SUBMISSION"
_SUBMISSION_STATUS_CONTACT = "CONTACT_FOR_SUBMISSION"
_SUBMISSION_STATUS_SHOW = "SHOW_SPECIFIC_OPPORTUNITY"
_SUBMISSION_STATUS_NONE = "NO_PUBLIC_SUBMISSION_ROUTE_FOUND"
_SUBMISSION_STATUS_UNKNOWN = "UNKNOWN"

_DECISION_ROLE_ORDER = {
    "music_director": 0,
    "program_director": 1,
    "music_programmer": 2,
    "music_submission": 3,
    "programming": 4,
    "music_scheduler": 5,
    "music_coordinator": 6,
}
_DECISION_ROLE_SET = frozenset(_DECISION_ROLE_ORDER)


def _submission_page_from(useful_pages: list[dict]) -> dict | None:
    candidates = [
        p for p in useful_pages or []
        if p.get("category") in ("send_music", "submission_guidelines")]
    if not candidates:
        return None
    candidates.sort(key=lambda p: (0 if p.get("reachable") is True else 1,
                                   p.get("url") or ""))
    return candidates[0]


def _best_contact_action(contacts: list[dict]) -> dict | None:
    decision = [
        c for c in contacts or []
        if c.get("role") in _DECISION_ROLE_SET
        and (c.get("email") or c.get("phone"))]
    if not decision:
        return None
    decision.sort(key=lambda c: (_DECISION_ROLE_ORDER.get(c.get("role"), 99),
                                 c.get("role") or ""))
    top = decision[0]
    role = str(top.get("role") or "").strip()
    if top.get("email"):
        label = ("Contact music director" if role == "music_director"
                 else "Contact program director"
                 if role == "program_director"
                 else "Contact music department")
        return {"url": f"mailto:{top['email']}",
                "label": label, "detail": top["email"]}
    if top.get("phone"):
        label = ("Call music director"
                 if role == "music_director"
                 else "Call music department")
        return {"url": None, "label": label, "detail": top["phone"]}
    return None


def submission_status_and_best_action(
    useful_pages: list[dict],
    submission: dict | None,
    contacts: list[dict],
    investigated: bool,
) -> tuple[str, str, dict]:
    """Derive the station's submission intelligence state and one best action.

    Returns ``(status, reason, best_action)``. ``best_action`` is a single
    clear next step; ``kind`` is one of submit|contact|browse|none. Nothing is
    ever fabricated: URLs come from stored facts / exact discovered pages.
    """
    submission_url = None
    if submission and isinstance(submission.get("submission_url"), dict):
        value = (submission["submission_url"] or {}).get("value")
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            submission_url = value
    submission_email = submission.get("submission_email") if submission else None
    sub_page = _submission_page_from(useful_pages)

    if submission_url or sub_page or submission_email:
        if submission_url:
            url, label, detail = submission_url, "official submission page", ""
            reason = "dedicated submission URL recorded on the station site"
        elif sub_page:
            url, label, detail = sub_page["url"], \
                sub_page.get("label") or "submission page", ""
            # The dedicated submission page is stronger evidence than an
            # inferred generic email: its anchor text was explicitly about
            # music submissions.
            reason = "station submission page discovered on the station site"
        else:
            url, label, detail = f"mailto:{submission_email}", \
                "submission email", submission_email
            reason = f"submission email on the station site ({submission_email})"
        action = {"kind": "submit", "label": "Send music",
                  "url": url, "detail": detail}
        return _SUBMISSION_STATUS_DIRECT, reason, action

    contact_action = _best_contact_action(contacts)
    contact_page = next(
        (p for p in useful_pages or [] if p.get("category") == "contact"), None)
    if contact_action or contact_page:
        if contact_action:
            action = {"kind": "contact", "url": contact_action["url"],
                      "label": contact_action["label"],
                      "detail": contact_action["detail"],
                      "reason": "music decision-maker contact on the station site"}
            reason = (f"{contact_action['label']} "
                      f"({contact_action['detail']}) on the station site")
        else:
            action = {"kind": "browse", "label": "Contact station",
                      "url": contact_page["url"],
                      "detail": contact_page.get("label") or "",
                      "reason": "station contact page discovered on the station site"}
            reason = "station contact page found - reach out for music submissions"
        return _SUBMISSION_STATUS_CONTACT, reason, action

    opportunity_page = next(
        (p for p in useful_pages or []
         if p.get("category") in ("dj_directory", "programming")), None)
    if opportunity_page:
        action = {"kind": "browse", "label": "Browse station DJs & programming",
                  "url": opportunity_page["url"],
                  "detail": opportunity_page.get("label") or "",
                  "reason": "station DJ/programming pages found — show-specific outreach"}
        return _SUBMISSION_STATUS_SHOW, \
            "station DJ/programming pages found (show-specific opportunity)", \
            action

    if investigated:
        action = {"kind": "none", "label": "No public submission route found",
                  "url": None, "detail": "",
                  "reason": "station site investigated; no public submission "
                            "or music-contact route surfaced"}
        return _SUBMISSION_STATUS_NONE, \
            "investigated the station site and found no public route", action

    action = {"kind": "none", "label": "Submission route not yet investigated",
              "url": None, "detail": "",
              "reason": "no pages or facts recorded for this station yet"}
    return _SUBMISSION_STATUS_UNKNOWN, \
        "station has not been investigated yet", action


# -- Phase 8: submission assets + link accessibility --------------------------
#
# Contract rule (approved boundary correction): asset responses carry the
# opaque track_id and business metadata ONLY — never storage locations.

TRACK_FIELDS = (
    "track_id", "sha256", "original_filename", "size_bytes", "content_type",
    "status", "reject_reason", "notes", "created_at", "updated_at",
)


def track_projection(row: dict) -> dict:
    projection = {key: row.get(key) for key in TRACK_FIELDS}
    projection["links"] = {"self": f"/api/v1/tracks/{row['track_id']}"}
    return projection


def tracks_payload(rows: list[dict], total: int, limit: int,
                   offset: int) -> dict:
    return {
        "tracks": [track_projection(r) for r in rows],
        "total": total, "limit": limit, "offset": offset,
    }


def submission_view(identity_key: str, submission: dict | None,
                    last_checks: list[dict] | None = None) -> dict:
    return {
        "identity_key": identity_key,
        "submission": dict(submission) if submission else None,
        "last_checks": [dict(c) for c in (last_checks or [])],
    }


def error_body(code: str, message: str) -> dict:
    return {"ok": False, "data": None,
            "error": {"code": code, "message": message}}


def success_body(data) -> dict:
    return {"ok": True, "data": data, "error": None}
