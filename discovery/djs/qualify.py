"""DJ candidate qualification: the strict gate between discovery and
ingestion.

A webpage being found does NOT make a qualified DJ. ``quality`` rules follow
the product contract DISCOVERED → VERIFIED → RELEVANT → ACTIONABLE:

- A candidate is *qualified* only when positive evidence on the result or
  the fetched page shows an actual DJ / selector / host / programmer /
  decision-maker (personal DJ site, platform profile, radio-host page, club
  residence, bookings/mixes text, a leading ``DJ`` title).
- Strong non-DJ markers (event/ticketing hosts and URLs, playlist and
  concert/listing pages) *reject* the candidate even when a DJ name appears —
  an event page is not DJ evidence.
- Otherwise the candidate is ``needs_review``: it is NEVER ingested
  automatically, because a generic/music/venue page merely mentioning a DJ
  is not sufficient evidence.

Precedence: reject first (disqualifiers win), then qualify on positive
evidence, else needs_review.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from discovery.models import utc_now_iso

VERDICT_QUALIFIED = "qualified"
VERDICT_REJECTED = "rejected"
VERDICT_NEEDS_REVIEW = "needs_review"

KIND_PERSONAL_SITE = "personal_site"
KIND_PLATFORM_PROFILE = "platform_profile"
KIND_NOT_A_DJ = "not_a_dj"

# Event/ticketing/subscription-result hosts. A URL on one of these is never
# DJ evidence, even when the result title names a specific act.
_REJECT_HOST_FRAGMENTS = (
    "eventbrite", "ticketmaster", "songkick", "seatgeek", "eventful",
    "dice.", "dice.fm", "lu.ma", "meetup.", "biletix", "enterticket",
)

# Path fragments that mark event/ticket/playlist/listing pages.
_REJECT_PATH_FRAGMENTS = (
    "/events", "/event/", "/tickets", "/ticketing", "/date/",
    "/playlist", "/sets", "/shows", "/tour/", "/gigs",
)

# Text markers that identify an event/ticketing/playlist/listing page.
_REJECT_TEXT_FRAGMENTS = (
    "buy tickets", "get tickets", "tickets on sale", "tickets", "ticketing",
    "rsvp", "register now", "eventbrite", "what's on", "things to do",
    "upcoming events", "event listing", "event calendar", "events calendar",
    "concert listing", "concerts in", "gigs in", "gig listings",
    "gig guide", "tour dates", "line-up", "lineup", "playlist",
    "playlists", "mixtape",
)

# DJ platform profiles are music-person pages; they qualify ONLY with a DJ
# signal in the title/snippet, otherwise they could be a musician.
_PLATFORM_HOSTS = ("mixcloud.com", "soundcloud.com")

_POSITIVE_TITLE_FRAGMENTS = (
    "dj bookings", "booking this dj", "book this dj", "hire a dj",
    "hire this dj", "dj services", "club dj", "wedding dj",
    "resident dj", "radio host", "selecta", "selector", "turntablist",
    "turntablism", "dj mixes", "studio mix", "deejay", "dee-jay",
)


@dataclass(frozen=True)
class Qualification:
    """Deterministic classification verdict for one DJ candidate."""

    verdict: str
    kind: str | None
    reason: str
    evidence_url: str
    evaluated_at: str

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "kind": self.kind,
            "reason": self.reason,
            "evidence_url": self.evidence_url,
            "evaluated_at": self.evaluated_at,
        }


def _host_and_path(url: str | None) -> tuple[str, str]:
    try:
        parts = urlsplit(url or "")
        return (parts.netloc or "").lower(), (parts.path or "").lower()
    except ValueError:
        return "", ""


def _lead_dj_signal(text: str) -> bool:
    """A bare ``DJ`` / ``Selector`` head that names the person."""
    lowered = text.lower()
    if lowered.startswith("dj ") or lowered.startswith("dj,") or \
            lowered.startswith("dj|") or lowered.startswith("dj·") or \
            lowered.startswith("dj —") or lowered.startswith("dj -"):
        return True
    return lowered.startswith(("deejay ", "dee-jay ", "selecta ",
                               "selector ", "resident dj ", "club dj "))


def _positive_evidence(title: str, snippet: str, host: str) -> str | None:
    """Return the positive-evidence label, or None when absent."""
    title_low = title.lower()
    all_text = f"{title_low} {snippet.lower()}"
    if any(fragment in all_text for fragment in _POSITIVE_TITLE_FRAGMENTS):
        return "artist page names DJ / host / selector roles"
    if _lead_dj_signal(title_low):
        return "title names the DJ"
    if host in _PLATFORM_HOSTS and ("dj" in all_text or "deejay" in all_text):
        return "platform profile with DJ signal"
    return None


def _disqualify_host_or_path(url: str) -> str | None:
    """Host/path-level disqualifiers are absolute event/listing markers."""
    host, path = _host_and_path(url)
    for fragment in _REJECT_HOST_FRAGMENTS:
        if fragment in host:
            return f"ticketing/event host detected: {host}"
    for fragment in _REJECT_PATH_FRAGMENTS:
        if fragment in path:
            return f"event/ticket/playlist page path: {path}"
    return None


def _disqualify_text(title: str, snippet: str) -> str | None:
    """Text-level disqualifiers apply only when no DJ evidence exists."""
    text = f"{title.lower()} {snippet.lower()}"
    for fragment in _REJECT_TEXT_FRAGMENTS:
        if fragment in text:
            return f"event/ticket/playlist marker in page text: {fragment!r}"
    return None


def classify_candidate(*, url: str, title: str = "", snippet: str = "",
                       page_title: str | None = None) -> Qualification:
    """One deterministic verdict for a discovered DJ candidate.

    ``title``/``snippet`` come from the search result; ``page_title`` (when
    fetched) is the ultimate evidence and overrides the result title.

    Precedence: host/path event markers always disqualify; positive DJ
    evidence then qualifies; remaining event/playlist/listing text markers
    disqualify; otherwise the candidate needs human review (never auto-
    ingested).
    """
    evidence_title = (page_title or title or "").strip()
    host, _path = _host_and_path(url)

    hard_reject = _disqualify_host_or_path(url)
    if hard_reject:
        return Qualification(
            verdict=VERDICT_REJECTED, kind=KIND_NOT_A_DJ,
            reason=(f"not a DJ profile — {hard_reject}; evidence URL is not "
                    "a personal site, agency profile or radio/profile page"),
            evidence_url=url, evaluated_at=utc_now_iso())

    evidence = _positive_evidence(evidence_title, snippet, host)
    if evidence:
        kind = KIND_PLATFORM_PROFILE if host in _PLATFORM_HOSTS \
            else KIND_PERSONAL_SITE
        return Qualification(
            verdict=VERDICT_QUALIFIED, kind=kind,
            reason=f"qualified on evidence: {evidence}",
            evidence_url=url, evaluated_at=utc_now_iso())

    soft_reject = _disqualify_text(evidence_title, snippet)
    if soft_reject:
        return Qualification(
            verdict=VERDICT_REJECTED, kind=KIND_NOT_A_DJ,
            reason=(f"not a DJ profile — {soft_reject}; the page is an "
                    "event/ticket/playlist/listing page, not a DJ profile"),
            evidence_url=url, evaluated_at=utc_now_iso())

    return Qualification(
        verdict=VERDICT_NEEDS_REVIEW, kind=None,
        reason=("no DJ evidence on this page and no disqualifying marker — "
                "a generic/mention-only/venue page alone is not a qualified "
                "DJ; human review required before any use"),
        evidence_url=url, evaluated_at=utc_now_iso())