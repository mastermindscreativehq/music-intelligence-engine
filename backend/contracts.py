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
        "contacts": [dict(c) for c in contacts],
        "submission": dict(submission) if submission else None,
        "preferred_submission_contacts": [
            {"contact_uid": c["contact_uid"], "role": c.get("role"),
             "email": c.get("email")}
            for c in contacts if c.get("preferred_for_submissions")
        ],
    }
    return payload


def error_body(code: str, message: str) -> dict:
    return {"ok": False, "data": None,
            "error": {"code": code, "message": message}}


def success_body(data) -> dict:
    return {"ok": True, "data": data, "error": None}
