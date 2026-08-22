"""Submission-path intelligence.

Identifies HOW artists can submit music, strictly from evidence present in
already-collected pages:

- instructions snippet (sentence-level capture around submission cues)
- restrictions ("no attachments", "mp3 only", ...)
- method inference (email / web_form / postal) — always labeled as
  INFERENCE with reasons; never presented as fact

Nothing here invents a submission channel: no page evidence → no path.
"""

from __future__ import annotations

import re

_INSTRUCTION_CUES = re.compile(
    r"(submit|send|mail|email|attach|upload|post)\s+(?:your\s+)?"
    r"(music|demos?|tracks?|songs?|cds?|mp3s?|press\s+kits?|bio)|"
    r"(music|demo|track)\s+submissions?",
    re.I,
)

_RESTRICTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "no_attachments": re.compile(r"no\s+(?:email\s+)?attachments?", re.I),
    "digital_only": re.compile(r"(mp3|digital|links?)\s+only|only\s+(?:accept|via)\s+(?:mp3|digital|links?)", re.I),
    "no_phone_calls": re.compile(r"no\s+phone\s+calls?", re.I),
    "postal_only": re.compile(r"(postal|physical)\s+(mail|copies)\s+only|by\s+mail\s+only", re.I),
    "no_drop_ins": re.compile(r"no\s+drop[- ]ins?", re.I),
    "review_window": re.compile(r"(allow|please allow|within)\s+\S*\s*(weeks?|days?)\s*(?:for|to)\s+(?:a\s+)?review", re.I),
}

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_MAX_INSTRUCTION_CHARS = 400


def extract_submission_instructions(text: str | None) -> str | None:
    """Sentences containing explicit submission cues, bounded to ~400 chars."""
    if not isinstance(text, str) or not text.strip():
        return None
    sentences = _SENTENCE_SPLIT.split(text)
    picked: list[str] = []
    total = 0
    for sentence in sentences:
        if _INSTRUCTION_CUES.search(sentence):
            snippet = " ".join(sentence.split())
            if total + len(snippet) + 1 > _MAX_INSTRUCTION_CHARS:
                break
            picked.append(snippet)
            total += len(snippet) + 1
        if len(picked) >= 5:
            break
    return "\n".join(picked) if picked else None


def detect_restrictions(text: str | None) -> list[dict]:
    """Restriction signals found verbatim on the page."""
    if not isinstance(text, str) or not text:
        return []
    out: list[dict] = []
    for token, pattern in _RESTRICTION_PATTERNS.items():
        match = pattern.search(text)
        if match:
            out.append({
                "restriction": token,
                "evidence_text": " ".join(match.group(0).split()),
            })
    return out


def infer_submission_methods(
    has_form: bool,
    submission_email: str | None,
    texts: list[str],
) -> dict:
    """Evidence-based INFERENCE of submission channels.

    Returns {methods: [...], confidence, reasons}. Every method is justified;
    an empty method set is the honest outcome when nothing supports one.
    """
    methods: list[str] = []
    reasons: list[str] = []

    if submission_email:
        methods.append("email")
        reasons.append(f"submission email publicly listed ({submission_email})")
    if has_form:
        methods.append("web_form")
        reasons.append("on-page submission form detected in HTML")
    combined = "\n".join(t or "" for t in texts).lower()
    if re.search(r"(mail|mailing address|post)\b.{0,60}"
                 r"\b(cd|demo|package|usb|address)", combined) \
            and "postal_only" in {r["restriction"] for r in detect_restrictions(combined)}:
        methods.append("postal")
        reasons.append("page indicates postal submissions")

    confidence = round(min(0.3 + 0.3 * len(methods), 0.9), 2) if methods else 0.0
    if not methods:
        reasons.append("no publicly evidenced submission channel found")
    return {
        "methods": methods,
        "confidence": confidence,
        "reasons": reasons,
        "kind": "inference",
    }
