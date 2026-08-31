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
from urllib.parse import urlsplit

from crawler.pages import ParsedPage
from crawler.urls import canonical_domain

from discovery.radio.schema import (
    EnrichedContact,
    RadioIntelligenceRecord,
    SourceFetchRecord,
    SubmissionPath,
    UsefulPage,
)

from enrichment import submissions as submission_intel
from enrichment.confidence import score_contact
from enrichment.contacts import build_contacts_from_page
from enrichment.emails import email_quality, extract_emails_from_text, normalize_email
from enrichment.formats import detect_formats, detect_genres, extract_market
from enrichment.staff_directory import _looks_like_person_name
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

# Roles that justify keeping a name-only contact (no email) — these are
# music-programming-relevant positions worth preserving for later enrichment.
_CONTACT_MUSIC_ROLES = {
    "music_director", "program_director", "music_programmer",
    "music_submission", "programming", "music_scheduler",
    "music_coordinator", "host", "dj",
}

# Relevance ordering for presentation: music-submission decision-makers
# first, then progressively less submission-relevant roles.  Keeps
# advertising / generic roles from outranking music decision-makers in
# the surfaced contact list while preserving all qualified contacts.
_CONTACT_RELEVANCE = {
    "music_director": 0,
    "program_director": 1,
    "music_programmer": 2,
    "music_submission": 3,
    "programming": 4,
    "music_scheduler": 5,
    "music_coordinator": 6,
    "host": 7,
    "dj": 8,
    "media": 9,
    "booking": 10,
    "producer": 11,
    "advertising": 12,
    "general": 13,
}
_RELEVANCE_DEFAULT = 20


def _is_qualified_contact(raw: dict) -> bool:
    """Return True when a raw contact dict has intelligence value.

    Three tiers:
    - actionable: has email (highest priority — submission channel).
    - intelligence-only: named person with music-relevant role (useful
      context for later enrichment even without email).
    - rejected: name-only without email and without music role, or
      navigation/UI false positive with no intelligence value.

    A contact name, when present, must plausibly look like a human
    person's name.  A role label match is NEVER sufficient evidence that
    nearby text is a person's name — the name itself must pass the
    structural person-name validator (``_looks_like_person_name``).  This
    blocks navigation/UI/search/promotional labels (e.g. "Record Fair",
    "Advanced Search") from entering the intelligence data even when a
    role keyword happens to appear nearby.
    """
    name = raw.get("name")
    if name:
        # Structural person-name gate: reject navigation/UI/org labels
        # that slipped through role-adjacency extraction.
        if not _looks_like_person_name(str(name)):
            return False
    if raw.get("email"):
        return True
    role = raw.get("role") or "unknown"
    if role in _CONTACT_MUSIC_ROLES:
        return True
    return False

_INSTRUCTION_PAGE_RE = re.compile(r"submi", re.I)
_SUBMISSION_LOCALPARTS = {"music", "submissions", "submit", "md",
                          "musicdept", "programming"}

# Ordered classification rules for discovered station-level useful pages.
# The CATEGORY is an inference from the anchor text (with the page URL only as
# secondary context); the displayed URL always remains the exact discovered
# href. First rule to match wins (more specific categories come first).
_USEFUL_CATEGORY_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("send_music", re.compile(
        r"send[\s\-_]?us[\s\-_]?(your)?[\s\-_]?music|"
        r"send[\s\-_]?(your|us)[\s\-_]?music|"
        r"submit[\s\-_]?(your)?[\s\-_]?music|"
        r"music[\s\-_]?submissions?|give[\s\-_]?us[\s\-_]?(your)?[\s\-_]?music|"
        r"promo" , re.I)),
    ("submission_guidelines", re.compile(
        r"submission[\s\-_]?guidelines|how[\s\-_]?to[\s\-_]?submit|"
        r"(guidelines|requirements)\b", re.I)),
    ("dj_directory", re.compile(
        r"\bdjs?\b|dj(?:s|'s)?\b|disc[\s\-]?jockeys?\b|deejays?\b|"
        r"air[\s\-]?staff\b|on[\s\-]?air[\s\-]?(personalities|staff|hosts)\b|"
        r"\bhosts?\b|\bdjs?(?:[\s\-]?(and|&)[\s\-]?staff)?(\s+(directory|pages|email|list))?",
        re.I)),
    ("contact", re.compile(
        r"contact[\s\-_]?(us)?\b|get[\s\-_]?in[\s\-_]?touch\b|email[\s\-_]?us\b|"
        r"reach[\s\-_]?us\b|\bdirectory\b|staff(?:[\s\-_]?directory)?\b|"
        r"\bpeople\b|\bteam\b", re.I)),
    ("programming", re.compile(
        r"programming\b|program[\s\-_]?director\b|\bshows?\b|\bschedule\b",
        re.I)),
    ("about", re.compile(r"\babout([\s\-_]?us)?\b$", re.I)),
]

# Useful-page category display priority (lower = more prominent). Mirrors the
# ordering a user cares about for outreach; never affects the stored URL.
_USEFUL_CATEGORY_ORDER = {
    "send_music": 0,
    "submission_guidelines": 1,
    "dj_directory": 2,
    "programming": 3,
    "contact": 4,
    "about": 5,
    "other": 6,
}


def classify_useful_page(label: str, url: str = "") -> str:
    """Return a useful-page category key inferred from *label* text.

    Pure classification helper: it never fabricates or alters a URL.
    """
    text = f"{label or ''} {url or ''}".strip().lower()
    if not text:
        return "other"
    for category, pattern in _USEFUL_CATEGORY_RULES:
        if pattern.search(text):
            return category
    return "other"


def _unknown_page_reachable(url: str, fetch_records) -> tuple[bool | None, int | None]:
    """Reachability evidence for *url* if this exact URL was fetched."""
    for f in fetch_records or []:
        try:
            if f.url == url or f.url == url.rstrip("/") or \
                    f.url.rstrip("/") == url.rstrip("/"):
                return bool(f.ok), f.status
        except AttributeError:
            pass
    return None, None


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
    contacts_by_person_role: dict[tuple[str, str], EnrichedContact] = {}
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
        if contact.name and not _looks_like_person_name(str(contact.name)):
            continue  # navigation/UI label already in legacy data: drop
        ordered.append(contact)
        if contact.email:
            contacts_by_email[contact.email] = contact
        if contact.name:
            contacts_by_person_role[(contact.name.strip().lower(),
                                     contact.role)] = contact
    for page in pages:
        for raw in build_contacts_from_page(page):
            if not _is_qualified_contact(raw):
                continue
            email = raw.get("email")
            name = (raw.get("name") or "").strip()
            match = contacts_by_email.get(email) if email else None
            # Deduplicate the same person+role discovered on multiple
            # pages (e.g. the same staffer listed on several pages), even
            # when they have no email.  Prevents duplicate contacts and
            # merges useful provenance from each source.
            if match is None and name:
                match = contacts_by_person_role.get((name.lower(),
                                                     str(raw.get("role")
                                                         or "unknown")))
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
                if fresh.name:
                    contacts_by_person_role[(fresh.name.strip().lower(),
                                             fresh.role)] = fresh
            else:
                for prov in raw.get("provenance") or []:
                    if prov not in match.provenance:
                        match.provenance.append(prov)
                if match.name is None and raw.get("name"):
                    match.name = raw["name"]
                if match.phone is None and raw.get("phone"):
                    match.phone = raw["phone"]
                if raw.get("email") and not match.email:
                    match.email = raw["email"]
                    contacts_by_email[match.email] = match

    for contact in ordered:
        score, reasons = score_contact(contact.to_dict(), site_domains)
        contact.confidence_score = score
        contact.confidence_reasons = reasons
    # Surface music-submission decision-makers before lower-priority roles.
    ordered.sort(key=lambda c: (
        _CONTACT_RELEVANCE.get(c.role, _RELEVANCE_DEFAULT),
        -c.confidence_score,
    ))
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

    # --- station-level useful pages (evidence-backed discovered links) --------
    record.useful_pages = build_useful_pages(
        pages, fetch_records or [], site_domains)
    # Persist the evidence-backed list alongside the station record so the
    # API contract can surface it verbatim on later reads.
    record.raw_metadata["useful_pages"] = [
        p.to_dict() for p in record.useful_pages
    ]

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


# Non-navigational / non-evidence schemes that must NEVER surface as a useful
# page URL. ``ParsedPage.links`` already restricts to http(s), but the builder
# re-guards defensively so a future caller can't smuggle one in.
_USABLE_PAGE_SCHEME_RE = re.compile(r"^https?://", re.I)


def _same_document_path(url: str, source_url: str) -> bool:
    """True if *url* is the same document as *source_url* (same scheme+host+
    path; fragments ignore), i.e. an intra-page anchor rather than a route."""
    try:
        u = urlsplit(url)
        s = urlsplit(source_url)
    except ValueError:
        return False
    if u.scheme.lower() != s.scheme.lower():
        return False
    if (u.netloc or "").lower() != (s.netloc or "").lower():
        return False
    return _norm_path(u.path) == _norm_path(s.path)


def _norm_path(path: str) -> str:
    return (path or "/").rstrip("/") or "/"


# Non-document resources that are never a navigational "page": audio streams,
# playlist/config payloads, feeds, images/docs, media players. A Useful Page
# must be an actual HTML/document destination.
_MEDIA_FEED_EXT = (
    ".m3u", ".m3u8", ".pls", ".asx", ".asf", ".ram", ".rm", ".swf",
    ".mp3", ".mp4", ".m4a", ".aac", ".ogg", ".wav", ".flac", ".avi", ".mov",
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico",
    ".xml", ".rss", ".atom", ".json", ".ics", ".vcf", ".zip", ".dmg",
    ".exe", ".apk", ".css", ".js", ".ts",
)
_MEDIA_FEED_RE = re.compile(
    r"(?:" + "|".join(
        re.escape(e) + r"$" for e in sorted(_MEDIA_FEED_EXT, key=len, reverse=True)
    ) + r")",
    re.I,
)

_INDIVIDUAL_ID_PARAMS = ["id", "uid", "user", "userid", "user_id", "to",
                         "from", "person", "profile", "member", "contactid"]
# Individual email/contact-FORM endpoints (a person's mail form), e.g.
# ``email.php?id=N``. This deliberately EXCLUDES station-level words like
# staff/directory/people/contact so a "DJ & staff email list" page (a station
# resource) is never mistaken for an individual route.
_INDIVIDUAL_EMAIL_PATH_RE = re.compile(
    r"(?:\bemail\b|\bmails\b|\bmailto\b|\bmessage\b|\bsend\b)", re.I)


def _is_media_or_feed_resource(url: str, label: str) -> bool:
    """True when the link targets a non-document media/feed/config payload.

    Only a navigational HTML page is a Useful Page; anything that is an audio
    stream, playlist/config file, feed, image, or static asset is not.
    """
    try:
        path = (urlsplit(url).path or "").rstrip("/")
    except (ValueError, AttributeError):
        return False
    if not path:
        return False
    return bool(_MEDIA_FEED_RE.search(path))


def _is_individual_contact_route(url: str, label: str) -> bool:
    """True when the link is a per-person email/contact route, not a station page.

    Station-level Useful Pages are kept strictly separate from individual
    people. A link that opens a specific person's email form — an
    ``email.php?id=N``-style endpoint with an individual identifier query —
    is an individual's route captured as a person contact elsewhere and must
    not surface as a station Useful Page. Detection is generic (never
    station-specific): an individual-email form path carrying a person id.
    """
    try:
        split = urlsplit(url)
    except (ValueError, AttributeError):
        return False
    path = (split.path or "").lower()
    query = (split.query or "").lower()
    if not _INDIVIDUAL_EMAIL_PATH_RE.search(path):
        return False
    return any(f"{p}=" in query for p in _INDIVIDUAL_ID_PARAMS)


# Session / auth / form-action chrome is NOT a navigational content page. It is
# the station's login/logout/register, a favicon callback, or a CDN/anti-spam
# helper (e.g. Cloudflare ``/cdn-cgi/email-protection``). None is a Useful Page.
_AUTH_CHROME_PATH_RE = re.compile(
    r"(?:^|/)(?:auth|login|log[-_]?in|logout|log[-_]?out|register|sign[-_]?in"
    r"|sign[-_]?up|signup|fav_?icon|cdn-cgi|email-protection)(?:/|$|\.)", re.I)
_AUTH_CHROME_QUERY_RE = re.compile(
    r"(?:^|[?&])a=|fav_?icon|type=(?:program|episode|channel|homepage)")


# Media-player / stream-player instances are per-episode media pages, not
# station-level navigational pages (e.g. ``flashplayer.php?show=N&archive=M``).
_PLAYER_PATH_RE = re.compile(r"(?:flashplayer|streambox|mediaplayer|audioplayer)", re.I)
_INSTANCE_QUERY_PARAMS = (
    "show", "archive", "episode", "player", "stream", "program",
    "track", "song", "audio", "video", "clip",
)


def _is_auth_or_chrome(url: str) -> bool:
    """True for login/auth/favicon/CDN form-chrome routes, not pages."""
    try:
        split = urlsplit(url)
    except (ValueError, AttributeError):
        return False
    path = (split.path or "").lower()
    query = (split.query or "").lower()
    return bool(_AUTH_CHROME_PATH_RE.search(path)
                or _AUTH_CHROME_QUERY_RE.search(query))


def _is_player_instance(url: str) -> bool:
    """True when the link is a per-episode media-player instance."""
    try:
        split = urlsplit(url)
    except (ValueError, AttributeError):
        return False
    path = (split.path or "").lower()
    query = (split.query or "").lower()
    if _PLAYER_PATH_RE.search(path):
        return True
    return any(f"{p}=" in query for p in _INSTANCE_QUERY_PARAMS)


# A per-item instance page carries a numeric database id or a calendar date
# somewhere in its path (e.g. a show route ``/playlists/shows/11198``, an
# archive file ``Playlists/Wfmu/top30.001110.html``, or
# ``/BT/Airplay_Lists/2018/2018-02-02.html``). These are individual items, not
# station-level navigational pages. Detection is generic (never station-specific).
_INSTANCE_ID_RE = re.compile(r"\d{5,}")
# A numeric database id as a whole path segment (e.g. ``/playlists/shows/2109``
# or an airplay date folder ``/Airplay_Lists/2015/``). Station page slugs are
# words, never a bare numeric segment.
_WHOLE_SEGMENT_ID_RE = re.compile(r"(?:^|/)\d{4,}(?:/|$)")
_INSTANCE_DATE_RE = re.compile(r"\b\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?\b")


def _is_per_item_instance(url: str) -> bool:
    """True when the path embeds a per-item instance id / calendar date.

    Distinguishes a station-level page (``/playlists/DJ``, ``/about/``) from a
    per-item page (``/playlists/shows/11198``, ``/archives/2023/0507.html``).
    Generic and never station-specific.
    """
    try:
        path = urlsplit(url).path
    except (ValueError, AttributeError):
        return False
    if not path:
        return False
    return bool(_INSTANCE_ID_RE.search(path)
                or _WHOLE_SEGMENT_ID_RE.search(path)
                or _INSTANCE_DATE_RE.search(path)
                or _UUID_RE.search(path))


# Per-episode / per-item media pages keyed by a UUID (e.g. an episode player
# route ``/shows/episode/simplecast/<uuid>``). A UUID is a per-item instance,
# never a station-level navigational page.
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


# A person's profile page lives under a people/dj/host collection with a
# person slug that follows (``/djs/albina-cabrera/``, ``/author/erics/``).
# The collection directory itself (``/djs/``) is a station page and is kept;
# the specific-person page under it is PEOPLE and excluded. Generic and never
# station-specific.
_PERSON_COLLECTION_SEG = re.compile(
    r"/(?:djs|dj|hosts|host|presenters|presenter|people|person|personality"
    r"|onair|on-air|staff|staff-directory|team|author|contributors|reporters)"
    r"(?:s)?/", re.I)


def _is_person_profile(url: str, label: str) -> bool:
    """True for a specific person's profile page, not a station page."""
    try:
        path = urlsplit(url).path
    except (ValueError, AttributeError):
        return False
    path = "/" + (path or "").lstrip("/")
    m = _PERSON_COLLECTION_SEG.search(path)
    if not m:
        return False
    # There must be a person slug AFTER the collection segment (e.g. `/djs/slug/`).
    rest = path[m.end():].lstrip("/")
    if not rest or rest.startswith(("#", "?")):
        return False
    return True


def build_useful_pages(
    pages: list[ParsedPage],
    fetch_records: list[SourceFetchRecord] | None = None,
    site_domains: set[str] | None = None,
) -> list[UsefulPage]:
    """Assemble evidence-backed station-level Useful Pages from crawled links.

    Every returned page preserves, verbatim, the EXACT resolved href that was
    discovered in an HTML anchor on a crawled page (``PageLink.href_absolute``)
    together with the exact anchor text (``PageLink.anchor_text``) and the
    source page URL where it was found. No URL is ever constructed from a
    label, domain, template, route convention, or guessed slug; no URL is
    invented when evidence is absent.

    ``site_domains`` (canonical registrable domains) restricts surfacing to
    the station's own site, keeping station-level pages separate from
    individual people/outreach routes.
    """
    seen: set[str] = set()
    result: list[UsefulPage] = []
    site = set(site_domains or ())

    for page in pages:
        for link in page.links:
            url = (link.href_absolute or "").strip()
            if not url or not _USABLE_PAGE_SCHEME_RE.match(url):
                continue
            # Never surface mailto:/javascript:/non-navigational hrefs.
            low = url.lower()
            if low.startswith(("mailto:", "javascript:", "tel:", "#")):
                continue
            source_url = (page.url or "").strip()
            # Same-page anchor (self-link or fragment-only "#..." navigation):
            # the resolved path equals the source page's path (optionally with
            # a fragment), so it navigates within the current document and is
            # not a distinct navigational route. Never surface it.
            if _same_document_path(url, source_url):
                continue
            # Station-level pages only: restrict to the station's own site
            # (or an explicitly in-site domain). Keep them separate from any
            # individual contact / outreach route.
            try:
                if site and canonical_domain(url) not in site:
                    continue
            except ValueError:
                continue

            label_parts = " ".join((link.anchor_text or "").split()).strip()
            label = label_parts or url
            # A Useful Page is a station-level navigational page. Links to
            # non-document resources (audio streams, feeds, media/config
            # payloads) are not pages — drop them. This keeps the list to
            # genuinely navigable station pages, never stream/feed clutter.
            if _is_media_or_feed_resource(url, label):
                continue
            # Individual-contact/email routes (e.g. a per-DJ email form) are
            # PEOPLE, not station pages. They are captured as person contacts
            # elsewhere; they must not be surfaced as station Useful Pages.
            if _is_individual_contact_route(url, label):
                continue
            # A specific person's profile page (``/djs/<name>/``) is PEOPLE,
            # not a station page. The collection directory itself is kept.
            if _is_person_profile(url, label):
                continue
            # Session/auth/form chrome and CDN machinery are not content pages.
            if _is_auth_or_chrome(url):
                continue
            # Per-episode media-player instances are not station pages.
            if _is_player_instance(url):
                continue
            # Per-item pages (episode/archive/data item with a numeric id or
            # calendar date in the path) are not station-level navigation.
            if _is_per_item_instance(url):
                continue

            exact = url
            if exact in seen:
                continue
            seen.add(exact)

            category = classify_useful_page(label, exact)
            reachable, status = _useful_page_reach(fetch_records, exact)
            result.append(UsefulPage(
                url=exact,
                label=label,
                category=category,
                source_url=source_url,
                method="link",
                discovered_at="",
                reachable=reachable,
                status=status,
                provenance=[{
                    "kind": "fact",
                    "source_url": source_url,
                    "url": exact,
                    "label": label,
                    "category": category,
                    "method": "link",
                }],
            ))

    # Most outreach-relevant categories first; stable within a category.
    result.sort(key=lambda p: (_USEFUL_CATEGORY_ORDER.get(p.category, 6),
                               p.url))
    return result


def _useful_page_reach(
    fetch_records: list[SourceFetchRecord],
    url: str,
) -> tuple[bool | None, int | None]:
    """Reachability evidence for *url* ONLY from a recorded fetch of that
    exact URL; never from a guess."""
    norm = url.rstrip("/")
    for f in fetch_records or []:
        try:
            if f.url.rstrip("/") == norm:
                return bool(f.ok), f.status
        except AttributeError:
            continue
    return None, None
