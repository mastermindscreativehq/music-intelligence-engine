"""Contact assembly and normalization.

Builds ContactRecord-shaped dictionaries from page evidence:
- emails with surrounding-context role classification
- conservative name extraction near role keywords (rule-derived, low weight)
- phone numbers from tel: hrefs and simple text patterns

An email is never assumed to belong to a named person; unnamed role contacts
are normal output.
"""

from __future__ import annotations

import re
import uuid

from crawler.pages import ParsedPage
from enrichment.emails import extract_emails_from_text, normalize_email
from enrichment.roles import CONTEXT_CHARS, classify_role, classify_role_near

NAME_PATTERN = re.compile(
    r"\b([A-Z][a-z]+(?:'[A-Za-z]+)?(?:\s+[A-Z][a-z']+){1,3})\b"
)

# "Role: Name" / "Name — Role" / "Name, Role"
_ROLE_WORDS = (
    r"(?:assistant\s+)?music\s*director|program\s*director|"
    r"programming\s*director|director\s*of\s*programming|station\s*manager|"
    r"general\s*manager|\bdj\b|on[- ]air\s*(?:host|personality)|host"
)
# Role words match case-insensitively via scoped (?i:...); the NAME capture
# stays case-sensitive so proper-noun capitalization remains a filter.
NAME_BEFORE_ROLE = re.compile(
    rf"\b([A-Z][a-z']+(?:\s+[A-Z][a-z']+)+)\s*[,–—-]\s*((?i:{_ROLE_WORDS}))\b",
)
NAME_AFTER_COLON = re.compile(
    # Continuation words must stay on the SAME LINE (horizontal whitespace
    # only) — otherwise a colon-name followed by a newline + sentence grabs
    # the next sentence's first word into the person's name.
    rf"((?i:{_ROLE_WORDS}))\s*:\s*"
    rf"([A-Z][a-z']+(?:[ \t]+[A-Z][a-z']+){{0,3}})",
)


def extract_phone_numbers(text: str) -> list[str]:
    """Conservative NANP-style extraction from text (no guessing)."""
    if not isinstance(text, str) or not text:
        return []
    pattern = re.compile(
        r"(?:\+?1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]\d{3}[\s.-]\d{4}"
    )
    out: list[str] = []
    for match in pattern.findall(text):
        digits = re.sub(r"\D", "", match)
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        # Minimal NANP sanity: area code must not start with 0/1. Exchange
        # restrictions (e.g., 555-12xx fictional ranges) are NOT enforced —
        # over-validation silently drops real numbers.
        if len(digits) != 10 or digits[0] in "01":
            continue
        formatted = f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
        if formatted not in out:
            out.append(formatted)
    return out


def _context_around(text: str, index: int) -> str:
    start = max(0, index - CONTEXT_CHARS)
    end = min(len(text), index + CONTEXT_CHARS)
    return text[start:end]


def extract_contact_names(text: str) -> list[tuple[str, str]]:
    """Return (name, role_guess) pairs found via adjacency patterns."""
    results: list[tuple[str, str]] = []

    def clean(name: str) -> str | None:
        name = " ".join(name.strip(" .,-").split())
        # Reject obvious non-person matches (e.g., "The Station", ALLCAPS).
        if not 2 <= len(name.split()) <= 4:
            return None
        if any(word.isupper() and len(word) > 3 for word in name.split()):
            return None
        return name

    for match in NAME_AFTER_COLON.finditer(text):
        name = clean(match.group(2))
        if name:
            results.append((name, classify_role(match.group(1))))
    for match in NAME_BEFORE_ROLE.finditer(text):
        name = clean(match.group(1))
        if name:
            results.append((name, classify_role(match.group(2))))
    deduped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, role in results:
        key = name.lower()
        if key not in seen:
            seen.add(key)
            deduped.append((name, role))
    return deduped


def build_contacts_from_page(page: ParsedPage) -> list[dict]:
    """Assemble contact dicts from one parsed page.

    Shape mirrors discovery.radio.schema.ContactRecord.to_dict() minus
    station_id (assigned by the pipeline).
    """
    contacts: dict[str, dict] = {}   # keyed by email when present

    body_text = page.text or ""
    for email in extract_emails_from_text(body_text):
        idx = body_text.lower().find(email)
        context = _context_around(body_text, idx) if idx >= 0 else ""
        # Proximity-aware: the nearest role evidence decides, so a
        # neighboring contact's label can't hijack this one.
        role = (classify_role_near(body_text, idx) if idx >= 0
                else classify_role(""))
        contacts[email] = {
            "id": str(uuid.uuid4()),
            "station_id": None,
            "name": None,
            "role": role,
            "email": email,
            "phone": None,
            "source_url": page.url,
            "confidence_score": 0.3,
            "verified_at": None,
            "provenance": [{
                "value": email,
                "source_url": page.url,
                "source_type": "official_website_page",
                "method": "text_rule",
                "discovered_at": "",
                "also_seen_at": [],
            }],
        }

    for mailto in page.mailtos:
        email = normalize_email(mailto)
        if email and email in contacts:
            continue
        if email:
            # Address not present in visible text: fall back to whole-page
            # role evidence (conservative; still rule-based).
            contacts[email] = {
                "id": str(uuid.uuid4()),
                "station_id": None,
                "name": None,
                "role": classify_role(page.text or ""),
                "email": email,
                "phone": None,
                "source_url": page.url,
                "confidence_score": 0.25,
                "verified_at": None,
                "provenance": [{
                    "value": email,
                    "source_url": page.url,
                    "source_type": "official_website_mailto",
                    "method": "mailto_rule",
                    "discovered_at": "",
                    "also_seen_at": [],
                }],
            }

    # Attach names to matching-role email contacts where evidence aligns.
    names = extract_contact_names(body_text)
    used_names: set[str] = set()
    for name, role in names:
        for contact in contacts.values():
            if contact["name"] is None and contact["role"] == role \
                    and name.lower() not in used_names:
                contact["name"] = name
                contact["provenance"].append({
                    "value": name,
                    "source_url": page.url,
                    "source_type": "official_website_page",
                    "method": "name_adjacency_rule",
                    "discovered_at": "",
                    "also_seen_at": [],
                })
                used_names.add(name.lower())
                break

    # Phones become standalone contacts only if no email exists yet on page.
    phones = extract_phone_numbers(body_text)
    for phone in phones:
        already = any(c["phone"] == phone for c in contacts.values())
        if not already and not contacts:
            contacts[f"phone:{phone}"] = {
                "id": str(uuid.uuid4()),
                "station_id": None,
                "name": None,
                "role": "unknown",
                "email": None,
                "phone": phone,
                "source_url": page.url,
                "confidence_score": 0.2,
                "verified_at": None,
                "provenance": [{
                    "value": phone,
                    "source_url": page.url,
                    "source_type": "official_website_page",
                    "method": "phone_rule",
                    "discovered_at": "",
                    "also_seen_at": [],
                }],
            }
    return list(contacts.values())
