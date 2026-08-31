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
    summary["links"] = {
        "self": f"/api/v1/stations/{row['identity_key']}",
        "intelligence": f"/api/v1/stations/{row['identity_key']}/intelligence",
        "contacts": f"/api/v1/stations/{row['identity_key']}/contacts",
    }
    return summary


def station_detail(row: dict) -> dict:
    """Full stored station fields (JSON columns already decoded)."""
    detail = dict(row)   # every stored column is part of the contract
    detail["links"] = {
        "self": f"/api/v1/stations/{row['identity_key']}",
        "intelligence": f"/api/v1/stations/{row['identity_key']}/intelligence",
        "contacts": f"/api/v1/stations/{row['identity_key']}/contacts",
    }
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


def intelligence_payload(station: dict, emails: list[dict],
                         phones: list[dict], contacts: list[dict],
                         submission: dict | None,
                         fetches: list[dict] | None = None) -> dict:
    """Full intelligence view with an explicit FACT/INFERENCE/UNKNOWN map."""
    inferred: list[str] = []
    if submission and isinstance(submission.get("methods"), dict) \
            and submission["methods"].get("methods"):
        inferred.append("submission.methods")
    payload = {
        "station": station_detail(station),
        "emails": [dict(fact) for fact in emails],      # Fact dicts verbatim
        "phone_numbers": [dict(fact) for fact in phones],
        "contacts": [dict(c) for c in contacts],
        "submission": dict(submission) if submission else None,
        "fetches": [dict(f) for f in (fetches or [])],
        "useful_pages": [
            dict(p) for p in ((
                station.get("raw_metadata") or {}).get("useful_pages") or [])
            if isinstance(p, dict)
        ],
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
    payload = {
        "station_identity_key": station["identity_key"],
        "station_name": station["name"],
        "contacts": _contact_views(contacts),
        "submission": dict(submission) if submission else None,
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
