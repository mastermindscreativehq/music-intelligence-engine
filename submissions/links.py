"""Submission/reference link targets and accessibility guards (Phase 8).

"Accessible submission/reference links" (roadmap Phase 8) means the
operator can see whether the submission path a station ADVERTISES is
actually reachable — before Phase 9 ever composes a message.

Design rules:

- Targets come ONLY from stored evidence: the ``submission_url`` Fact and
  the ``instructions`` source page of a station's SubmissionPath. Nothing
  is guessed, discovered, or normalized beyond scheme filtering.
- Checks are bounded and polite: they run through the crawler's
  ``StdlibHttpFetcher`` (robots.txt, per-host rate limiting, timeouts,
  response-size cap) — composition, not reimplementation.
- SSRF guard: before any network call, the target host must resolve to
  public unicast addresses. Loopback/private/link-local/reserved ranges
  are refused WITHOUT contacting them, and the refusal is recorded as an
  ordinary check row so the audit trail stays complete.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

ALLOWED_SCHEMES = ("http", "https")


class SsrfBlocked(ValueError):
    """Target host resolves to a non-public address; never contacted."""


def extract_check_targets(submission):
    """Ordered, de-duplicated [(url, target_kind)] from a SubmissionPath dict.

    Kinds: ``submission_url`` (the Fact value) and ``instructions_page``
    (the instructions Fact's source page). Non-http(s) entries are skipped.
    """
    if not isinstance(submission, dict):
        return []
    candidates = []
    url_fact = submission.get("submission_url")
    if isinstance(url_fact, dict) and url_fact.get("value"):
        candidates.append((url_fact["value"], "submission_url"))
    instructions = submission.get("instructions")
    if isinstance(instructions, dict) and instructions.get("source_url"):
        candidates.append(
            (instructions["source_url"], "instructions_page"))
    seen, targets = set(), []
    for url, kind in candidates:
        if not isinstance(url, str):
            continue
        parts = urlsplit(url.strip())
        if parts.scheme.lower() not in ALLOWED_SCHEMES or not parts.hostname:
            continue
        normalized = url.strip()
        if (normalized, kind) in seen:
            continue
        seen.add((normalized, kind))
        targets.append((normalized, kind))
    return targets


def _addresses_for(host, resolve=None):
    """All IP addresses *host* resolves to (IP literals pass straight
    through). ``resolve`` is injectable for tests."""
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass
    resolver = resolve or socket.getaddrinfo
    infos = resolver(host, None)
    return [info[4][0] for info in infos]


def assert_public_url(url, resolve=None):
    """Raise :class:`SsrfBlocked` unless every resolved address is public.

    Blocks loopback, private, link-local, reserved, multicast, unspecified,
    and carrier-grade NAT (100.64/10) ranges for both IP versions.
    """
    host = (urlsplit(url).hostname or "").strip().strip("[]").lower()
    if not host:
        raise SsrfBlocked(f"URL has no resolvable host: {url!r}")
    try:
        addresses = _addresses_for(host, resolve=resolve)
    except OSError as exc:
        raise SsrfBlocked(f"DNS resolution failed for {host!r}: {exc}") \
            from None
    for address_text in addresses:
        address = ipaddress.ip_address(address_text.split("%")[0])
        cgnat = (address.version == 4
                 and address in ipaddress.ip_network("100.64.0.0/10"))
        if (address.is_loopback or address.is_private or address.is_link_local
                or address.is_reserved or address.is_multicast
                or address.is_unspecified or cgnat):
            raise SsrfBlocked(
                f"{host!r} resolves to a non-public address "
                f"({address}); refusing to contact it")
