"""Deterministic first-pass contact-role classification.

Transparent ordered keyword rules; first match wins. `unknown` is the honest
default. A future AI enrichment hook may re-classify unknown cases without
changing call sites.
"""

from __future__ import annotations

import re

_EMAIL_IN_LINE = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)

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


def find_role_evidence_near(text: str | None, anchor_index: int) -> dict | None:
    """Locate explicit role-label evidence near *anchor_index* in *text*.

    Phase 4A (evidence-based role attribution): identical search strategy
    as :func:`classify_role_near` (which delegates here), but instead of
    collapsing the result to a bare role token it preserves the traceable
    evidence: the EXACT matched label substring as it appears in the source,
    its absolute character span, and which line (relative to the anchor's
    line) carried it.

    Returns ``None`` when no ROLE_RULES pattern matches any scanned line —
    absence of evidence must never produce attribution.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    if not 0 <= anchor_index < len(text):
        return None
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

    def scan(line_no: int):
        if 0 <= line_no < len(lines):
            raw = lines[line_no][0]
            for pattern, role in _COMPILED:
                match = pattern.search(raw)
                if match:
                    return role, match, line_no
        return None

    def unpack(hit) -> dict:
        role, match, line_no = hit
        _, line_start = lines[line_no]
        return {
            "role": role,
            "matched_label": match.group(0),
            "line_index": line_no,
            "line_offset": line_no - target,
            "char_start": line_start + match.start(),
            "char_end": line_start + match.end(),
        }

    def scan_direction(step: int, limit: int):
        # Scan a single direction. A line that itself carries an email
        # address is the tail of ANOTHER entry ("Role: Name, addr" lines in
        # a staff directory), so no role label beyond it belongs to the
        # contact at *anchor_index* — stop at the first such boundary.
        for k in range(1, limit + 1):
            lineno = target + step * k
            if not 0 <= lineno < len(lines):
                return None
            if _EMAIL_IN_LINE.search(lines[lineno][0]):
                return None
            hit = scan(lineno)
            if hit is not None:
                return unpack(hit)
        return None

    # Same line first, then labels ABOVE the address (station pages place
    # the label before its contact), then below-the-address labels as the
    # last resort — precedence is asymmetric ON PURPOSE.
    own = scan(target)
    if own is not None:
        return unpack(own)
    for hit in (scan_direction(-1, 3), scan_direction(1, 3)):
        if hit is not None:
            return hit
    return None


def classify_role_near_with_evidence(
        text: str | None, anchor_index: int) -> tuple[str, dict | None]:
    """Return ``(role, evidence_dict_or_None)`` for the contact at
    *anchor_index*.

    The role token is exactly what :func:`classify_role_near` returns; the
    second element carries the traceable evidence (see
    :func:`find_role_evidence_near`) or ``None`` when the role is
    ``unknown``.
    """
    evidence = find_role_evidence_near(text, anchor_index)
    if evidence is None:
        return "unknown", None
    return evidence["role"], evidence


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

    Implemented by delegating to :func:`classify_role_near_with_evidence`
    so both entry points can never disagree about attribution.
    """
    role, _ = classify_role_near_with_evidence(text, anchor_index)
    return role


# Context window scanned around an email occurrence when classifying.
CONTEXT_CHARS = 120


def classify_email_context(context_text: str) -> str:
    """Role classification over the text surrounding an address."""
    return classify_role(context_text)
