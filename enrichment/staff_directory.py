"""Staff-directory detection and structured person/email extraction.

Detects pages that list staff/team/people and extracts structured
name + role + email entries from the flat text output of the HTML
parser.  Designed to work with ``crawler.pages.ParsedPage`` which
flattens all HTML to text — no DOM access required.

Email intelligence is the primary goal.  Extraction priority:

1. Named person + relevant email + role (highest value).
2. Music/program/submission email without a named person.
3. Named person with role but no email (useful for later enrichment).
4. Phone numbers are secondary supporting data only — attached when
   found alongside an email, never stored as standalone contacts.

The extraction runs as a first pass inside
``enrichment.contacts.build_contacts_from_page`` when the page is
detected as a staff directory.  It produces contact-shaped dicts
compatible with the existing ingestion pipeline (same keys, same
provenance conventions).
"""

from __future__ import annotations

import re
import uuid

from crawler.pages import ParsedPage
from enrichment.emails import normalize_email
from enrichment.roles import classify_role

# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

_TITLE_SIGNALS = re.compile(
    r"\b(staff|team|people|our\s+people|meet\s+the\s+team|"
    r"directory|personnel|faculty|broadcasters|on[- ]air)\b",
    re.I,
)

_URL_SIGNALS = re.compile(
    r"(/staff|/team|/people|/about/staff|/about/team|/about/people|"
    r"/our[- ]team|/our[- ]staff|/meet[- ]the[- ]team|"
    r"/directory|/personnel|/broadcasters|/on[- ]air)",
    re.I,
)

# Heuristic: a page with many capitalized-word pairs near role keywords
# is likely a staff listing even without a telling title/URL.
_ROLE_KEYWORDS = re.compile(
    r"\b(music\s*director|program\s*director|station\s*manager|"
    r"general\s*manager|\bdj\b|host|presenter|producer|"
    r"programm?ing|media|booking|advertising|"
    r"music\s*(programmer|scheduler|coordinator)|"
    r"general\s*(inquiries|enquiries|questions)|"
    r"department|office)\b",
    re.I,
)

# Minimum number of name-like lines near role keywords to flag as staff page.
_MIN_STRUCTURED_ENTRIES = 3

# Words that appear in navigation menus, link text, and organizational
# labels but are NOT person names.  Used by _looks_like_person_name to
# filter false positives.
_NAVIGATION_WORDS = re.compile(
    r"\b(contact|donate|donation|donations|search|subscribe|subscribe|"
    r"stream|listen|live|play|archive|archives|merchandise|merch|"
    r"volunteer|volunteers|support|about|help|menu|faq|faqs|"
    r"privacy|policy|policies|terms|conditions|"
    r"record|records|fair|fairs|events|event|calendar|"
    r"news|newsletter|press|media|广告|vertise|advertising|"
    r"sponsors?|partners?|links|share|tweet|email|"
    r"login|sign|log|register|account|profile|"
    r"home|page|pages|site|web|back|next|prev|"
    r"service|services|cadre|sysadmin|database|"
    r"mailing|address|phone|fax|"
    r"staff|team|people|personnel|department|office|"
    r"inquiries|enquiries|questions|general|"
    r"esteemed|swag|submissions?|meeting|"
    r"yourself|myself|himself|herself|itself|ourselves|themselves)\b",
    re.I,
)

# Words that appear in show / program titles but almost never in real
# person names.  When any of these appear as standalone words in a line
# that _looks_like_person_name would otherwise accept, the line is
# rejected — it is a show title, not a person.
_SHOW_TITLE_WORDS = re.compile(
    r"\b(zone|sound|factory|radio|hour|show|club|lounge|theater|theatre|"
    r"melodrama|percussion|modernists|cofferdam|bonculator|bonsulator|"
    r"music|playlists?|clock|shake|sofrito|tongues|"
    r"transit|paradise|odyssey|mission|expedition|"
    r"machines?|robots?|monsters?|agents?|detectives?|"
    r"carnival|circus|freaks?|bandits?|outlaws?|pirates?|"
    r"bedroom|kitchen|bathroom|attic|basement|garage|"
    r"cafeteria|canteen|diner|tavern|saloon|"
    r"boulevard|avenue|highway|expressway|"
    r"junction|crossroads|terminus|destination|"
    r"warehouse|depot|terminal|platform|"
    r"junkyard|scrapyard|graveyard|"
    r"festival|celebration|spectacle|"
    r"revival|renaissance|resurgence|"
    r"playland|funfair|playlist|playbill|playbook)\b",
    re.I,
)


def is_staff_directory(page: ParsedPage) -> bool:
    """Return True when *page* looks like a staff/team/people directory.

    Three detection layers, any one suffices:

    1. ``<title>`` contains a staff/team/people signal.
    2. URL path contains a staff/team/people signal.
    3. Content heuristic: enough name-like lines near role keywords.
    """
    title = page.title or ""
    if _TITLE_SIGNALS.search(title):
        return True
    if _URL_SIGNALS.search(page.url):
        return True
    return _content_heuristic(page.text or "")


def _content_heuristic(text: str) -> bool:
    """Heuristic: many name-like lines with nearby role keywords."""
    if not text:
        return False
    lines = text.split("\n")
    hits = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or len(stripped) > 120:
            continue
        if not _looks_like_person_name(stripped):
            continue
        # Check same line and adjacent lines for role keywords.
        window = "\n".join(
            lines[max(0, i - 1):min(len(lines), i + 2)])
        if _ROLE_KEYWORDS.search(window):
            hits += 1
            if hits >= _MIN_STRUCTURED_ENTRIES:
                return True
    return False


# ---------------------------------------------------------------------------
# Name detection helpers
# ---------------------------------------------------------------------------

# 2-4 capitalized words, possibly hyphenated, with optional particles.
# [A-Z][a-zA-Z]+ allows Mc-prefix names (McGasko, McDonald, etc.).
_NAME_RE = re.compile(
    r"^[A-Z][a-z]+(?:[- ][A-Z][a-zA-Z]+)*"
    r"(?:\s+(?:de|van|von|del|da|di|le)\s+[A-Z][a-zA-Z]+(?:[- ][A-Za-z]+)*)*"
    r"(?:\s+[A-Z][a-zA-Z]+(?:[- ][A-Za-z]+)*){0,2}$"
)


def _looks_like_person_name(text: str) -> bool:
    """Conservative check: does *text* look like a person's name?"""
    text = text.strip().rstrip(",.")
    if not text:
        return False
    words = text.split()
    if not 2 <= len(words) <= 5:
        return False
    # Reject ALLCAPS words longer than 3 chars (e.g., "STATION STAFF").
    if any(w.isupper() and len(w) > 3 for w in words):
        return False
    # Reject if any word is a known role/title keyword — these are not names.
    if _ROLE_KEYWORDS.search(text):
        return False
    # Reject common navigation/UI words that look like names.
    if _NAVIGATION_WORDS.search(text):
        return False
    # Reject show / program titles (e.g., "Travel Zone", "The Bonsulator").
    if _SHOW_TITLE_WORDS.search(text):
        return False
    # Must match the name pattern.
    return bool(_NAME_RE.match(text))


# Separators that split name from role on a single line.
_SEPARATOR = re.compile(r"\s*[,–—-]+\s*|\s*:\s+")


def _is_role_only_line(text: str) -> bool:
    """Return True when *text* is a pure role label, not a role+name combo.

    A pure role line like "Music Director", "DJ", or "Station Manager &
    Program Director" has only role keywords and connectors (ampersands,
    slashes, colons, etc.).  A role+name line like "DJ Roman Angelos"
    has person-name words outside the role keywords.
    """
    text = text.strip()
    if not text:
        return False
    # Must contain at least one role keyword.
    if not _ROLE_KEYWORDS.search(text):
        return False
    # Strip all role-keyword matches and check whether only connectors /
    # punctuation / whitespace remain.
    remaining = _ROLE_KEYWORDS.sub("", text)
    remaining = re.sub(r"[&/|,;:\s]+", "", remaining)
    remaining = re.sub(r"[-\u2013\u2014()]", "", remaining)
    return not remaining

# Pattern for "Name — Role" or "Name, Role" on one line.
_NAME_ROLE_LINE = re.compile(
    r"^(?P<name>[A-Z][a-z]+(?:[- ][A-Z][a-z]+)*"
    r"(?:\s+(?:de|van|von|del|da|di|le)\s+[A-Z][a-z]+(?:[- ][A-Za-z]+)*)*"
    r"(?:\s+[A-Z][a-z]+(?:[- ][A-Za-z]+)*){0,3})"
    r"\s*[,–—-]+\s*"
    r"(?P<role>.+)$"
)
_ROLE_NAME_LINE = re.compile(
    r"^(?P<role>.+?)\s*:\s+"
    r"(?P<name>[A-Z][a-z]+(?:[- ][A-Z][a-z]+)*"
    r"(?:\s+(?:de|van|von|del|da|di|le)\s+[A-Z][a-z]+(?:[- ][A-Za-z]+)*)*"
    r"(?:\s+[A-Z][a-z]+(?:[- ][A-Za-z]+)*){0,3})$"
)

# Email pattern (same as enrichment.emails but local for proximity checks).
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Conservative phone pattern for proximity checks only (not standalone extraction).
_PHONE_RE = re.compile(
    r"(?:\+?1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]\d{3}[\s.-]\d{4}"
)


# ---------------------------------------------------------------------------
# Structured extraction
# ---------------------------------------------------------------------------

def extract_staff_entries(page: ParsedPage) -> list[dict]:
    """Extract structured person+email entries from a staff-directory page.

    Returns a list of contact-shaped dicts with keys:
    ``name``, ``role``, ``email``, ``phone``, ``source_url``,
    ``confidence_score``, ``provenance``.

    Email is the primary extraction target.  Entries without an email are
    still produced when a named person with a role is found (useful for
    later enrichment), but phone-only entries are never created.  Phone
    numbers are attached only when found alongside an email address.

    The caller merges these with the standard email-based extraction
    (dedup by email).
    """
    text = page.text or ""
    lines = text.split("\n")
    entries: list[dict] = []
    used_names: set[str] = set()

    # Pass 1: line-by-line structured parsing.
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # --- Pattern A: "Name — Role" or "Name, Role" on one line --------
        m = _NAME_ROLE_LINE.match(line)
        if m:
            name = m.group("name").strip()
            role_text = m.group("role").strip()
            entry = _entry_from_name_role(
                name, role_text, page.url, lines, i, used_names)
            if entry:
                entries.append(entry)
            i += 1
            continue

        # --- Pattern B: "Role: Name" on one line -------------------------
        m = _ROLE_NAME_LINE.match(line)
        if m:
            role_text = m.group("role").strip()
            name = m.group("name").strip()
            entry = _entry_from_name_role(
                name, role_text, page.url, lines, i, used_names)
            if entry:
                entries.append(entry)
            i += 1
            continue

        # --- Pattern C: Name on its own line, role + contact below -------
        if _looks_like_person_name(line):
            name = line
            role_text = None
            email = None
            phone = None
            # Scan next lines for role, email, phone — but stop at a
            # blank line (block separator) or another person name (start
            # of the next entry) to prevent borrowing a role from an
            # unrelated entry.
            for j in range(i + 1, min(i + 5, len(lines))):
                next_line = lines[j].strip()
                if not next_line:
                    break
                if _looks_like_person_name(next_line):
                    break
                if role_text is None and _is_role_only_line(next_line):
                    # If the line *after* this role is a person name,
                    # the role belongs to that next person, not the
                    # current one — do not borrow it.
                    if (j + 1 < len(lines)
                            and _looks_like_person_name(
                                lines[j + 1].strip())):
                        break
                    role_text = next_line
                em = _EMAIL_RE.search(next_line)
                if em and email is None:
                    email = normalize_email(em.group(0))
                # Phone: only capture when an email is also found nearby.
                if email and phone is None:
                    ph = _PHONE_RE.search(next_line)
                    if ph:
                        phone = _format_phone(ph.group(0))
            if name.lower() not in used_names:
                entry = _build_entry(
                    name=name,
                    role_text=role_text,
                    email=email,
                    phone=phone,
                    source_url=page.url,
                )
                if entry:
                    entries.append(entry)
                    used_names.add(name.lower())
            i += 1
            continue

        i += 1

    # Pass 2: detect "Role\nName" pairs — a pure role-only line
    # immediately followed by a person name.  This handles the common
    # layout where the role appears *above* the name (e.g., WFMU
    # office-staff section).  Updates existing Pass-1 entries that were
    # created without a role.
    i = 0
    while i < len(lines) - 1:
        role_line = lines[i].strip()
        name_line = lines[i + 1].strip()
        if _is_role_only_line(role_line) and _looks_like_person_name(name_line):
            # Try to update an existing entry that has role=unknown.
            for entry in entries:
                if (entry["name"].lower() == name_line.lower()
                        and entry["role"] == "unknown"):
                    entry["role"] = classify_role(role_line)
                    entry["provenance"].append({
                        "method": "role_label_rule",
                        "value": role_line,
                        "source_url": page.url,
                        "source_type": "official_website_page",
                    })
                    break
            else:
                # Name not yet in entries — create a new one.
                if name_line.lower() not in used_names:
                    entry = _entry_from_name_role(
                        name_line, role_line, page.url, lines,
                        i + 1, used_names)
                    if entry:
                        entries.append(entry)
        i += 1

    return entries


def _entry_from_name_role(
    name: str,
    role_text: str,
    source_url: str,
    lines: list[str],
    line_index: int,
    used_names: set[str],
) -> dict | None:
    """Build one entry from a name+role detected on the same line.

    Scans nearby lines for email (primary) and phone (secondary, only
    when an email is also found) to complete the entry.
    """
    name = name.strip().rstrip(",.")
    if not name or name.lower() in used_names:
        return None
    role = classify_role(role_text)

    email = None
    phone = None
    for j in range(max(0, line_index - 1), min(len(lines), line_index + 4)):
        scan_line = lines[j].strip()
        em = _EMAIL_RE.search(scan_line)
        if em and email is None:
            email = normalize_email(em.group(0))
        # Phone: only capture when an email is also found nearby.
        if email and phone is None:
            ph = _PHONE_RE.search(scan_line)
            if ph:
                phone = _format_phone(ph.group(0))

    entry = _build_entry(
        name=name,
        role_text=role_text,
        email=email,
        phone=phone,
        source_url=source_url,
    )
    if entry:
        used_names.add(name.lower())
    return entry


def _format_phone(raw: str) -> str:
    """Format a matched phone string to (XXX) XXX-XXXX."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] in "01":
        return raw
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


# Roles that justify keeping a name-only entry (no email) — these are
# music-programming-relevant positions worth preserving for future passes.
_MUSIC_RELEVANT_ROLES = {
    "music_director", "program_director", "music_programmer",
    "music_submission", "programming", "music_scheduler",
    "music_coordinator", "host", "dj",
}


def _build_entry(
    name: str,
    role_text: str | None,
    email: str | None,
    phone: str | None,
    source_url: str,
) -> dict | None:
    """Construct a contact-shaped dict with provenance.

    Email is the primary extraction target.  An entry is produced when
    an email is present (with or without a name), or when a named person
    has a music-programming-relevant role.  Name-only entries without
    email AND without a music-relevant role are dropped — they create
    noise without intelligence value.
    """
    if not email and not name:
        return None
    role = classify_role(role_text) if role_text else "unknown"

    # Drop name-only entries that lack both email and music-relevant role.
    if not email and role not in _MUSIC_RELEVANT_ROLES:
        return None

    # Email-based entries score higher; name-only entries are lower value
    # but still useful for future enrichment passes.
    base_score = 0.55 if email else 0.30
    provenance: list[dict] = []
    if name:
        provenance.append({
            "value": name,
            "source_url": source_url,
            "source_type": "official_website_page",
            "method": "staff_directory_extraction",
            "discovered_at": "",
            "also_seen_at": [],
        })
    if role_text:
        provenance.append({
            "value": role_text,
            "source_url": source_url,
            "source_type": "official_website_page",
            "method": "role_label_rule",
            "discovered_at": "",
            "also_seen_at": [],
        })
    if email:
        provenance.append({
            "value": email,
            "source_url": source_url,
            "source_type": "official_website_page",
            "method": "text_rule",
            "discovered_at": "",
            "also_seen_at": [],
        })
    # Phone: secondary supporting data, only when email is present.
    if phone and email:
        provenance.append({
            "value": phone,
            "source_url": source_url,
            "source_type": "official_website_page",
            "method": "phone_rule",
            "discovered_at": "",
            "also_seen_at": [],
        })
    elif phone:
        phone = None  # drop phone when no email — not a qualified contact
    return {
        "id": str(uuid.uuid4()),
        "station_id": None,
        "name": name,
        "role": role,
        "email": email,
        "phone": phone,
        "source_url": source_url,
        "confidence_score": base_score,
        "verified_at": None,
        "provenance": provenance,
    }
