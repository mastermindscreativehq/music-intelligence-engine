"""Transparent, additive confidence scoring for station records.

Every point of the score is explainable: weights are constants below and the
reasons are returned alongside the score. No opaque model output.

Email quality feeds the score but NEVER implies deliverability — Phase 2
semantics are "publicly discovered", not "verified".
"""

from __future__ import annotations

WEIGHTS = {
    "website_reachable": 0.25,
    "official_domain": 0.10,
    "name_match": 0.15,
    "contact_page": 0.15,
    "own_domain_email": 0.20,
    "generic_email": 0.10,
    "free_email": 0.03,
    "submission_page": 0.15,
    "station_type_evidence": 0.05,
    "multiple_sources": 0.10,
}
PENALTY_BROKEN_SITE = -0.30
PENALTY_IDENTITY_MISMATCH = -0.15


def score_station(record: dict) -> tuple[float, list[str]]:
    """Return (score in [0,1], human-readable reasons) for a record dict."""
    reasons: list[str] = []
    score = 0.0

    if record.get("website_reachable"):
        score += WEIGHTS["website_reachable"]
        reasons.append("official website reachable")
    else:
        score += PENALTY_BROKEN_SITE
        reasons.append("website unreachable or broken at discovery time")

    domain = (record.get("website") or "").lower()
    if domain:
        score += WEIGHTS["official_domain"]
        reasons.append("dedicated website domain present")

    if record.get("name_matches_site"):
        score += WEIGHTS["name_match"]
        reasons.append("station name confirmed on website title")
    elif record.get("website_reachable"):
        score += PENALTY_IDENTITY_MISMATCH
        reasons.append("site title does not resemble station name")

    if record.get("contact_url"):
        score += WEIGHTS["contact_page"]
        reasons.append("public contact page found")

    emails = record.get("emails") or []
    own_domain = any(
        "own_domain" in (f.get("quality") or {}).get("signals", [])
        for f in emails
    )
    free_only = bool(emails) and all(
        "free_provider" in (f.get("quality") or {}).get("signals", [])
        for f in emails
    )
    if emails and own_domain:
        score += WEIGHTS["own_domain_email"]
        reasons.append("professional contact email on station domain found")
    elif emails:
        score += WEIGHTS["free_email"] if free_only else WEIGHTS["generic_email"]
        reasons.append(
            "contact email found on third-party/free provider"
            if not free_only else "only free-provider email found"
        )

    if record.get("submission_url"):
        score += WEIGHTS["submission_page"]
        reasons.append("public music submission page found")

    if record.get("station_type") not in (None, "", "unknown"):
        score += WEIGHTS["station_type_evidence"]
        reasons.append(f"station type evidence ({record.get('station_type')})")

    if len(record.get("source_urls") or []) >= 2:
        score += WEIGHTS["multiple_sources"]
        reasons.append("discovered via multiple source references")

    final = max(0.0, min(1.0, round(score, 2)))
    return final, reasons


def rescore(record: dict) -> dict:
    """Recompute score/reasons on a record dict in place; returns it."""
    score, reasons = score_station(record)
    record["confidence_score"] = score
    record["confidence_reasons"] = reasons
    return record


# ---------------------------------------------------------------------------
# Phase 3: transparent contact-level confidence.
# ---------------------------------------------------------------------------

CONTACT_SOURCE_WEIGHTS = {
    "submission_page": 0.15,
    "contact_page": 0.12,
    "official_website_page": 0.08,
    "official_website_mailto": 0.10,
}
CONTACT_MUSIC_ROLE_BONUS = 0.15      # music_director/music_submission/programming
CONTACT_SPECIFIC_PERSON_BONUS = 0.15  # named individual
CONTACT_OWN_DOMAIN_BONUS = 0.25
CONTACT_FREE_PROVIDER_PENALTY = -0.10
CONTACT_CAP = 0.95   # unverified contacts never reach 1.0 (Phase 3 ≠ verification)


def score_contact(contact: dict, station_domains: set[str]) -> tuple[float, list[str]]:
    """Explainable confidence for one enriched contact dict.

    Deliberately capped below 1.0: extraction is not verification.
    """
    reasons: list[str] = []
    score = 0.3

    source_type = _classify_source_type(contact.get("source_url") or "")
    weight = CONTACT_SOURCE_WEIGHTS.get(
        contact.get("source_kind") or source_type,
        CONTACT_SOURCE_WEIGHTS["official_website_page"],
    )
    score += weight
    reasons.append(f"evidence source: {source_type}")

    if contact.get("email"):
        email = contact["email"]
        domain = email.rsplit("@", 1)[-1]
        own = any(domain == d or domain.endswith("." + d)
                  for d in station_domains or set())
        if own:
            score += CONTACT_OWN_DOMAIN_BONUS
            reasons.append("email on station domain")
        elif "@" in email:
            reasons.append("third-party email domain")
        quality = ((contact.get("quality") or {})
                   if isinstance(contact.get("quality"), dict) else {})
        if quality.get("free_provider") or (
                email.rsplit("@", 1)[-1] in _FREE_PROVIDERS):
            score += CONTACT_FREE_PROVIDER_PENALTY
            reasons.append("free-provider email reduces confidence")

    if contact.get("name"):
        score += CONTACT_SPECIFIC_PERSON_BONUS
        reasons.append("named person extracted with adjacency evidence")

    if contact.get("role") in MUSIC_ROLES:
        score += CONTACT_MUSIC_ROLE_BONUS
        reasons.append(f"music-relevant role evidence ({contact.get('role')})")
    elif contact.get("role") in ("unknown", None, ""):
        reasons.append("role could not be determined from context")

    final = max(0.05, min(CONTACT_CAP, round(score, 2)))
    return final, reasons


MUSIC_ROLES = {"music_director", "music_submission", "programming",
               "music_programmer", "program_director"}

_FREE_PROVIDERS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
    "icloud.com", "live.com", "msn.com",
}


def _classify_source_type(url: str) -> str:
    lowered = (url or "").lower()
    if "submi" in lowered:
        return "submission_page"
    if "contact" in lowered:
        return "contact_page"
    return "official_website_page"