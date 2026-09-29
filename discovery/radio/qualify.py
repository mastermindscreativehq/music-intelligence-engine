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

   - ``qualified``    — decisive self-identification on the site's own entry
                         point (callsign or frequency TOGETHER WITH a
                         self-reference such as "our frequency", "we
                         broadcast", "broadcasting from …", "listen live").
   - ``rejected``     — the host is a hard-denied destination (government,
                         news organization, encyclopedia, Q&A site, directory,
                         social/streaming platform), or the page is a
                         per-item/article path — nothing on it can ever be an
                         official station site.
   - ``needs_review`` — the site was reachable but carries no verifiable
                         self-identification; it is quarantined, never
                         promoted. A page that only REFERENCES a station
                         (``mentions_station``) lands here too.

   A station record must represent an actual station, not an article or page
   that happens to mention one.

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

# --- article / per-item page segments (compound + locale tolerant) ----------
#
# A station's OFFICIAL site lives on its entry point (the site root, or a
# shallow station route). It does not live under ``/news/``,
# ``/news-events/``, ``/en-int/about/news-info/`` or ``/blog/``. Matching any
# path SEGMENT — not just the first — is what catches compound and
# locale-prefixed forms that the anchored ``_REJECT_PATH_RE`` misses, e.g.
# ``/twin-cities.umn.edu/news-events/real-college-radio-...``,
# ``/rode.com/en-int/about/news-info/they-killed-local-radio-...`` and
# ``/frontiersin.org/journals/communication/articles/10.3389/.../full``.
_ARTICLE_SEGMENT_EXACT = frozenset({
    "news", "newsroom", "news-update", "news-updates", "newsupdate",
    "newsletter", "newsletters",
    "article", "articles", "interview", "interviews", "editorial",
    "blog", "blogs", "post", "posts", "story", "stories",
    "press", "press-releases", "press-release", "pressroom", "media-center",
    "feature", "features", "opinion", "opinions",
    "review", "reviews", "podcast", "podcasts", "episode", "episodes",
    "magazine", "publication", "publications",
})
# Compound roots whose first token is itself an article root: ``news-events``,
# ``news-info``, ``blog-post``, ``press-release`` … Normalised below.
_ARTICLE_SEGMENT_PREFIXES = (
    "news-", "news_", "press-", "press_", "blog-", "blog_",
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
    r"radio station(?:,| which)? (?:we|that we)|on-air station|"
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

# --- station SELF-identification -------------------------------------------
#
# A callsign or a frequency only identifies a station when the site presents
# it as its OWN. Inside article prose ("KCPR 91.7 FM broadcasts from ...")
# the same words are a REFERENCE to some other station, so decisive evidence
# is only accepted together with a self-reference from this site. Quoted or
# third-party prose is deliberately not covered by any of these patterns.
_SITE_SELF_REFERENCE_RE = re.compile(
    r"\b("
    r"our (?:call letters|callsign|frequency|station|programming|schedule"
    r"|lineup|studios?|signal|transmitter)|"
    r"we broadcast(?:ing|s)?|we are (?:on the air|an? (?:radio|station)"
    r"|listening)|"
    r"broadcasting (?:from|live|on|via|in)|broadcasting station|"
    r"this (?:radio )?station (?:broadcasts|plays|is|transmits)|"
    r"radio station(?:,| which)? (?:we|that we)|on-air station|"
    r"listen (?:live|now|online)|tune in|now on air|on the air now|"
    r"currently on air|we are live|live stream(?:ing)?|now playing|"
    r"currently playing|press play|the player below"
    r")\b",
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


def _norm_segment(segment: str) -> str:
    """One path segment reduced to a comparable form (lowercase, ``_``→``-``,
    surrounding noise stripped) so ``news_events`` == ``news-events``."""
    return segment.strip().strip(".,;:*").lower().replace("_", "-")


def article_path_segment(url: str) -> str | None:
    """The first per-item/article path segment in *url*, or ``None``.

    An article is never a station's official entry point, so this is the
    page-type precedence check applied BEFORE any station-evidence scan.
    """
    for raw_segment in _path(url).split("/"):
        segment = _norm_segment(raw_segment)
        if not segment:
            continue
        if segment in _ARTICLE_SEGMENT_EXACT:
            return segment
        if segment.startswith(_ARTICLE_SEGMENT_PREFIXES):
            return segment
    return None


def _is_per_item_path(url: str) -> tuple[bool, str]:
    """``(is_per_item, reason)`` for an obviously non-entry-point URL."""
    if _REJECT_PATH_RE.match(_path(url)):
        return True, f"path {_path(url)!r} is a per-item/non-station page"
    segment = article_path_segment(url)
    if segment is not None:
        return True, (f"path {_path(url)!r} is an article/per-item page "
                      f"(segment {segment!r}), not a station entry point")
    return False, ""


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

    if _is_per_item_path(url)[0]:
        return CandidateVerdict(
            REJECTED, "non_station_path", _is_per_item_path(url)[1],
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
    secondary_texts: tuple[str, ...] = (),
) -> SiteVerdict:
    """The HARD gate: decide whether a fetched site is an actual station.

    Deterministic, no network. Takes the site's homepage URL plus any text
    actually collected from the site (homepage title, page text, search
    snippet). A site qualifies ONLY on verifiable on-site station evidence —
    never on a search snippet's mention of "radio".

    Two rules keep a page that merely TALKS ABOUT a station from becoming a
    station record:

    1. **Page-type precedence.** A per-item/article path (any segment: bare,
       compound such as ``news-events``/``news-info``, or locale-prefixed
       such as ``/en-int/about/news-info/``) is never a station's own entry
       point, so it is rejected before any evidence is examined.
    2. **Self-identification on the entry point.** Only *texts* (the site's
       own homepage/primary entry page) may promote a verdict to
       ``qualified``, and decisive evidence (a callsign or a frequency) is
       only accepted together with a self-reference from this site.
       ``secondary_texts`` (linked sub-pages) are still scanned for deny /
       information signals and are reported in the evidence, but they can
       never promote a page to ``qualified``.

    - denied host (government / news / encyclopedia / Q&A / directory /
      platform) -> REJECTED, no further text is examined.
    - otherwise QUALIFIED when decisive self-identification is present.
    - a reachable site that only references stations -> NEEDS_REVIEW
      (``mentions_station``), quarantined, never promoted.
    - a reachable site with no station evidence -> NEEDS_REVIEW
      (``no_station_evidence``), quarantined, never promoted.
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

    # (1) page-type precedence: an article is never the station's own site.
    per_item, per_item_reason = _is_per_item_path(website_url)
    if per_item:
        return SiteVerdict(
            REJECTED, "non_station_path", per_item_reason,
            [f"path={_path(website_url)}"], evaluated_at)

    # Entry-point text: what THIS site says about ITSELF. Secondary text
    # (linked sub-pages) is deliberately excluded from promotion.
    entry_parts = [part for part in (homepage_title, *texts) if part]
    entry_text = " ".join(" ".join(entry_parts).split())

    # Deny/information rules still see everything the site publishes.
    everything = " ".join(
        part for part in (homepage_title, *texts, *secondary_texts, snippet)
        if part)
    everything = " ".join(everything.split())
    if not everything:
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
        if pattern.search(entry_text):
            evidence.append(name)
    # Sub-page evidence is recorded for review but cannot promote.
    secondary_evidence: list[str] = []
    for name, pattern in (
        ("callsign", _SITE_CALLSIGN_RE),
        ("frequency", _SITE_FREQ_RE),
        ("station_type", _SITE_STATION_TYPE_RE),
        ("broadcast_self", _SITE_BROADCAST_SELF_RE),
        ("listen_air", _SITE_LISTEN_AIR_RE),
        ("schedule", _SITE_SCHEDULE_RE),
    ):
        if pattern.search(" ".join(secondary_texts)):
            secondary_evidence.append(f"subpage:{name}")

    self_reference = bool(_SITE_SELF_REFERENCE_RE.search(entry_text))
    if self_reference:
        evidence.append("self_reference")

    # A station's own entry point usually BRANDS its identity: its callsign
    # or frequency appears in the page title. This is recorded as supporting
    # evidence only. It is deliberately NOT required for promotion, because
    # the primary page is frequently an interior page ("Music Submissions -
    # Radio Laurier", "How to Submit Music") whose title carries no callsign;
    # requiring it would demote real stations. The first-person requirement
    # below is the load-bearing gate.
    title_text = " ".join((homepage_title or "").split())
    branded_decisive = any(
        pattern.search(title_text)
        for pattern in (_SITE_CALLSIGN_RE, _SITE_FREQ_RE))
    if branded_decisive:
        evidence.append("branded_identity")

    decisive = {"callsign", "frequency"}
    support = {"station_type", "broadcast_self", "listen_air", "schedule"}
    strong = [e for e in evidence if e in decisive]
    supporting = [e for e in evidence if e in support]

    if _SITE_INFO_RE.search(everything):
        verdict = CandidateVerdict(
            REJECTED, "non_station_info",
            "page text reads as information, not a station",
            [f"match={_SITE_INFO_RE.search(everything).group(0)}"])
        return _as_site_verdict(verdict, evaluated_at, secondary_evidence)

    # (2) Self-identification required, on the site's OWN entry point. The
    # page must speak about ITSELF in the first person as an on-air service.
    # Third-person station mentions ("KCPR broadcasts from the University of
    # Minnesota", "Radio K is a low-power station") are REFERENCES -- article
    # prose, a directory listing, a news report -- and are quarantined rather
    # than promoted. This, not the title, is what separates a station's own
    # homepage from a page that merely talks about stations.
    if not self_reference:
        if strong or supporting or _SITE_BRAND_RADIO_RE.search(entry_text):
            referenced = strong + supporting
            if referenced:
                why = ("page references a radio station ("
                       + ", ".join(referenced)
                       + ") but the site never identifies itself as one")
            else:
                why = ("page talks about radio but the site never identifies "
                       "itself as a station")
            return SiteVerdict(
                NEEDS_REVIEW, "mentions_station", why,
                evidence + secondary_evidence, evaluated_at)
        return SiteVerdict(
            NEEDS_REVIEW, "no_station_evidence",
            "site reachable but no verifiable station evidence found",
            evidence + secondary_evidence, evaluated_at)

    if strong:
        verdict = CandidateVerdict(
            QUALIFIED, "station_signals",
            "station signals present on site: " + ", ".join(evidence),
            evidence)
        return _as_site_verdict(verdict, evaluated_at, secondary_evidence)

    if len(supporting) >= 2:
        verdict = CandidateVerdict(
            QUALIFIED, "broadcast_signals",
            "broadcast language present on site: " + ", ".join(supporting),
            evidence)
        return _as_site_verdict(verdict, evaluated_at, secondary_evidence)

    # A branded name ("KQXR Radio", "The Wire FM") plus any single on-air /
    # programming signal is verifiable self-identification.
    if _SITE_BRAND_RADIO_RE.search(entry_text) and supporting:
        verdict = CandidateVerdict(
            QUALIFIED, "branded_station",
            "station-branded site with on-site broadcast signal: "
            + ", ".join(supporting),
            ["brand_radio", *supporting])
        return _as_site_verdict(verdict, evaluated_at, secondary_evidence)

    return SiteVerdict(
        NEEDS_REVIEW, "no_station_evidence",
        "site reachable but no verifiable station evidence found",
        evidence + secondary_evidence, evaluated_at)


def _as_site_verdict(verdict: CandidateVerdict, evaluated_at: str,
                     secondary: list[str] | None = None) -> SiteVerdict:
    return SiteVerdict(
        verdict.verdict, verdict.kind, verdict.reason,
        list(verdict.evidence) + list(secondary or []), evaluated_at)
