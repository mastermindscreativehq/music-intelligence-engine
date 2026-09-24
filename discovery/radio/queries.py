"""Targeted radio discovery query generation (Phase 1 input fix).

The previous generic query builder produced broad phrases such as
``community radio station`` that SerpAPI/Google answer with government
bureaus, regulatory pages, encyclopedias, directories and articles. This
builder is intentionally aimed at ACTUAL music-radio opportunities: every
query carries a submission or radio-programming intent phrase so the SERP
results are station pages with submission/contact/programming pages — the
evidence the post-fetch hard gate (``discovery.radio.qualify``) needs.

Query focus is a pure function of the structured request — same request,
same queries, no hard-coded cities:

  dimensions          request field
  ------------------  ---------------------------
  station type        ``station_type``   (college / community / independent …)
  genre               ``genre``          (jazz / country / classical …)
  country             ``country``
  state/region        ``state_or_region``
  city                ``city``
  sector head         ``query``          (audit seed; unused in query text)

The music-submission intent itself is a property of THIS builder: the
canonical ``_INTENT_PHRASES`` below are applied to every job regardless of
input, so the intent never depends on a request field. The public
``DiscoveryRequest`` contract (see ``discovery.models``) therefore does NOT
carry ``submission_intent`` — the deployed API contract stays exact.

Role-focused phrasings (``music director`` / ``program director`` /
``music programming``) are always part of the primary-subject group, so
director/programming opportunities surface even on a plain submission job.

Output keeps the generic-builder contract: deterministic, deduplicated,
capped. The provider then issues each query verbatim to SerpAPI; the gate
stays the final authority downstream.
"""

from __future__ import annotations

import re

from discovery.models import DiscoveryRequest

MAX_QUERIES = 8

# Canonical submission phrasings, most-productive first. These are the
# supported Phase 1 intent mechanism: every job applies the full set, so
# each job stays music-submission-targeted no matter its dimensions.
_INTENT_PHRASES = (
    "music submissions",
    "submit music",
    "artist submissions",
    "music submission",
)

# Radio-programming / director phrasings matched against the primary subject.
_ROLE_PHRASES = (
    "music director",
    "program director",
    "music programming",
)

# Intents delivered as imperatives ("submit music") must not be prefixed
# with "accepting ..."; noun phrases ("music submissions") may be.
_VERB_START_INTENT = re.compile(
    r"^(?:submit|send|get|share|upload)\b", re.I)


def _subjects(request: DiscoveryRequest) -> list[str]:
    """Subject heads, most specific first (used verbatim as keyword phrases)."""
    station_type = request.station_type
    genre = request.genre
    subjects: list[str] = []
    if station_type and genre:
        subjects.append(f"{genre} {station_type} radio station")
    if station_type:
        subjects.append(f"{station_type} radio station")
    if genre:
        subjects.append(f"{genre} radio station")
    if not subjects:
        q = (request.query or "").strip().lower()
        if q and "radio" in q:
            subjects.append(" ".join(q.split()))
    if "radio station" not in subjects:
        subjects.append("radio station")
    deduped: list[str] = []
    for subject in subjects:
        collapsed = " ".join(subject.split())
        if collapsed and collapsed not in deduped:
            deduped.append(collapsed)
    return deduped


def _intents() -> list[str]:
    return list(_INTENT_PHRASES)


def build_radio_queries(request: DiscoveryRequest) -> list[str]:
    """Deterministic, submission-targeted radio station queries.

    Emission order: intent-leading query for the primary (most specific)
    subject, role queries, then the same intent-leading query for each broader
    subject, so typed + genre requests still cover ``<type> radio station``
    and ``<genre> radio station`` heads; any leftover budget is used for the
    ``accepting …`` reinforcement and the remaining intent phrasings on the
    primary subject. Geography is appended to every query when provided.
    """
    subjects = _subjects(request)
    intents = _intents()
    geo_parts = [request.city, request.state_or_region, request.country]
    geo = " ".join(p for p in geo_parts if p).strip()

    queries: list[str] = []

    def push(text: str) -> None:
        collapsed = " ".join(text.split())
        if collapsed and collapsed not in queries and \
                len(queries) < MAX_QUERIES:
            queries.append(collapsed)

    primary = subjects[0]
    first_intent = intents[0]

    push(f"{primary} {first_intent} {geo}".strip())
    for role in _ROLE_PHRASES:
        push(f"{primary} {role} {geo}".strip())
    for subject in subjects[1:]:
        push(f"{subject} {first_intent} {geo}".strip())
    if not _VERB_START_INTENT.match(first_intent):
        push(f"{primary} accepting {first_intent} {geo}".strip())
    for intent in intents[1:]:
        push(f"{primary} {intent} {geo}".strip())

    return queries[:MAX_QUERIES]