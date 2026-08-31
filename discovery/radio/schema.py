"""Normalized radio-station and contact records.

JSON-mappable dataclasses designed to map cleanly onto the Phase 1 entity
model (Organization / Contact / ContactMethod / Source) in Phase 6 — no
database is touched here.

Working convention: the pipeline builds StationRecord instances, then uses
their ``to_dict()`` form for deduplication (enrichment.dedupe), scoring
(enrichment.confidence) and output. Dicts are the interchange format;
dataclasses guarantee field shape at construction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

ORGANIZATION_TYPE = "radio_station"


def _new_id() -> str:
    return str(uuid.uuid4())


@dataclass
class ContactRecord:
    """A person or role-based contact point attached to a station.

    An email is never assumed to belong to a named person: name may be None.
    """

    id: str = field(default_factory=_new_id)
    station_id: str | None = None
    name: str | None = None
    role: str = "unknown"
    email: str | None = None
    phone: str | None = None
    source_url: str | None = None
    confidence_score: float = 0.2
    verified_at: str | None = None
    provenance: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "station_id": self.station_id,
            "name": self.name,
            "role": self.role,
            "email": self.email,
            "phone": self.phone,
            "source_url": self.source_url,
            "confidence_score": self.confidence_score,
            "verified_at": self.verified_at,
            "provenance": list(self.provenance),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ContactRecord":
        known = {
            "id", "station_id", "name", "role", "email", "phone",
            "source_url", "confidence_score", "verified_at", "provenance",
        }
        return cls(**{k: v for k, v in data.items() if k in known})


# ---------------------------------------------------------------------------
# Phase 3 additions: enriched intelligence records.
# These EXTEND the Phase 2 model; StationRecord/ContactRecord stay unchanged.
# ---------------------------------------------------------------------------

@dataclass
class EnrichedContact:
    """Contact with enrichment-layer confidence and evidence.

    `role` uses the shared role vocabulary (enrichment.roles). Confidence is
    explainable via `confidence_reasons`; `verified_at` stays None until a
    future verification phase actually verifies the contact.
    """

    id: str = field(default_factory=_new_id)
    station_id: str | None = None
    name: str | None = None
    role: str = "unknown"
    email: str | None = None
    phone: str | None = None
    source_url: str | None = None
    confidence_score: float = 0.0
    confidence_reasons: list[str] = field(default_factory=list)
    verified_at: str | None = None
    preferred_for_submissions: bool = False
    provenance: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "station_id": self.station_id,
            "name": self.name,
            "role": self.role,
            "email": self.email,
            "phone": self.phone,
            "source_url": self.source_url,
            "confidence_score": round(self.confidence_score, 2),
            "confidence_reasons": list(self.confidence_reasons),
            "verified_at": self.verified_at,
            "preferred_for_submissions": self.preferred_for_submissions,
            "provenance": list(self.provenance),
        }


@dataclass
class SubmissionPath:
    """The evidenced way an artist can submit music to this station.

    FACT fields (url/email/instructions/restrictions) carry provenance.
    `methods` is an INFERENCE produced by enrichment.submissions and is
    labeled as such inside its own structure.
    """

    submission_url: dict | None = None          # Fact
    submission_email: str | None = None         # plain value; provenance in email_facts
    programming_contact_role: str | None = None # e.g. music_director, if evidenced
    instructions: dict | None = None            # Fact (text snippet)
    restrictions: list[dict] = field(default_factory=list)
    methods: dict | None = None                 # inference bundle
    confidence_score: float = 0.0
    confidence_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "submission_url": dict(self.submission_url) if self.submission_url else None,
            "submission_email": self.submission_email,
            "programming_contact_role": self.programming_contact_role,
            "instructions": dict(self.instructions) if self.instructions else None,
            "restrictions": [dict(r) for r in self.restrictions],
            "methods": dict(self.methods) if self.methods else None,
            "confidence_score": round(self.confidence_score, 2),
            "confidence_reasons": list(self.confidence_reasons),
        }


@dataclass
class SourceFetchRecord:
    """Outcome of one enrichment-time fetch of a known station URL."""

    url: str
    ok: bool
    status: int | None = None
    error_kind: str | None = None
    fetched_at: str = ""

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "ok": self.ok,
            "status": self.status,
            "error_kind": self.error_kind,
            "fetched_at": self.fetched_at,
        }


# Categories used to classify a discovered station-level useful page. The
# category is an INFERENCE from anchor text / surrounding context; the URL
# itself is always the exact href discovered on the crawled page.
USEFUL_PAGE_CATEGORIES = frozenset({
    "send_music",
    "dj_directory",
    "contact",
    "programming",
    "about",
    "submission_guidelines",
    "other",
})


@dataclass
class UsefulPage:
    """A station-level page discovered as an exact link on a crawled page.

    Data-integrity contract: ``url`` is the EXACT resolved href found in the
    HTML anchor. It is never constructed, normalized into a different route,
    or guessed from the station domain. ``label`` is the anchor text exactly
    as discovered (trimmed). ``category`` is a labeled inference; ``source_url``
    is the page the link was found on. ``reachable``/``status`` come only from
    a recorded fetch of this exact URL, never from a guess.
    """

    url: str
    label: str
    category: str = "other"
    source_url: str = ""
    method: str = "link"
    discovered_at: str = ""
    rechecked_at: str = ""
    reachable: bool | None = None
    status: int | None = None
    provenance: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "label": self.label,
            "category": self.category,
            "source_url": self.source_url,
            "method": self.method,
            "discovered_at": self.discovered_at,
            "rechecked_at": self.rechecked_at,
            "reachable": self.reachable,
            "status": self.status,
            "provenance": list(self.provenance),
        }


@dataclass
class RadioIntelligenceRecord:
    """Phase 3 output: an enriched radio intelligence record.

    Wraps/extends a discovered StationRecord without mutating it. Absent
    fields are UNKNOWN by omission — nothing is fabricated. Every evidence-
    backed value carries Fact-style provenance.
    """

    station_id: str = ""
    organization_type: str = ORGANIZATION_TYPE
    # identity
    name: str = ""
    alternate_names: list[str] = field(default_factory=list)
    website: str | None = None
    domain: str | None = None
    # location
    country: str | None = None
    state_or_region: str | None = None
    city: str | None = None
    market_area: str | None = None
    # characteristics
    station_type: str = "unknown"
    classification_confidence: float = 0.0
    classification_evidence: list[str] = field(default_factory=list)
    formats: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    genre_evidence: dict = field(default_factory=dict)
    language: str | None = None
    description: str | None = None
    # contacts & channels
    emails: list[dict] = field(default_factory=list)       # Facts + quality
    phone_numbers: list[dict] = field(default_factory=list)  # Facts
    contacts: list[EnrichedContact] = field(default_factory=list)
    submission: SubmissionPath | None = None
    useful_pages: list[UsefulPage] = field(default_factory=list)
    social_urls: dict[str, str] = field(default_factory=dict)
    # sources & lifecycle
    source_urls: list[str] = field(default_factory=list)
    fetches: list[SourceFetchRecord] = field(default_factory=list)
    discovered_at: str = ""
    last_verified_at: str | None = None
    last_observed_at: str = ""
    # scoring & state
    confidence_score: float = 0.0
    confidence_reasons: list[str] = field(default_factory=list)
    status: str = "enriched"
    raw_metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "station_id": self.station_id,
            "organization_type": self.organization_type,
            "name": self.name,
            "alternate_names": list(self.alternate_names),
            "website": self.website,
            "domain": self.domain,
            "country": self.country,
            "state_or_region": self.state_or_region,
            "city": self.city,
            "market_area": self.market_area,
            "station_type": self.station_type,
            "classification_confidence": round(self.classification_confidence, 2),
            "classification_evidence": list(self.classification_evidence),
            "formats": list(self.formats),
            "genres": list(self.genres),
            "genre_evidence": dict(self.genre_evidence),
            "language": self.language,
            "description": self.description,
            "emails": [dict(f) for f in self.emails],
            "phone_numbers": [dict(f) for f in self.phone_numbers],
            "contacts": [c.to_dict() for c in self.contacts],
            "submission": self.submission.to_dict() if self.submission else None,
            "useful_pages": [p.to_dict() for p in self.useful_pages],
            "social_urls": dict(self.social_urls),
            "source_urls": list(self.source_urls),
            "fetches": [f.to_dict() for f in self.fetches],
            "discovered_at": self.discovered_at,
            "last_verified_at": self.last_verified_at,
            "last_observed_at": self.last_observed_at,
            "confidence_score": round(self.confidence_score, 2),
            "confidence_reasons": list(self.confidence_reasons),
            "status": self.status,
            "raw_metadata": dict(self.raw_metadata),
        }


@dataclass
class StationRecord:
    """Normalized radio station entity (Phase 2 local representation)."""

    id: str = field(default_factory=_new_id)
    organization_type: str = ORGANIZATION_TYPE
    name: str = ""
    alternate_names: list[str] = field(default_factory=list)
    legal_name: str | None = None
    website: str | None = None
    country: str | None = None
    state_or_region: str | None = None
    city: str | None = None
    station_type: str = "unknown"
    classification_confidence: float = 0.0
    classification_evidence: list[str] = field(default_factory=list)
    format: str | None = None
    genres: list[str] = field(default_factory=list)
    language: str | None = None
    description: str | None = None
    emails: list[dict] = field(default_factory=list)          # Fact + quality
    contacts: list[ContactRecord] = field(default_factory=list)
    phone_numbers: list[dict] = field(default_factory=list)   # Facts
    submission_url: dict | None = None                        # Fact
    contact_url: dict | None = None                           # Fact
    programming_url: dict | None = None                       # Fact
    social_urls: dict[str, str] = field(default_factory=dict)
    source_urls: list[str] = field(default_factory=list)
    website_reachable: bool = False
    name_matches_site: bool = False
    discovered_at: str = ""
    last_verified_at: str | None = None
    last_observed_at: str = ""
    confidence_score: float = 0.0
    confidence_reasons: list[str] = field(default_factory=list)
    status: str = "discovered"
    raw_metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "organization_type": self.organization_type,
            "name": self.name,
            "alternate_names": list(self.alternate_names),
            "legal_name": self.legal_name,
            "website": self.website,
            "country": self.country,
            "state_or_region": self.state_or_region,
            "city": self.city,
            "station_type": self.station_type,
            "classification_confidence": round(
                self.classification_confidence, 2),
            "classification_evidence": list(self.classification_evidence),
            "format": self.format,
            "genres": list(self.genres),
            "language": self.language,
            "description": self.description,
            "emails": [dict(f) for f in self.emails],
            "contacts": [c.to_dict() for c in self.contacts],
            "phone_numbers": [dict(f) for f in self.phone_numbers],
            "submission_url": dict(self.submission_url) if self.submission_url else None,
            "contact_url": dict(self.contact_url) if self.contact_url else None,
            "programming_url": dict(self.programming_url) if self.programming_url else None,
            "social_urls": dict(self.social_urls),
            "source_urls": list(self.source_urls),
            "website_reachable": self.website_reachable,
            "name_matches_site": self.name_matches_site,
            "discovered_at": self.discovered_at,
            "last_verified_at": self.last_verified_at,
            "last_observed_at": self.last_observed_at,
            "confidence_score": round(self.confidence_score, 2),
            "confidence_reasons": list(self.confidence_reasons),
            "status": self.status,
            "raw_metadata": dict(self.raw_metadata),
        }
