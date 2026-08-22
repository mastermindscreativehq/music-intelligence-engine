"""Email extraction, normalization, and quality signals.

Rules of engagement:
- only literal text emails and mailto: hrefs are extracted; obfuscation
  mechanisms (Cloudflare email-protection, image text, JS construction) are
  NOT defeated — that would violate the collection policy
- no address is ever invented or guessed
- normalization is deterministic: whitespace, case, surrounding punctuation
- quality is reported as signals; deliverability is NEVER claimed
"""

from __future__ import annotations

import re

EMAIL_CORE = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
EMAIL_RE = re.compile(EMAIL_CORE)
EMAIL_FULL_RE = re.compile(rf"^{EMAIL_CORE}$")

# Zero-width / formatting characters stripped before matching.
_INVISIBLES = re.compile(r"[\u200b\u200c\u200d\ufeff\xa0]")

FREE_PROVIDERS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
    "icloud.com", "live.com", "msn.com", "me.com", "comcast.net",
}

ROLE_INBOXES = {
    "info", "music", "md", "pd", "apd", "programming",
    "musicdirector", "musicdirector@",  # defensive, harmless
    "programdirector", "programdirector2",
    "submissions", "submit", "submission", "musicdepartment",
    "office", "admin", "administrator", "contact", "hello", "general",
    "reception", "news", "frontdesk", "operations", "manager",
}

# Characters stripped from the OUTER ends of a candidate. The dot is
# deliberately excluded: trailing sentence dots are handled separately and
# a LEADING dot must survive to validation so it can be rejected.
_SURROUNDING_PUNCT = " \t\r\n,:;!?\"'`()[]<>{}\u201c\u201d\u2018\u2019"


def extract_emails_from_text(text: str) -> list[str]:
    """Ordered unique raw email candidates found in plain text."""
    if not isinstance(text, str) or not text:
        return []
    cleaned = _INVISIBLES.sub("", text)
    seen: list[str] = []
    for match in EMAIL_RE.findall(cleaned):
        normalized = normalize_email(match)
        if normalized and normalized not in seen:
            seen.append(normalized)
    return seen


def extract_mailto_addresses(mailto_values: list[str]) -> list[str]:
    """Normalize addresses taken from parsed mailto: hrefs."""
    out: list[str] = []
    for raw in mailto_values or []:
        normalized = normalize_email(raw.split("?", 1)[0])
        if normalized and normalized not in out:
            out.append(normalized)
    return out


def normalize_email(raw: str) -> str | None:
    """Return a canonical lowercase address, or None when unusable.

    Strips invisibles/whitespace, surrounding punctuation, and trailing
    sentence dots; validates the shape. Returns None rather than guessing.
    """
    if not isinstance(raw, str):
        return None
    value = _INVISIBLES.sub("", raw).strip().lower()
    value = value.strip(_SURROUNDING_PUNCT)
    while value.endswith("."):
        value = value[:-1].strip(_SURROUNDING_PUNCT)
    # Collapse internal whitespace produced by HTML formatting.
    value = re.sub(r"\s+", "", value)
    if not EMAIL_FULL_RE.match(value):
        return None
    local, domain = value.rsplit("@", 1)
    if local.startswith(".") or local.endswith(".") or ".." in local:
        return None
    if domain.startswith(".") or domain.endswith(".") or ".." in domain:
        return None
    return value


def email_quality(email: str, station_domains: set[str]) -> dict:
    """Quality signals for one normalized address.

    Returns signal flags + a coarse tier. These are discovery-time signals
    only — never a deliverability claim.
    """
    local, _, domain = email.partition("@")
    own_domain = any(
        domain == d or domain.endswith("." + d) for d in station_domains
    )
    free = domain in FREE_PROVIDERS
    role_based = local in ROLE_INBOXES or local.rstrip("0123456789") in ROLE_INBOXES

    signals = ["valid_format"]
    if own_domain:
        signals.append("own_domain")
    else:
        signals.append("domain_mismatch")
        if free:
            signals.append("free_provider")
    if role_based:
        signals.append("role_inbox")

    if own_domain and role_based:
        tier = "professional"
    elif own_domain or role_based:
        tier = "generic"
    elif free:
        tier = "weak"
    else:
        tier = "unclassified"
    return {
        "signals": signals,
        "tier": tier,
        "inbox_kind": "role_based" if role_based else "personal_or_other",
        "domain": domain,
        "matches_station_domain": own_domain,
        "free_provider": free,
    }
