"""Radio intelligence assembly (Phase 3).

Pure layer: turns a discovered StationRecord (dict) plus optionally re-fetched
pages into a RadioIntelligenceRecord. No IO happens here — fetching is the
enrichment engine's job — so this module is deterministic and testable
offline.

Data-integrity rules enforced by construction:

- FACT values only ever originate from extracted/observed evidence;
- INFERENCE output (submission methods) is labeled ``kind: inference``;
- absent evidence leaves fields unset (UNKNOWN) — nothing is fabricated;
- merging unions provenance and never destroys earlier observations.
"""

from __future__ import annotations

import re

from crawler.pages import ParsedPage
from crawler.urls import canonical_domain

from discovery.radio.schema import (
    EnrichedContact,
    RadioIntelligenceRecord,
    SourceFetchRecord,
    SubmissionPath,
)

from enrichment import submissions as submission_intel
from enrichment.confidence import score_contact
from enrichment.contacts import build_contacts_from_page
from enrichment.emails import email_quality, extract_emails_from_text, normalize_email
from enrichment.formats import detect_formats, detect_genres, extract_market
from enrichment.stations import classify_station, detect_social_urls

# Preference order for choosing the station's primary music-submission
# contact. Only roles in this ranking may be preferred_for_submissions —
# a generic inbox without music evidence is NEVER auto-promoted.
_SUBMISSION_ROLE_RANK = {
    "music_director": 0,
    "music_submission": 1,
    "programming": 2,
    "music_programmer": 3,
    "program_director": 4,
}

_INSTRUCTION_PAGE_RE = re.compile(r"submi", re.I)
_SUBMISSION_LOCALPARTS = {"music", "submissions", "submit", "md",
                          "musicdept", "programming"}


def build_intelligence_record(
    station: dict,
    pages: list[ParsedPage],
    fetch_records: list[SourceFetchRecord] | None = None,
) -> RadioIntelligenceRecord:
    """Assemble an enriched intelligence record from a Phase 2 record.

    *station* must be the dict form of StationRecord (discovery output).
    *pages* are parsed HTML pages belonging to this station (possibly empty
    for offline enrichment). Existing facts always survive; page signals are
    unioned in without mutating the input record.
    """
    record = RadioIntelligenceRecord()
    record.station_id = str(station.get("id") or "")
    record.name = str(station.get("name") or "")
    record.alternate_names = list(station.get("alternate_names") or [])
    record.website = station.get("website")
    try:
        record.domain = canonical_domain(record.website) if record.website else None
    except ValueError:
        record.domain = None
    site_domains = {record.domain} if record.domain else set()

    # --- location -----------------------------------------------------------
    record.country = station.get("country")
    record.state_or_region = station.get("state_or_region")
    record.city = station.get("city")

    # --- characteristics ------------------------------------------------------
    texts = [page.text or "" for page in pages]
    titles = [page.title for page in pages if page.title]
    snippet = ((station.get("raw_metadata") or {}).get("candidate_snippet")) or ""
    all_texts = texts + ([snippet] if snippet else [])

    record.station_type = station.get("station_type") or "unknown"
    record.classification_confidence = float(
        station.get("classification_confidence") or 0.0)
    record.classification_evidence = list(
        station.get("classification_evidence") or [])
    if record.station_type == "unknown" and all_texts:
        classification = classify_station(all_texts)
        record.station_type = classification.station_type
        record.classification_confidence = classification.confidence
        record.classification_evidence = classification.evidence

    discovered_genres, genre_evidence = detect_genres(all_texts)
    merged_genres = list(dict.fromkeys(
        list(station.get("genres") or []) + discovered_genres))
    record.genres = merged_genres
    record.genre_evidence = genre_evidence

    detected_formats, _ = detect_formats(all_texts)
    carried_format = [station["format"]] if station.get("format") else []
    record.formats = list(dict.fromkeys(carried_format + detected_formats))

    record.market_area = extract_market(texts)
    record.language = station.get("language")
    record.description = snippet or station.get("description")

    # --- emails: union existing Facts with page extraction --------------------
    emails_by_value: dict[str, dict] = {}
    for fact in station.get("emails") or []:
        value = normalize_email(str(fact.get("value", "")))
        if not value:
            continue
        stored = dict(fact)
        stored["value"] = value
        stored.setdefault("also_seen_at", [])
        emails_by_value[value] = stored
    for page in pages:
        candidates = extract_emails_from_text(page.text or "")
        for mailto in page.mailtos:
            normalized = normalize_email(mailto.split("?", 1)[0])
            if normalized:
                candidates.append(normalized)
        for email in candidates:
            quality = email_quality(email, site_domains)
            existing = emails_by_value.get(email)
            if existing is None:
                emails_by_value[email] = _email_fact(email, page.url, quality)
            else:
                seen_at = existing.setdefault("also_seen_at", [])
                if page.url and page.url != existing.get("source_url") \
                        and page.url not in seen_at:
                    seen_at.append(page.url)
                existing.setdefault("quality", quality)
    record.emails = list(emails_by_value.values())

    # --- phones -----------------------------------------------------------------
    phones_by_value: dict[str, dict] = {}
    for fact in station.get("phone_numbers") or []:
        if isinstance(fact, dict) and fact.get("value"):
            phones_by_value[fact["value"]] = dict(fact)
    record.phone_numbers = list(phones_by_value.values())

    # --- contacts: preserve existing, rebuild from pages -------------------------
    contacts_by_email: dict[str, EnrichedContact] = {}
    ordered: list[EnrichedContact] = []
    for existing in station.get("contacts") or []:
        contact = EnrichedContact(
            id=str(existing.get("id")),
            station_id=record.station_id,
            name=existing.get("name"),
            role=str(existing.get("role") or "unknown"),
            email=existing.get("email"),
            phone=existing.get("phone"),
            source_url=existing.get("source_url"),
            confidence_score=float(existing.get("confidence_score") or 0),
            verified_at=existing.get("verified_at"),
            provenance=list(existing.get("provenance") or []),
        )
        ordered.append(contact)
        if contact.email:
            contacts_by_email[contact.email] = contact
    for page in pages:
        for raw in build_contacts_from_page(page):
            email = raw.get("email")
            match = contacts_by_email.get(email) if email else None
            if match is None:
                fresh = EnrichedContact(
                    station_id=record.station_id,
                    name=raw.get("name"),
                    role=str(raw.get("role") or "unknown"),
                    email=email,
                    phone=raw.get("phone"),
                    source_url=raw.get("source_url") or page.url,
                    provenance=list(raw.get("provenance") or []),
                )
                ordered.append(fresh)
                if email:
                    contacts_by_email[email] = fresh
            else:
                for prov in raw.get("provenance") or []:
                    match.provenance.append(prov)
                if match.name is None and raw.get("name"):
                    match.name = raw["name"]

    for contact in ordered:
        score, reasons = score_contact(contact.to_dict(), site_domains)
        contact.confidence_score = score
        contact.confidence_reasons = reasons
    record.contacts = ordered

    # --- socials --------------------------------------------------------------
    socials = dict(station.get("social_urls") or {})
    all_links = [link.href_absolute for page in pages for link in page.links]
    for platform, url in detect_social_urls(all_links).items():
        socials.setdefault(platform, url)
    record.social_urls = socials

    # --- sources & lifecycle -----------------------------------------------------
    merged_sources = list(dict.fromkeys(
        list(station.get("source_urls") or [])))
    ok_fetches = [f.url for f in (fetch_records or []) if f.ok]
    merged_sources += [u for u in ok_fetches if u not in set(merged_sources)]
    record.source_urls = merged_sources
    record.fetches = list(fetch_records or [])
    record.discovered_at = station.get("discovered_at") or ""
    record.last_verified_at = station.get("last_verified_at")
    record.last_observed_at = station.get("last_observed_at") or ""
    record.raw_metadata = {
        "homepage_title": (station.get("raw_metadata") or {})
        .get("homepage_title"),
        "pages_considered": [p.url for p in pages],
        "titles_observed": titles[:5],
        "enrichment_mode": "pages" if pages else "offline_facts_only",
    }

    # --- submission intelligence -----------------------------------------------
    record.submission = _build_submission_path(
        record,
        pages,
        station.get("submission_url")
        if isinstance(station.get("submission_url"), dict) else None,
    )

    # --- overall confidence: Phase 2 base + transparent enrichment deltas ------
    base = float(station.get("confidence_score") or 0.0)
    reasons = list(station.get("confidence_reasons") or [])
    if discovered_genres:
        base += 0.03
        reasons.append(
            f"genre evidence found ({', '.join(discovered_genres[:3])})")
    if record.submission and record.submission.instructions:
        base += 0.04
        reasons.append("submission instructions captured")
    if any(c.preferred_for_submissions for c in record.contacts):
        base += 0.03
        reasons.append("evidenced music-submission contact identified")
    record.confidence_score = max(0.0, min(1.0, round(base, 2)))
    record.confidence_reasons = reasons
    return record


def _build_submission_path(
    record: RadioIntelligenceRecord,
    pages: list[ParsedPage],
    discovery_submission_url: dict | None,
) -> SubmissionPath | None:
    """Assemble SubmissionPath strictly from evidence (may return None)."""
    path = SubmissionPath()
    path.submission_url = discovery_submission_url

    # Preferred music-submission contact (rank-limited; generic inboxes are
    # never promoted without their own music-role evidence).
    music_contacts = [
        c for c in record.contacts
        if c.role in _SUBMISSION_ROLE_RANK and (c.email or c.phone)
    ]
    music_contacts.sort(key=lambda c: (_SUBMISSION_ROLE_RANK[c.role],
                                       -c.confidence_score))
    if music_contacts:
        music_contacts[0].preferred_for_submissions = True
        path.programming_contact_role = music_contacts[0].role

    # Choose the most authoritative submission-ish page for text mining.
    submission_page: ParsedPage | None = None
    for page in pages:
        if _INSTRUCTION_PAGE_RE.search(page.url):
            submission_page = page
            break
    if submission_page is None:
        for page in pages:
            if submission_intel.extract_submission_instructions(page.text):
                submission_page = page
                break

    # Submission email: provenance must point at a submission page, or the
    # address itself / its owner carries explicit music-submission evidence.
    submission_email: str | None = None
    if submission_page is not None:
        page_emails = [
            e for e in extract_emails_from_text(submission_page.text or "") if e
        ] + [
            n for n in (
                normalize_email(m.split("?", 1)[0])
                for m in submission_page.mailtos
            ) if n
        ]
        for candidate in page_emails:
            quality = email_quality(candidate,
                                    {record.domain} if record.domain else set())
            localpart = candidate.rsplit("@", 1)[0]
            if "own_domain" in quality["signals"] or localpart in _SUBMISSION_LOCALPARTS:
                submission_email = candidate
                break
    if submission_email is None:
        for contact in music_contacts:
            if contact.role == "music_submission" and contact.email:
                submission_email = contact.email
                break

    if submission_page is not None:
        instructions = submission_intel.extract_submission_instructions(
            submission_page.text)
        if instructions:
            path.instructions = {
                "value": instructions,
                "source_url": submission_page.url,
                "source_type": "official_website_page",
                "method": "sentence_rule",
                "discovered_at": "",
                "also_seen_at": [],
            }
        path.restrictions = submission_intel.detect_restrictions(
            submission_page.text)

    has_form = bool(submission_page and submission_page.forms)
    path.methods = submission_intel.infer_submission_methods(
        has_form=has_form,
        submission_email=submission_email,
        texts=[p.text or "" for p in pages],
    )
    path.submission_email = submission_email

    # Explainable path confidence.
    score = 0.0
    reasons: list[str] = []
    if path.submission_url:
        score += 0.35
        reasons.append("dedicated submission URL discovered")
    if submission_email:
        score += 0.30
        reasons.append("submission email supported by page evidence")
    if path.instructions:
        score += 0.15
        reasons.append("explicit submission instructions captured")
    if path.methods and path.methods["methods"]:
        score += 0.10
        reasons.append(
            f"submission method(s) inferred "
            f"({', '.join(path.methods['methods'])})")
    if path.restrictions:
        score += 0.05
        reasons.append("submission restrictions documented")
    path.confidence_score = min(round(score, 2), 0.95)
    path.confidence_reasons = reasons

    if path.submission_url is None and not submission_email \
            and path.instructions is None and not music_contacts:
        return None
    return path


def _email_fact(value: str, source_url: str, quality: dict) -> dict:
    return {
        "value": value,
        "source_url": source_url,
        "source_type": "official_website_page",
        "method": "text_rule",
        "discovered_at": "",
        "also_seen_at": [],
        "quality": quality,
    }
