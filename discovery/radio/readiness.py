"""Outreach-readiness projection for enriched radio stations (Phase 1).

Pure layer: turns a serialized ``RadioIntelligenceRecord`` (dict) into an
evidence-backed *outreach readiness* projection consumed by outreach/users.
No IO happens here — everything is computed from fields that were already
extracted and verified upstream — so the projection is deterministic and
testable offline.

Readiness statuses:

- ``outreach_ready``: the station carried sufficient verified evidence and a
  usable music-submission route. Safe to feed the outreach handoff.
- ``needs_review``: at least one blocking reason applies (defined below). Such
  records are NEVER silently treated as qualified — they must be reviewed.
- ``rejected``: qualification rejected the station outright; nothing is
  actionable and the record is not persisted.

``needs_review`` reasons (never fabricated; each only fires from recorded
evidence):

- ``site_unreachable``: a live run fetched pages, and every fetch failed.
- ``insufficient_station_evidence``: qualification did not confirm the site as
  a station (needs_review / missing verdict).
- ``submission_route_unclear``: no usable music-submission route found.
- ``contact_not_verified``: no contact evidence at all (no contact page, no
  general contact email, no music/programming director contact, no contact
  record with an email).

The projection never invents a person, an email, or a URL: the ``person``
identity comes only from extracted name+role evidence, and the ``route`` value
only from records already carrying source/evidence provenance.
"""

from __future__ import annotations

from discovery.models import utc_now_iso

READINESS_OUTREACH_READY = "outreach_ready"
READINESS_NEEDS_REVIEW = "needs_review"
READINESS_REJECTED = "rejected"

REASON_SITE_UNREACHABLE = "site_unreachable"
REASON_SUBMISSION_ROUTE_UNCLEAR = "submission_route_unclear"
REASON_CONTACT_NOT_VERIFIED = "contact_not_verified"
REASON_INSUFFICIENT_STATION_EVIDENCE = "insufficient_station_evidence"

# How each reason reads as a one-line human note.
_REASON_LABELS = {
    REASON_SITE_UNREACHABLE:
        "station site unreachable on latest check",
    REASON_SUBMISSION_ROUTE_UNCLEAR:
        "no usable music-submission route found",
    REASON_CONTACT_NOT_VERIFIED:
        "no verified contact pathway found",
    REASON_INSUFFICIENT_STATION_EVIDENCE:
        "station qualification not confirmed",
}

# Person contact slots that may carry a named music/programming director.
_RANKED_DIRECTOR_ROLES = (
    ("music_director", "music_director_name", "music_director_email"),
    ("program_director", "program_director_name", "program_director_email"),
)


def _channels(record: dict) -> dict:
    try:
        meta = record.get("raw_metadata") or {}
    except AttributeError:
        meta = {}
    if isinstance(meta, dict) and isinstance(meta.get("contact_channels"), dict):
        return meta["contact_channels"]
    return {}


def _usable_route(record: dict) -> dict | None:
    """Best evidence-backed music-submission route, or None.

    Priority: dedicated submission URL -> submission email -> named
    music/programming director email. Only these three channel kinds qualify
    as a usable outreach route — a generic contact mailbox never does. A
    route only exists when the underlying value is a non-empty string with
    source/evidence provenance; no value is ever synthesized here.
    """
    channels = _channels(record)
    submission = (record.get("submission") or {}) or {}
    if not isinstance(submission, dict):
        submission = {}

    # 1. Dedicated submission URL (a verified webform/route).
    sub_url = submission.get("submission_url") or channels.get("music_submission_url")
    if isinstance(sub_url, dict) and isinstance(sub_url.get("value"), str) and sub_url["value"].strip():
        return {
            "kind": "webform",
            "value": sub_url["value"],
            "source_url": sub_url.get("source_url") or sub_url.get("value"),
            "field": "submission_url",
        }

    # 2. Submission email.
    sub_email = submission.get("submission_email")
    if isinstance(sub_email, str) and sub_email.strip():
        sub_email = sub_email.strip()
        return {
            "kind": "email",
            "value": sub_email,
            "source_url": _email_source(record, sub_email),
            "field": "submission_email",
        }

    # 3. Named director email (music first, then programming).
    for role, name_key, email_key in _RANKED_DIRECTOR_ROLES:
        email_slot = channels.get(email_key)
        if isinstance(email_slot, dict) and isinstance(email_slot.get("value"), str) and email_slot["value"].strip():
            return {
                "kind": "email",
                "value": email_slot["value"],
                "source_url": email_slot.get("source_url"),
                "field": f"{role}_email",
            }

    return None


def _person(record: dict) -> dict | None:
    """Best named outreach person, or None.

    Prefers the person layer's conservative projection
    (``raw_metadata["person"]``) computed by the enrichment engine from
    explicitly published name+role associations. Falls back to the
    evidence-backed director channels for offline/older records so the
    projection stays deterministic either way. A person is only ever reported
    from explicit evidence — never invented from a name alone.
    """
    meta = {}
    try:
        meta = record.get("raw_metadata") or {}
    except AttributeError:
        meta = {}
    if isinstance(meta, dict):
        projection = meta.get("person")
        if isinstance(projection, dict) and (projection.get("name") or ""):
            person = {
                "name": projection["name"],
                "role": projection.get("role"),
                "role_label": projection.get("role_label"),
                "source_url": projection.get("source_url"),
            }
            if projection.get("email"):
                person["email"] = projection["email"]
            return person
    channels = _channels(record)
    for role, name_key, email_key in _RANKED_DIRECTOR_ROLES:
        slot = channels.get(name_key)
        if not isinstance(slot, dict) or not (slot.get("value") or ""):
            continue
        person = {
            "name": slot["value"],
            "role": role,
            "source_url": slot.get("source_url"),
        }
        email_slot = channels.get(email_key)
        if isinstance(email_slot, dict) and (email_slot.get("value") or ""):
            person["email"] = email_slot["value"]
            person["email_source_url"] = email_slot.get("source_url")
        return person
    return None


def _email_source(record: dict, email: str) -> str | None:
    """Source URL for a submission email from page-level email facts."""
    for fact in record.get("emails") or []:
        if isinstance(fact, dict) and (fact.get("value") or "") == email:
            return fact.get("source_url")
    return None


def _has_contact_evidence(record: dict) -> bool:
    """True when the station exposes at least one verified contact pathway.

    Contact evidence is a separate gate from the route: it asks whether the
    station published *any* reachable contact (director name/email, a contact
    page, a contact record with an email, or a generic contact mailbox).
    Values must be non-empty strings — an empty/null slot never counts. This
    gate never promotes a station to outreach_ready on its own; without a
    verified route (submission URL / submission email / director email) the
    status stays needs_review.
    """
    channels = _channels(record)
    if isinstance(channels.get("general_contact_email"), str) and \
            channels["general_contact_email"].strip():
        return True
    for key in ("music_director_name", "music_director_email",
                "program_director_name", "program_director_email",
                "contact_url"):
        slot = channels.get(key)
        if isinstance(slot, dict) and isinstance(slot.get("value"), str) and slot["value"].strip():
            return True
    contact_url = record.get("contact_url")
    if isinstance(contact_url, dict) and isinstance(contact_url.get("value"), str) and contact_url["value"].strip():
        return True
    for contact in record.get("contacts") or []:
        if isinstance(contact, dict) and isinstance(contact.get("email"), str) and contact["email"].strip():
            return True
    return False


def _site_unreachable_live(record: dict) -> bool:
    """True when a live run fetched pages and every fetch failed."""
    fetches = record.get("fetches") or []
    if not fetches:
        return False
    ok_count = 0
    for fetch in fetches:
        if isinstance(fetch, dict) and fetch.get("ok"):
            ok_count += 1
    return ok_count == 0


def _qualification(record: dict) -> dict:
    try:
        meta = record.get("raw_metadata") or {}
    except AttributeError:
        meta = {}
    if isinstance(meta, dict) and isinstance(meta.get("qualification"), dict):
        return meta["qualification"]
    return {}


def compute_outreach_readiness(record: dict) -> dict:
    """Compute the outreach-readiness projection for one intelligence record."""
    qualification = _qualification(record)
    verdict = qualification.get("verdict")
    reasons: list[str] = []
    route = None
    person = _person(record)

    if verdict == "rejected":
        return {
            "status": READINESS_REJECTED,
            "reasons": [REASON_INSUFFICIENT_STATION_EVIDENCE],
            "reason_labels": [_REASON_LABELS[REASON_INSUFFICIENT_STATION_EVIDENCE]],
            "route": None,
            "person": person,
            "evidence": {
                "qualification": qualification,
                "website": record.get("website"),
            },
            "evaluated_at": utc_now_iso(),
        }

    # Order matters: deeper checks only run when the station itself is sound.
    if _site_unreachable_live(record):
        reasons.append(REASON_SITE_UNREACHABLE)

    if verdict != "qualified":  # needs_review / missing / anything else
        reasons.append(REASON_INSUFFICIENT_STATION_EVIDENCE)

    if reasons:
        return {
            "status": READINESS_NEEDS_REVIEW,
            "reasons": reasons,
            "reason_labels": [_REASON_LABELS[r] for r in reasons],
            "route": None,
            "person": person,
            "evidence": {
                "qualification": qualification,
                "website": record.get("website"),
                "channels": _channels(record) or None,
            },
            "evaluated_at": utc_now_iso(),
        }

    route = _usable_route(record)
    if route is None:
        reasons.append(REASON_SUBMISSION_ROUTE_UNCLEAR)

    if not _has_contact_evidence(record):
        reasons.append(REASON_CONTACT_NOT_VERIFIED)

    channels = _channels(record)
    status = READINESS_OUTREACH_READY if not reasons else READINESS_NEEDS_REVIEW
    return {
        "status": status,
        "reasons": reasons,
        "reason_labels": [_REASON_LABELS[r] for r in reasons],
        "route": route,
        "person": person,
        "evidence": {
            "qualification": qualification,
            "website": record.get("website"),
            "channels": channels or None,
            "contact_url": record.get("contact_url"),
        },
        "evaluated_at": utc_now_iso(),
    }