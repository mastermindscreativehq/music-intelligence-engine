"""Deterministic station qualification — pre-fetch AND post-fetch.

This module holds the two gates that keep non-station pages out of the
station tables:

1. ``classify_station_candidate`` classifies a raw ``Candidate`` BEFORE any
   fetch using only its URL, title, and snippet. Verdicts:

   - ``qualified``    — clear station signals (callsign, band/frequency,
                        "radio"/"broadcast"/"community radio", etc.).
   - ``needs_review`` — no clear signal either way; still fetched because
                        unknown is not the same as disqualified.
   - ``rejected``     — a non-station destination (social/streaming/
                        aggregator host, or an article/event/lyrics/jobs/shop
                        path); dropped before fetching.

2. ``classify_station_site`` is the HARD post-fetch gate: it decides whether
   a fetched site IS an actual radio station. Only verifiable station
   evidence qualifies — a page is never promoted from search metadata alone.
   Verdicts:

   - ``qualified``    — decisive on-site station evidence (callsign,
                        frequency/band, broadcast self-reference, on-air /
                        listen-live / programming language, station type).
   - ``rejected``     — the host is a hard-denied destination (government,
                        news organization, encyclopedia, Q&A site, directory,
                        social/streaming platform) — nothing on it can ever
                        be an official station site.
   - ``needs_review`` — the site was reachable but carries no verifiable
                        station evidence; it is quarantined, never promoted.

Both gates are deterministic and generic (no per-station hardcoding).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from crawler.urls import canonical_domain
from discovery.models import utc_now_iso

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

# ---------------------------------------------------------------------------
# Post-fetch deny rules: hosts that can never be an official station site.
# ---------------------------------------------------------------------------

# Registrable domains hard-denied on the post-fetch gate. The site's own
# homepage is the evidence — a government bureau, newsroom, encyclopedia, Q&A
# or directory page mentioning "radio" is NOT a station.
_DENY_HOSTS = frozenset({
    # encyclopedias / reference
    "britannica.com", "encyclopedia.com", "infoplease.com", "wiktionary.org",
    "investopedia.com", "thoughtco.com",
    # Q&A / advice
    "quora.com", "answers.com", "stackexchange.com", "stackoverflow.com",
    # news organizations (network newsrooms, not local stations)
    "nytimes.com", "washingtonpost.com", "wsj.com", "cnn.com", "bbc.com",
    "bbc.co.uk", "theguardian.com", "reuters.com", "apnews.com",
    "nbcnews.com", "cbsnews.com", "abcnews.com", "npr.org", "thehill.com",
    "politico.com", "vox.com", "businessinsider.com", "bloomberg.com",
    "huffpost.com", "buzzfeednews.com",
    # government / public-information portals
    "gov.uk", "parliament.uk", "usa.gov",
})

# TLD suffix rule covers the US federal/state government hosts cited as
# garbage (bls.gov, senate.gov, usda.gov, fcc.gov) and military sites.
_DENY_TLD_SUFFIXES = (".gov", ".mil", ".gov.uk", ".fc.ke")


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


@dataclass
class SiteVerdict:
    """Post-fetch qualification outcome for one fetched site.

    ``evaluated_at`` is an ISO timestamp; everything else mirrors
    :class:`CandidateVerdict`. Persisted (raw_metadata.qualification) to keep
    rejected/non-station rows reviewable after the fact.
    """

    verdict: str
    kind: str = "unknown"
    reason: str = ""
    evidence: list[str] = field(default_factory=list)
    evaluated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "kind": self.kind,
            "reason": self.reason,
            "evidence": list(self.evidence),
            "evaluated_at": self.evaluated_at,
        }


# Post-fetch positive station signals, named for the evidence list.
_SITE_CALLSIGN_RE = re.compile(r"\b(?:W|K|C|X)[A-Z]{2,4}\b")
_SITE_FREQ_RE = re.compile(
    r"\b\d{2,3}(?:\.\d{1,2})?\s?(?:fm|am|khz|mhz)\b", re.I)
_SITE_STATION_TYPE_RE = re.compile(
    r"\b("
    r"community radio|public radio|college radio|campus radio|"
    r"student radio|independent radio|internet radio|pirate radio|"
    r"non-commercial radio|low[- ]power radio|"
    r"sports radio|news[ /-]talk radio|talk radio|"
    r"religious radio|christian radio|classical radio|ethnic radio"
    r")\b",
    re.I,
)
_SITE_BROADCAST_SELF_RE = re.compile(
    r"\b("
    r"we broadcast(?:ing|s)?|we are on the air|we are (?:a|an) radio|"
    r"this station (?:broadcasts|plays|is)|"
    r"broadcasting (?:from|live|on|via|in)|broadcasting station|"
    r"radio station(?:,| which)? (?:we|that)|on-air station|"
    r"our (?:call letters|callsign|frequency|station|programming)"
    r")\b",
    re.I,
)
_SITE_LISTEN_AIR_RE = re.compile(
    r"\b("
    r"listen (?:live|now)|tune in|tuned in|now on air|on the air now|"
    r"currently on air|live stream(?:ing)?|now playing|currently playing|"
    r"press play|the player below"
    r")\b",
    re.I,
)
_SITE_SCHEDULE_RE = re.compile(
    r"\b("
    r"program(?:me)? schedule|show schedule|station schedule|"
    r"weekly (?:schedule|lineup)|upcoming (?:shows|programs)|"
    r"dj (?:shows?|programs?|lineup)|"
    r"broadcast (?:schedule|times?)|air(?:ing)? times?"
    r")\b",
    re.I,
)
# A brand that identifies itself as a station ("KQXR Radio", "The Wire FM")
# is only corroboration — it never qualifies alone.
_SITE_BRAND_RADIO_RE = re.compile(
    r"\b(?:[A-Za-z0-9&.'\- ]{2,40}\s)?"
    r"(?:radio|fm|am|station)\b(?![^\n]{0,20}\b(?:jobs|courses|history)\b)",
    re.I,
)
# Decisive text that a reachable page is an information page, not a station
# — only consulted when no station evidence exists.
_SITE_INFO_RE = re.compile(
    r"\b("
    r"how to start a radio station|history of radio|radio technology|"
    r"careers?\b.{0,30}\b(?:radio|broadcast)|jobs?\b.{0,30}\broadio|"
    r"radio school|study radio|radio courses"
    r")\b",
    re.I,
)


def _host_denied(host: str) -> bool:
    if not host:
        return False
    if any(host.endswith(suffix) for suffix in _DENY_TLD_SUFFIXES):
        return True
    for denied in _DENY_HOSTS:
        if host == denied or host.endswith("." + denied):
            return True
    return False


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


# ---------------------------------------------------------------------------
# Post-fetch hard gate
# ---------------------------------------------------------------------------

def classify_station_site(
    *,
    website_url: str,
    homepage_title: str = "",
    texts: tuple[str, ...] = (),
    snippet: str = "",
) -> SiteVerdict:
    """The HARD gate: decide whether a fetched site is an actual station.

    Deterministic, no network. Takes the site's homepage URL plus any text
    actually collected from the site (homepage title, page text, search
    snippet). A site qualifies ONLY on verifiable on-site station evidence —
    never on a search snippet's mention of "radio".

    - denied host (government / news / encyclopedia / Q&A / directory /
      platform) -> REJECTED, no further text is examined.
    - otherwise QUALIFIED when decisive evidence is present.
    - a reachable site with no station evidence -> NEEDS_REVIEW (quarantined,
      never promoted).
    """
    evaluated_at = utc_now_iso()
    host = _host(website_url)
    platform = _platform_host(host) if host else None
    if platform is not None:
        return SiteVerdict(
            REJECTED, "platform_page",
            f"host {host!r} is a {platform} page, not a station site",
            [f"host={host}"], evaluated_at)
    if _host_denied(host):
        return SiteVerdict(
            REJECTED, "denied_host",
            f"host {host!r} is a government/news/encyclopedia/Q&A site, "
            "not a station",
            [f"host={host}"], evaluated_at)

    text = " ".join(
        part for part in (homepage_title, *texts, snippet) if part)
    text = " ".join(text.split())
    if not text:
        return SiteVerdict(
            NEEDS_REVIEW, "no_station_evidence",
            "site reachable but no text evidence to verify it as a station",
            [], evaluated_at)

    evidence: list[str] = []
    for name, pattern in (
        ("callsign", _SITE_CALLSIGN_RE),
        ("frequency", _SITE_FREQ_RE),
        ("station_type", _SITE_STATION_TYPE_RE),
        ("broadcast_self", _SITE_BROADCAST_SELF_RE),
        ("listen_air", _SITE_LISTEN_AIR_RE),
        ("schedule", _SITE_SCHEDULE_RE),
    ):
        if pattern.search(text):
            evidence.append(name)

    decisive = {"callsign", "frequency"}
    support = {"station_type", "broadcast_self", "listen_air", "schedule"}
    strong = [e for e in evidence if e in decisive]
    supporting = [e for e in evidence if e in support]

    if strong:
        verdict = CandidateVerdict(
            QUALIFIED, "station_signals",
            "station signals present on site: " + ", ".join(evidence),
            evidence)
        return _as_site_verdict(verdict, evaluated_at)

    if len(supporting) >= 2:
        verdict = CandidateVerdict(
            QUALIFIED, "broadcast_signals",
            "broadcast language present on site: " + ", ".join(supporting),
            evidence)
        return _as_site_verdict(verdict, evaluated_at)

    # A branded name ("KQXR Radio", "The Wire FM") plus any single on-air /
    # programming signal is verifiable self-identification.
    if _SITE_BRAND_RADIO_RE.search(text) and supporting:
        verdict = CandidateVerdict(
            QUALIFIED, "branded_station",
            "station-branded site with on-site broadcast signal: "
            + ", ".join(supporting),
            ["brand_radio", *supporting])
        return _as_site_verdict(verdict, evaluated_at)

    if _SITE_INFO_RE.search(text):
        verdict = CandidateVerdict(
            REJECTED, "non_station_info",
            "page text reads as information, not a station",
            [f"match={_SITE_INFO_RE.search(text).group(0)}"])
        return _as_site_verdict(verdict, evaluated_at)

    return SiteVerdict(
        NEEDS_REVIEW, "no_station_evidence",
        "site reachable but no verifiable station evidence found",
        evidence, evaluated_at)


def _as_site_verdict(verdict: CandidateVerdict, evaluated_at: str) -> SiteVerdict:
    return SiteVerdict(
        verdict.verdict, verdict.kind, verdict.reason,
        list(verdict.evidence), evaluated_at)
