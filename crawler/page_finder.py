"""Focused contact/submission page discovery for a station website.

Not a spider: from the homepage we rank same-site links by keyword evidence
and optionally guess a tiny set of conventional paths, always capped by the
per-site page budget.
"""

from __future__ import annotations

import re

from crawler.pages import ParsedPage
from crawler.urls import canonical_domain, normalize_url

# (pattern over path+anchor text, weight) — higher weight wins.
KEYWORD_HINTS: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"music[-_]?submissions?", re.I), 10),
    (re.compile(r"submit([-_]?music|[-_]?your[-_]?music)?", re.I), 10),
    (re.compile(r"submission", re.I), 9),
    (re.compile(r"program[-_]?director", re.I), 8),
    (re.compile(r"contact([-_]?us)?", re.I), 8),
    (re.compile(r"\bcontact\b", re.I), 7),
    (re.compile(r"playlist|airplay", re.I), 6),
    (re.compile(r"programming", re.I), 6),
    (re.compile(r"\bmusic\b", re.I), 5),
    (re.compile(r"\bstaff\b|\bteam\b|\bpeople\b", re.I), 5),
    (re.compile(r"\bdj?s?\b|on[-_]?air personalities|hosts?", re.I), 4),
    (re.compile(r"advert(ise|ising)", re.I), 3),
    (re.compile(r"\babout\b", re.I), 2),
    (re.compile(r"\bshows?\b|\bschedule\b", re.I), 1),
]

# Conventional paths guessed only when the homepage reveals nothing better.
GUESSED_PATHS = ["/contact", "/contact-us", "/submissions"]


def score_link(url: str, anchor_text: str) -> int:
    """Keyword evidence score for a link; 0 means uninteresting."""
    haystack = f"{url} {anchor_text}"
    best = 0
    for pattern, weight in KEYWORD_HINTS:
        if pattern.search(haystack):
            best = max(best, weight)
    return best


def select_priority_pages(
    homepage_url: str,
    homepage: ParsedPage,
    budget: int,
) -> list[str]:
    """Return up to *budget* normalized same-site URLs worth fetching.

    Ranked by keyword weight (descending), then discovery order. The
    homepage itself is never included. Off-site links are ignored.
    """
    site = canonical_domain(homepage_url)
    scored: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    order = 0
    try:
        seen.add(normalize_url(homepage_url))
    except ValueError:
        pass
    for link in homepage.links:
        url = link.href_absolute
        if not url.lower().startswith(("http://", "https://")):
            continue
        if canonical_domain(url) != site:
            continue
        try:
            normalized = normalize_url(url)
        except ValueError:
            continue
        if normalized in seen:
            continue
        weight = score_link(normalized, link.anchor_text)
        if weight <= 0:
            continue
        seen.add(normalized)
        scored.append((-weight, order, normalized))
        order += 1
    scored.sort()
    ranked = [url for _, _, url in scored]

    # Ensure at least one staff/team/people page is included when any
    # exist on the site — these are high-value for person/email enrichment
    # and may rank below submission/contact pages in the weight sort.
    _STAFF_RE = re.compile(r"\bstaff\b|\bteam\b|\bpeople\b", re.I)
    has_staff = any(_STAFF_RE.search(u) for u in ranked)
    if not has_staff and scored:
        # Find the highest-weight staff page that didn't make the cut.
        for _, _, url in scored:
            if _STAFF_RE.search(url):
                # Insert it as the last entry (lowest priority within budget).
                if len(ranked) >= budget:
                    ranked[-1] = url  # replace the lowest-priority entry
                else:
                    ranked.append(url)
                break

    remaining = budget - len(ranked)
    if remaining > 0:
        base = homepage_url.rstrip("/")
        for path in GUESSED_PATHS:
            candidate = base + path
            try:
                normalized = normalize_url(candidate)
            except ValueError:
                continue
            if normalized not in seen and canonical_domain(candidate) == site:
                seen.add(normalized)
                ranked.append(normalized)
                remaining -= 1
                if remaining <= 0:
                    break
    return ranked[:budget]
