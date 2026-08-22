"""Deterministic first-pass contact-role classification.

Transparent ordered keyword rules; first match wins. `unknown` is the honest
default. A future AI enrichment hook may re-classify unknown cases without
changing call sites.
"""

from __future__ import annotations

import re

ROLE_RULES: list[tuple[str, str]] = [
    (r"music\s*director", "music_director"),
    (r"assistant\s*music\s*director", "music_director"),
    (r"program\s*director", "program_director"),
    (r"programming\s*director", "programming"),
    (r"director\s*of\s*programming", "programming"),
    (r"music\s*(programmer|scheduler|coordinator)", "music_programmer"),
    (r"(music|demo|song|track)s?\s*(submissions?|to:)", "music_submission"),
    (r"submit\s+(your\s+)?(music|tracks|demos|songs)", "music_submission"),
    (r"send\s+us\s+your\s+music", "music_submission"),
    (r"station\s*manager", "station_manager"),
    (r"general\s*manager", "station_manager"),
    # Intent labels (bookings / press) must outrank generic on-air titles:
    # title words like "host"/"DJ" drift all over station copy and would
    # otherwise shadow evidence that sits right next to the contact.
    (r"media\s*(contact|inquiries|enquiries|relations)|press\s*(contact|inquiries|kit)|public\s*relations", "media"),
    (r"bookings?\b|event\s*requests?", "booking"),
    (r"\bproducer\b", "producer"),
    (r"(radio\s+)?host\b|presenter\b|on[- ]air\s*(host|personality)", "host"),
    (r"(\bdj\b|\bdjs\b)", "dj"),
    (r"general\s*(inquiries|enquiries|questions)", "general"),
    (r"advert(ising|isements?|ertise)?|underwriting|sponsorships?", "advertising"),
]

# Full Phase 2+3 role vocabulary (stable snake_case tokens).
ROLE_VOCABULARY = sorted({
    role for _, role in ROLE_RULES
} | {"unknown", "other"})

_COMPILED = [(re.compile(pattern, re.I), role) for pattern, role in ROLE_RULES]


def classify_role(text: str | None) -> str:
    """Return a role constant for *text*, or 'unknown'."""
    if not isinstance(text, str) or not text.strip():
        return "unknown"
    for pattern, role in _COMPILED:
        if pattern.search(text):
            return role
    return "unknown"


def classify_role_near(text: str | None, anchor_index: int) -> str:
    """Role evidence for the contact at *anchor_index*, searched line-wise.

    Whole-window classification fails on dense pages: one contact's window
    inevitably contains neighbors' labels, and pure nearest-match distance
    is wrong too — station pages place a label BEFORE its address
    ("Music Director: …\nReach the music department at md@…"), while the
    text AFTER an address usually belongs to the next contact.

    Strategy: search the anchor's own line first (within a line, ROLE_RULES
    priority order applies), then the preceding lines (−1, −2, −3) and only
    then the following lines (+1 … +3). Precedence is asymmetric ON PURPOSE:
    parsed HTML puts each link/address on its own text line, station pages
    place a role label BEFORE its address, while text AFTER an address
    usually belongs to the NEXT contact. No evidence within the window
    → 'unknown', the honest default.
    """
    if not isinstance(text, str) or not text.strip():
        return "unknown"
    if not 0 <= anchor_index < len(text):
        return "unknown"
    lines: list[tuple[str, int]] = []
    pos = 0
    for raw in text.split("\n"):
        lines.append((raw, pos))
        pos += len(raw) + 1
    target = len(lines) - 1
    for index, (raw, start) in enumerate(lines):
        if start <= anchor_index < start + len(raw) + 1:
            target = index
            break

    def first_role_on(line_no: int) -> str | None:
        if 0 <= line_no < len(lines):
            raw = lines[line_no][0]
            for pattern, role in _COMPILED:
                if pattern.search(raw):
                    return role
        return None

    if (hit := first_role_on(target)) is not None:
        return hit
    for offset in (1, 2, 3):          # labels live ABOVE the address
        if (hit := first_role_on(target - offset)) is not None:
            return hit
    for offset in (1, 2, 3):          # below-the-address labels: last resort
        if (hit := first_role_on(target + offset)) is not None:
            return hit
    return "unknown"


# Context window scanned around an email occurrence when classifying.
CONTEXT_CHARS = 120


def classify_email_context(context_text: str) -> str:
    """Role classification over the text surrounding an address."""
    return classify_role(context_text)
