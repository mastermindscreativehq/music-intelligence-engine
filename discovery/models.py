"""Core discovery domain models.

Organization-type agnostic on purpose: DiscoveryRequest, Candidate, Fact
(provenance), Failure and DiscoveryResult are shared by every future target
type. Radio-specific records live in ``discovery.radio.schema``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SourceType(str, Enum):
    OFFICIAL_SOURCE = "official_source"
    DIRECTORY_SOURCE = "directory_source"
    SOCIAL_SOURCE = "social_source"
    SEARCH_SOURCE = "search_source"
    OTHER = "other"


REQUEST_FIELDS = {
    "query", "country", "state_or_region", "city",
    "station_type", "genre", "language", "limit",
}

_QUERY_MAX_LEN = 300


@dataclass
class DiscoveryRequest:
    """Structured discovery input; malformed input fails fast here."""

    query: str
    country: str | None = None
    state_or_region: str | None = None
    city: str | None = None
    station_type: str | None = None
    genre: str | None = None
    language: str | None = None
    limit: int = 50

    def __post_init__(self) -> None:
        if not isinstance(self.query, str) or not self.query.strip():
            raise ValueError("query must be a non-empty string")
        if len(self.query) > _QUERY_MAX_LEN:
            raise ValueError("query exceeds 300 characters")
        self.query = self.query.strip()
        for name in ("country", "state_or_region", "city",
                     "station_type", "genre", "language"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a non-empty string or None")
            if isinstance(value, str):
                setattr(self, name, value.strip())
        if not isinstance(self.limit, int) or isinstance(self.limit, bool):
            raise ValueError("limit must be an integer")
        if not 1 <= self.limit <= 500:
            raise ValueError("limit must be between 1 and 500")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DiscoveryRequest":
        if not isinstance(data, dict):
            raise ValueError("request must be a JSON object")
        unknown = set(data) - REQUEST_FIELDS
        if unknown:
            raise ValueError(f"unknown request fields: {sorted(unknown)}")
        kwargs = {key: data[key] for key in REQUEST_FIELDS if key in data}
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "country": self.country,
            "state_or_region": self.state_or_region,
            "city": self.city,
            "station_type": self.station_type,
            "genre": self.genre,
            "language": self.language,
            "limit": self.limit,
        }


@dataclass
class Candidate:
    """A raw candidate organization URL returned by a provider."""

    title: str
    url: str
    source: str
    snippet: str = ""
    source_type: SourceType = SourceType.SEARCH_SOURCE
    discovered_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url.strip():
            raise ValueError("candidate url must be a non-empty string")
        if not isinstance(self.title, str):
            self.title = ""
        if not isinstance(self.source_type, SourceType):
            try:
                self.source_type = SourceType(self.source_type)
            except ValueError:
                self.source_type = SourceType.OTHER

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "snippet": self.snippet,
            "source_type": self.source_type.value,
            "discovered_at": self.discovered_at,
        }


@dataclass
class Fact:
    """A discovered value with full provenance.

    Invariant of the engine: no meaningful field exists without one of these.
    """

    value: str
    source_url: str
    source_type: str
    method: str = "rule"
    discovered_at: str = field(default_factory=utc_now_iso)
    also_seen_at: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise ValueError("fact value must be a string")
        if not isinstance(self.also_seen_at, list):
            self.also_seen_at = []

    def observe_again(self, source_url: str) -> None:
        """Record a later sighting without destroying the original provenance."""
        if source_url and source_url != self.source_url \
                and source_url not in self.also_seen_at:
            self.also_seen_at.append(source_url)

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "source_url": self.source_url,
            "source_type": self.source_type,
            "method": self.method,
            "discovered_at": self.discovered_at,
            "also_seen_at": list(self.also_seen_at),
        }


def fact_observe_again(fact: dict[str, Any], source_url: str) -> None:
    """Dict-form twin of :meth:`Fact.observe_again` for merged records."""
    if not isinstance(fact, dict):
        return
    seen = fact.setdefault("also_seen_at", [])
    if source_url and source_url != fact.get("source_url") \
            and source_url not in seen:
        seen.append(source_url)


@dataclass
class Failure:
    """One recoverable failure; recorded so runs continue past bad sites."""

    stage: str
    error_kind: str
    message: str
    url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "error_kind": self.error_kind,
            "message": self.message,
            "url": self.url,
        }


@dataclass
class DiscoveryResult:
    """Outcome of one discovery run: normalized records + failures."""

    request: dict[str, Any]
    queries: list[str]
    records: list[dict[str, Any]] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)
    started_at: str = field(default_factory=utc_now_iso)
    completed_at: str | None = None

    @property
    def record_count(self) -> int:
        return len(self.records)

    @property
    def failure_count(self) -> int:
        return len(self.failures)

    def to_dict(self) -> dict[str, Any]:
        self.completed_at = self.completed_at or utc_now_iso()
        return {
            "request": self.request,
            "queries": list(self.queries),
            "record_count": self.record_count,
            "failure_count": self.failure_count,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "records": [r.to_dict() if hasattr(r, "to_dict") else r
                        for r in self.records],
            "failures": [f.to_dict() for f in self.failures],
        }


@dataclass
class EnrichmentResult:
    """Outcome of one enrichment run (Phase 3). Generic across org types."""

    records: list[Any] = field(default_factory=list)   # enriched record dicts
    failures: list[Failure] = field(default_factory=list)
    started_at: str = field(default_factory=utc_now_iso)
    completed_at: str | None = None

    @property
    def record_count(self) -> int:
        return len(self.records)

    @property
    def failure_count(self) -> int:
        return len(self.failures)

    def to_dict(self) -> dict[str, Any]:
        self.completed_at = self.completed_at or utc_now_iso()
        return {
            "record_count": self.record_count,
            "failure_count": self.failure_count,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "records": [r.to_dict() if hasattr(r, "to_dict") else r
                        for r in self.records],
            "failures": [f.to_dict() for f in self.failures],
        }
