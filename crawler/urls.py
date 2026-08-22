"""URL normalization and canonical domain identity.

Deterministic, side-effect free utilities shared by the crawler (fetch
deduplication) and enrichment (station deduplication).

Limitations (documented in docs/radio-discovery.md):
- registrable-domain approximation uses a small multi-part suffix list,
  not the full Public Suffix List
- no IDN/punycode handling
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

ALLOWED_SCHEMES = ("http", "https")

# Query parameters that are pure tracking noise. Everything else is preserved.
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid",
    "igshid", "si", "ref_src", "ref_url", "ref", "spm", "scm",
}

# Multi-part public suffixes we collapse when approximating a registrable
# domain. Deliberately minimal; not a substitute for the PSL.
MULTI_PART_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk",
    "com.au", "net.au", "org.au",
    "co.nz", "net.nz", "org.nz",
    "com.br", "com.mx", "com.ar",
    "co.jp", "ne.jp", "or.jp",
    "com.cn", "org.cn",
    "co.in", "co.za", "com.sg", "com.tr", "com.tw",
}


class InvalidUrlError(ValueError):
    """Raised for URLs that are unusable or unsafe to fetch."""


def normalize_url(raw_url: str) -> str:
    """Return a canonical form of *raw_url*.

    - scheme must be http/https (anything else raises InvalidUrlError)
    - host lowercased; leading ``www.`` stripped
    - fragment dropped
    - tracking query parameters dropped; remaining params kept and sorted
      (meaningful parameters are never blindly destroyed)
    - trailing slash removed except at path root
    """
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise InvalidUrlError("empty URL")
    candidate = raw_url.strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", candidate):
        # Bare domains like "station.org/contact" are treated as https.
        candidate = "https://" + candidate
    parts = urlsplit(candidate)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise InvalidUrlError(f"unsupported scheme: {parts.scheme!r}")
    host = (parts.hostname or "").lower()
    if not host or "." not in host and host != "localhost":
        raise InvalidUrlError(f"missing or invalid host in {raw_url!r}")
    display_host = host[4:] if host.startswith("www.") else host
    if parts.port is not None:
        display_host = f"{display_host}:{parts.port}"
    path = parts.path or ""
    if len(path) > 1:
        path = path.rstrip("/")
        path = path if path else "/"
    query_pairs = [
        pair for pair in parts.query.split("&") if pair
    ]
    kept = []
    for pair in query_pairs:
        key = pair.split("=", 1)[0].lower()
        if key not in TRACKING_PARAMS:
            kept.append(pair)
    kept.sort()
    return urlunsplit((parts.scheme.lower(), display_host, path, "&".join(kept), ""))


def canonical_host(url_or_host: str) -> str:
    """Lowercased hostname without ``www.``, port, scheme, or path."""
    value = url_or_host.strip().lower()
    if "://" in value:
        value = urlsplit(value).hostname or ""
    else:
        value = value.split("/", 1)[0]
    value = value.rsplit("@", 1)[-1]          # drop userinfo if present
    value = value.split(":", 1)[0]            # drop port
    if value.startswith("www."):
        value = value[4:]
    return value


def canonical_domain(url_or_host: str) -> str:
    """Approximate the registrable domain of a URL or hostname.

    ``www.kqxr.org`` -> ``kqxr.org``; ``shop.bbc.co.uk`` -> ``bbc.co.uk``.
    """
    host = canonical_host(url_or_host)
    labels = [label for label in host.split(".") if label]
    if len(labels) >= 3:
        last_two = ".".join(labels[-2:])
        last_three = ".".join(labels[-3:])
        if last_two in MULTI_PART_SUFFIXES:
            return last_three
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def same_site(url_a: str, url_b: str) -> bool:
    """True when both URLs resolve to the same canonical domain."""
    try:
        return canonical_domain(url_a) == canonical_domain(url_b)
    except (InvalidUrlError, ValueError):
        return False


def slugify_name(name: str) -> str:
    """Stable slug used as part of the no-domain dedup fallback key."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower())
    return slug.strip("-")
