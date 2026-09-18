"""Deterministic station-candidate qualification.

Search engines return many non-station pages for a station query: social
profiles, streaming/aggregator pages, news articles, event/ticket listings,
lyrics/playlist pages, job boards, and shop/product pages. Fetching all of
them wastes budget and lets non-station content pollute station profiles.

This module classifies a raw ``Candidate`` BEFORE any fetch using only its
URL, title, and snippet — deterministic rules, no network, no model, no
station-specific hardcoding. Verdicts:

- ``qualified``    — clear station signals (callsign, band/frequency,
                     "radio"/"broadcast"/"community radio", etc.).
- ``needs_review`` — no clear signal either way; the engine still processes
                     it (unknown is not the same as disqualified).
- ``rejected``     — a non-station destination (social/streaming/aggregator
                     host, or an article/event/lyrics/jobs/shop path). These
                     are dropped before fetching.

Rules are intentionally conservative: only unambiguous non-station evidence
rejects a candidate, so unfamiliar but legitimate stations survive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from crawler.urls import canonical_domain

QUALIFIED = "qualified"
NEEDS_REVIEW = "needs_review"
REJECTED = "rejected"

# Site hosts that are never an official station site: social platforms,
# streaming/aggregators, encyclopedias, ticket/event marketplaces, job
# boards, and storefronts. Matched on the registrable domain (suffix match),
# so ``en.wikipedia.org`` and ``music.apple.com`` are covered.
_PLATFORM_HOSTS = frozenset({
    "facebook.com", "instagram.com", "twitter.com", "x.com", "tiktok.com",
    "linkedin.com", "pinterest.com", "reddit.com", "tumblr.com",
    "youtube.com", "youtu.be", "vimeo.com", "twitch.tv",
    "wikipedia.org", "wikimedia.org", "wikidata.org", "fandom.com",
    "spotify.com", "soundcloud.com", "mixcloud.com", "bandcamp.com",
    "apple.com", "deezer.com", "pandora.com", "audacy.com",
    "discogs.com", "last.fm", "allmusic.com", "genius.com",
    "azlyrics.com", "lyrics.com", "songfacts.com",
    "amazon.com", "ebay.com", "etsy.com", "yelp.com", "tripadvisor.com",
    "eventbrite.com", "ticketmaster.com", "stubhub.com", "songkick.com",
    "bandsintown.com", "seatgeek.com", "dice.fm",
    "tunein.com", "streema.com", "radio.com", "iheart.com",
    "radio-garden.com", "onlineradiobox.com", "mytuner-radio.com",
    "radios.com", "radio-uk.co.uk", "radioline.co",
    "indeed.com", "glassdoor.com", "ziprecruiter.com", "monster.com",
    "linktr.ee", "patreon.com", "gofundme.com", "change.org",
})

# Path roots that indicate a per-item (article/event/lyrics/product/job)
# page rather than an organization entry point.
_REJECT_PATH_RE = re.compile(
    r"^/(?:"
    r"playlist|playlists|song|songs|lyrics|album|albums|artist|artists|"
    r"news|article|articles|story|stories|press-release|"
    r"event|events|tickets?|tour|concerts?|"
    r"jobs?|careers?|vacanc|"
    r"wiki|shop|store|cart|checkout|product|products|"
    r"watch|video|videos|"
    r"tag|tags|category|categories|author|authors|"
    r"review|reviews|top-?\d+"
    r")(?:/|$)",
    re.I,
)

# Title/snippet phrases that unambiguously describe a non-station page.
_REJECT_TEXT_RE = re.compile(
    r"\b("
    r"top\s+\d+\s+songs?|playlist|lyrics|album review|"
    r"concert tickets?|tour dates|buy tickets|"
    r"wikipedia|"
    r"now hiring|jobs? in|job opening|"
    r"for sale|buy now|add to cart|"
    r"listen online free|online radio stations?\s+(?:list|directory)|"
    r"radio station jobs?|"
    r"download mp3|free download"
    r")\b",
    re.I,
)

# Positive station signals.
_CALLSIGN_RE = re.compile(r"\b(?:W|K|C|X)[A-Z]{2,4}\b")
_FREQ_RE = re.compile(
    r"\b\d{2,3}(?:\.\d{1,2})?\s?(?:fm|am|khz|mhz)\b", re.I)
_STATION_WORD_RE = re.compile(
    r"\b("
    r"radio|radio station|broadcast(?:ing)?|"
    r"community radio|public radio|college radio|campus radio|"
    r"pirate radio|low[- ]power|airwaves|on air|"
    r"fm|am"
    r")\b",
    re.I,
)


@dataclass
class CandidateVerdict:
    """Deterministic qualification outcome for one raw candidate."""

    verdict: str
    kind: str = "unknown"
    reason: str = ""
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "kind": self.kind,
            "reason": self.reason,
            "evidence": list(self.evidence),
        }


def _host(url: str) -> str:
    try:
        return canonical_domain(url)
    except (ValueError, TypeError):
        return ""


def _path(url: str) -> str:
    try:
        return urlsplit(url).path or "/"
    except (ValueError, TypeError):
        return "/"


def _platform_host(host: str) -> str | None:
    for platform in _PLATFORM_HOSTS:
        if host == platform or host.endswith("." + platform):
            return platform
    return None


def classify_station_candidate(
    *, url: str, title: str = "", snippet: str = "",
) -> CandidateVerdict:
    """Classify one candidate; never fetches, never guesses identity."""
    host = _host(url)
    platform = _platform_host(host) if host else None
    if platform is not None:
        return CandidateVerdict(
            REJECTED, "platform_page",
            f"host {host!r} is a {platform} page, not a station site",
            [f"host={host}"])

    if _REJECT_PATH_RE.match(_path(url)):
        return CandidateVerdict(
            REJECTED, "non_station_path",
            f"path {_path(url)!r} is a per-item/non-station page",
            [f"path={_path(url)}"])

    text = " ".join(f"{title or ''} {snippet or ''}".split())
    evidence: list[str] = []
    if _STATION_WORD_RE.search(text):
        evidence.append("station_keyword")
    if _CALLSIGN_RE.search(text):
        evidence.append("callsign")
    if _FREQ_RE.search(text):
        evidence.append("frequency")

    # A non-station title phrase rejects ONLY when no station signal is
    # present, so a real station's "Playlists & Shows" home page is never
    # thrown away on a single ambiguous word.
    match = _REJECT_TEXT_RE.search(text)
    if match and not evidence:
        return CandidateVerdict(
            REJECTED, "non_station_title",
            f"title/snippet describes a non-station page: {match.group(0)!r}",
            [f"match={match.group(0)}"])

    if evidence:
        return CandidateVerdict(
            QUALIFIED, "station_signals",
            "station signals present: " + ", ".join(evidence), evidence)

    return CandidateVerdict(
        NEEDS_REVIEW, "no_signal",
        "no decisive station or non-station signal", [])
