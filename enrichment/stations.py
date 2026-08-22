"""Station-type classification from observable public-page evidence.

Evidence-keyword counting over combined page text. Honest output: when
evidence is absent the answer is `unknown` with low confidence — never a
guessed type dressed up as certainty.

Also provides conservative social-link detection (recorded only; those
platforms are NOT scraped).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Ordered by tie-break priority (earlier wins on equal evidence counts).
STATION_TYPE_KEYWORDS: list[tuple[str, list[str]]] = [
    ("college", ["college radio", "college station", "student radio",
                 "student-run"]),
    ("university", ["university radio", "university station"]),
    ("campus", ["campus radio", "campus station", "campus and community"]),
    ("community", ["community radio", "community station",
                   "listener-supported", "non-commercial community"]),
    ("public", ["public radio", "npr", "npr member", "pacifica"]),
    ("independent", ["independent radio", "indie radio",
                     "independently owned and operated",
                     "independently operated"]),
    ("internet", ["internet radio", "online radio", "web radio",
                  "internet-only"]),
]

SOCIAL_PATTERNS: dict[str, re.Pattern[str]] = {
    "facebook": re.compile(r"https?://(?:www\.|[a-z]{2}-[a-z]{2}\.)?facebook\.com/"
                           r"[A-Za-z0-9_.\-]+/?", re.I),
    "instagram": re.compile(r"https?://(?:www\.)?instagram\.com/"
                            r"[A-Za-z0-9_.\-]+/?", re.I),
    "x": re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/"
                    r"[A-Za-z0-9_]{1,15}/?", re.I),
    "youtube": re.compile(r"https?://(?:www\.)?youtube\.com/"
                          r"(?:@|c/|channel/|user/)[A-Za-z0-9_.\-]+/?", re.I),
    "linkedin": re.compile(r"https?://(?:[a-z]{2}\.)?(?:www\.)?linkedin\.com/"
                           r"(?:company|in)/[A-Za-z0-9_.\-]+/?", re.I),
}


@dataclass
class StationClassification:
    station_type: str = "unknown"
    confidence: float = 0.1
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "station_type": self.station_type,
            "classification_confidence": round(self.confidence, 2),
            "classification_evidence": list(self.evidence),
        }


def classify_station(texts: list[str]) -> StationClassification:
    """Classify by keyword evidence across all collected page text."""
    combined = "\n".join(t for t in texts if isinstance(t, str)).lower()
    counts: dict[str, tuple[int, list[str]]] = {}
    for order, (stype, keywords) in enumerate(STATION_TYPE_KEYWORDS):
        hits: list[str] = []
        for kw in keywords:
            found = combined.count(kw)
            if found:
                hits.extend([kw] * min(found, 3))
        if hits:
            counts[stype] = (len(hits), hits)
    if not counts:
        return StationClassification()
    best_type = max(
        counts.items(),
        key=lambda item: (item[1][0], -list(dict(STATION_TYPE_KEYWORDS)).index(item[0])),
    )
    stype, (count, evidence_hits) = best_type
    confidence = min(0.4 + 0.15 * count, 0.9)
    return StationClassification(stype, confidence, evidence_hits[:10])


def detect_social_urls(links: list[str]) -> dict[str, str]:
    """First matching URL per platform from official-page links."""
    out: dict[str, str] = {}
    for url in links or []:
        if not isinstance(url, str):
            continue
        for platform, pattern in SOCIAL_PATTERNS.items():
            if platform in out:
                continue
            match = pattern.search(url)
            if match:
                out[platform] = match.group(0).rstrip("/")
    return out


PAGE_ROLE_MARKERS: dict[str, re.Pattern[str]] = {
    "contact_page": re.compile(r"contact", re.I),
    "submission_page": re.compile(r"submi(?:ssion|t)", re.I),
    "programming_page": re.compile(r"program|shows|schedule", re.I),
}


def page_role_from_url(url: str) -> str | None:
    """Coarse source-type marker for a page based on its URL path."""
    path = url.lower()
    for role, pattern in PAGE_ROLE_MARKERS.items():
        if pattern.search(path):
            return role
    return None
