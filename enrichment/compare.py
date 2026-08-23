"""Cross-source fact comparison (Phase 5).

Compares the provenance-backed observations already stored on an
intelligence record and reports, per claim slot, whether independent
sources AGREE or DISAGREE:

    corroborated  same value observed from >= 2 independent sources
    conflicting   different values claimed for the same slot
    single_source only one source observed the value
    unobserved    no provenance-bearing evidence at all

Hard guarantees (mirrors the engine-wide epistemology):

- The input record is NEVER mutated; comparison is read-only reporting.
- Conflicts are REPORTED with every side's provenance attached — this
  module never picks a winner and never overwrites stronger evidence
  with weaker evidence. Source-strength ranking exists purely to
  annotate which side currently carries the stronger evidence.
- Every entry traces back to exact source URLs and timestamps taken
  verbatim from the stored Facts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from crawler.urls import canonical_domain

from enrichment.emails import normalize_email

CORROBORATED = "corroborated"
CONFLICTING = "conflicting"
SINGLE_SOURCE = "single_source"
UNOBSERVED = "unobserved"

# Strength ranking over the source_type vocabulary already used by the
# engine (enrichment.confidence.CONTACT_SOURCE_WEIGHTS, discovery models).
# Higher wins; used ONLY to annotate conflicts.
SOURCE_STRENGTH = {
    "official_website_page": 4,
    "official_website_mailto": 3,
    "submission_page": 3,
    "contact_page": 2,
    "official_source": 4,
    "directory_source": 1,
    "public_directory": 1,
    "social_source": 0,
    "search_source": 0,
}

_WHITESPACE = re.compile(r"\s+")


def normalize_claim_value(kind: str, raw: object) -> str | None:
    """Canonical form of one observed value for equality checks."""
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    if kind == "email":
        return normalize_email(value)
    lowered = value.casefold()
    if kind == "url":
        return lowered.rstrip("/").split("#", 1)[0]
    return _WHITESPACE.sub(" ", lowered)


def observation_domain(source_url: object) -> str | None:
    """Registrable domain of a source URL; independence unit."""
    if not isinstance(source_url, str):
        return None
    try:
        return canonical_domain(source_url)
    except ValueError:
        return None


@dataclass
class ClaimEntry:
    """Comparison result for ONE claim slot (never mutates the record)."""

    claim: str                       # e.g. "emails[music@kzow.example]"
    kind: str                        # email | url | text
    outcome: str                     # one of the four outcome constants
    values: list[dict] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "claim": self.claim,
            "kind": self.kind,
            "outcome": self.outcome,
            "values": [dict(v) for v in self.values],
            "reasons": list(self.reasons),
        }


class SourceComparator:
    """Collects provenance-bearing observations and compares them."""

    def __init__(self) -> None:
        self._slots: dict[tuple[str, str], list[dict]] = {}
        self._order: list[tuple[str, str]] = []

    def observe(self, claim: str, kind: str, value: str, source_url: str,
                source_type: str = "", discovered_at: str = "") -> None:
        """Record one observation; provenance is kept verbatim."""
        normalized = normalize_claim_value(kind, value)
        if normalized is None:
            return
        key = (claim, kind)
        if key not in self._slots:
            self._slots[key] = []
            self._order.append(key)
        self._slots[key].append({
            "value": normalized,
            "original_value": value,
            "source_url": source_url or "",
            "source_type": source_type or "",
            "discovered_at": discovered_at or "",
        })

    def observe_fact(self, fact: dict, claim: str, kind: str) -> None:
        """Record one Fact-style dict plus its also_seen_at sightings."""
        if not isinstance(fact, dict):
            return
        value = fact.get("value")
        if value is None and kind != "text":
            return
        self.observe(claim, kind, str(value or ""),
                     fact.get("source_url") or "",
                     fact.get("source_type") or "",
                     fact.get("discovered_at") or "")
        for url in fact.get("also_seen_at") or []:
            self.observe(claim, kind, str(value or ""), url,
                         fact.get("source_type") or "",
                         fact.get("discovered_at") or "")

    # -- evaluation ---------------------------------------------------------

    def evaluate(self) -> list[ClaimEntry]:
        """Compare every observed slot; read-only."""
        entries: list[ClaimEntry] = []
        for claim, kind in self._order:
            observations = self._slots[(claim, kind)]
            distinct = _distinct_values(observations)
            domains_by_value = _domains_per_value(distinct)
            entry = ClaimEntry(claim=claim, kind=kind,
                               outcome=_outcome(distinct, domains_by_value))
            entry.values = [_annotate(v, o) for v, o in
                            sorted(distinct.items(),
                                   key=lambda kv: kv[1][0]["discovered_at"]
                                   or "9999")]
            entry.reasons = _reasons(entry.outcome, distinct,
                                     domains_by_value)
            entries.append(entry)
        return entries


def compare_record(record: dict) -> dict:
    """Full comparison report for one intelligence-record dict.

    Extracts every provenance-bearing claim the record carries (email /
    phone facts, submission URL & instructions, contact provenance) and
    evaluates them. Returns {"claims": [...], "summary": {...}}; the
    record itself is never modified.
    """
    comparator = SourceComparator()

    for fact in record.get("emails") or []:
        comparator.observe_fact(fact, f"emails[{fact.get('value')}]", "email")
    for fact in record.get("phone_numbers") or []:
        comparator.observe_fact(fact, f"phone_numbers[{fact.get('value')}]",
                                "text")
    submission = record.get("submission")
    if isinstance(submission, dict):
        comparator.observe_fact(submission.get("submission_url"),
                                "submission.url", "url")
        comparator.observe_fact(submission.get("instructions"),
                                "submission.instructions", "text")

    for index, contact in enumerate(
            record.get("contacts") or [] if isinstance(
                record.get("contacts"), list) else []):
        if not isinstance(contact, dict):
            continue
        for prov in contact.get("provenance") or []:
            if not isinstance(prov, dict):
                continue
            value = prov.get("value")
            if value is None:
                continue
            kind = "email" if "@" in str(value) else "text"
            comparator.observe_fact(prov, f"contacts[{index}]", kind)

    entries = comparator.evaluate()
    summary = {outcome: 0 for outcome in
               (CORROBORATED, CONFLICTING, SINGLE_SOURCE, UNOBSERVED)}
    for entry in entries:
        summary[entry.outcome] += 1
    return {
        "claims": [entry.to_dict() for entry in entries],
        "summary": summary,
    }


# -- internals ----------------------------------------------------------------


def _distinct_values(observations: list[dict]) -> dict[str, list[dict]]:
    distinct: dict[str, list[dict]] = {}
    for obs in observations:
        distinct.setdefault(obs["value"], []).append(obs)
    return distinct


def _domains_per_value(distinct: dict[str, list[dict]]) -> dict[str, set[str]]:
    return {
        value: {d for d in (observation_domain(o["source_url"])
                            for o in obs_list) if d}
        for value, obs_list in distinct.items()
    }


def _outcome(distinct: dict[str, list[dict]],
             domains: dict[str, set[str]]) -> str:
    if len(distinct) > 1:
        return CONFLICTING
    if len(distinct) == 1:
        value = next(iter(distinct))
        if len(domains[value]) >= 2:
            return CORROBORATED
        return SINGLE_SOURCE
    return UNOBSERVED


def _strength(observation: dict) -> int:
    return SOURCE_STRENGTH.get(observation["source_type"], 0)


def _annotate(value: str, observations: list[dict]) -> dict:
    strongest = max(observations, key=lambda o: (
        _strength(o), o["discovered_at"]))
    domains = sorted({d for d in (observation_domain(o["source_url"])
                                  for o in observations) if d})
    return {
        "value": value,
        "sources": [o["source_url"] for o in observations
                    if o["source_url"]],
        "source_types": sorted({o["source_type"] for o in observations
                                if o["source_type"]}),
        "first_discovered_at": min((o["discovered_at"] for o in observations
                                    if o["discovered_at"]), default=""),
        "independent_domains": len(domains),
        "strongest_evidence": {
            "source_url": strongest["source_url"],
            "source_type": strongest["source_type"],
            "strength": _strength(strongest),
            "observed_as": strongest["original_value"],
        },
    }


def _reasons(outcome: str, distinct: dict[str, list[dict]],
             domains: dict[str, set[str]]) -> list[str]:
    if outcome == CORROBORATED:
        value = next(iter(distinct))
        return [f"value corroborated by {len(domains[value])} "
                "independent sources"]
    if outcome == CONFLICTING:
        ordered = sorted(
            ((value, max(_strength(o) for o in obs_list))
             for value, obs_list in distinct.items()),
            key=lambda pair: -pair[1])
        parts = [f"{value!r} (evidence strength {score})"
                 for value, score in ordered]
        return ["sources disagree; both sides preserved: " + "; ".join(parts),
                "no automatic winner chosen — resolution requires human or "
                "newer evidence"]
    if outcome == SINGLE_SOURCE:
        return ["only one independent source observed this value"]
    return ["no provenance-bearing evidence found"]
