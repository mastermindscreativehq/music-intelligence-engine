"""Persistence service for radio intelligence (Phase 4 storage boundary).

Responsibility — and nothing more:

    intelligence result (dicts already produced by Phase 3)
        ↓ validation
        ↓ normalization
        ↓ deterministic upsert
        ↓ stored record

The service NEVER discovers, crawls, verifies, or invents intelligence. It
never mutates its inputs (records are deep-copied on arrival). Repeated
ingestion of the same intelligence is safe: station rows are keyed by the
same identity used during discovery deduplication
(`enrichment.dedupe.identity_key`), contacts by a content hash of their
stable business fields, so re-ingesting updates rather than duplicates.

FACT / INFERENCE / UNKNOWN handling:

- Email/phone Facts are stored verbatim (provenance intact);
- the submission payload is stored verbatim including its inference-labeled
  methods bundle — storage never promotes inference to fact;
- absent evidence remains NULL/missing; NULLs never erase known values on
  merge (an incoming None keeps the previously stored value).

Read-side methods exist only to serve the backend API contract; they contain
no business logic beyond decoding stored JSON columns.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sqlite3
import sys
import threading
import uuid
from dataclasses import dataclass, field

from crawler.urls import canonical_domain

from database.schema import SCHEMA_VERSION, apply_migrations

from discovery.events import (
    get_logger,
    log_event,
)

from discovery.models import utc_now_iso
from enrichment.dedupe import identity_key

EVENT_INGESTION_STARTED = "ingestion_started"
EVENT_RECORD_STORED = "record_stored"
EVENT_RECORD_REJECTED = "record_rejected"
EVENT_INGESTION_COMPLETED = "ingestion_completed"

_JSON_LIST_FIELDS = (
    "alternate_names", "classification_evidence", "formats", "genres",
    "source_urls", "confidence_reasons",
)
_JSON_DICT_FIELDS = ("genre_evidence", "social_urls", "raw_metadata")

# Development-fixture quarantine (read path only; storage is never mutated).
# A row is a dev artifact when its identity is a geo-name seed, or when its
# host lives on a reserved-TLD suffix (.example/.test/.invalid/.localhost/
# .local) or is absent. Such rows never surface in the production listing.
_DEV_HOST_SUFFIXES = (".example", ".test", ".invalid", ".localhost", ".local")
_DEV_HOST = "LOWER(COALESCE(NULLIF(domain, ''), NULLIF(website, '')))"
_DEV_FIXTURE_EXCLUSION_SQL = (
    "identity_key NOT LIKE 'namegeo:%' AND "
    + " AND ".join(
        f"{_DEV_HOST} IS NOT NULL AND {_DEV_HOST} NOT LIKE '%{suffix}' "
        "ESCAPE '\\'"
        for suffix in _DEV_HOST_SUFFIXES))
_FLOAT_UNIT_FIELDS = ("confidence_score", "classification_confidence")


def _dev_reserved_tld(expr: str, wildcard: str = "%") -> str:
    """SQL fragment: ``expr`` contains a reserved-TLD host suffix.

    NULLs normalise to an empty string so the marker is strictly FALSE (a
    NULL result would propagate through ``OR`` and hide every row, including
    legitimate ones). Works identically against SQLite (``wildcard="%"``)
    and PostgreSQL (``wildcard="%%"``, because psycopg parses ``%`` as a
    placeholder).
    """
    return "(" + " OR ".join(
        f"LOWER(COALESCE(CAST({expr} AS TEXT), '')) "
        f"LIKE '{wildcard}{suffix}{wildcard}'"
        for suffix in _DEV_HOST_SUFFIXES) + ")"


def _dj_dev_fixture_exclusion_sql(wildcard: str = "%") -> str:
    """Read-path quarantine for DJ rows (mirrors ``_DEV_FIXTURE_EXCLUSION_SQL``).

    A DJ is a dev artifact when any source-backed channel (value/source_url),
    the stored source_urls, or the identity name carries a reserved-TLD
    suffix or an unmistakable test marker (``test-dj-``, ``e2e``). Storage is
    never mutated — the row simply never surfaces in the production listing.
    """
    channel_value = _dev_reserved_tld("dc.value", wildcard)
    channel_source = _dev_reserved_tld("dc.source_url", wildcard)
    source_urls = _dev_reserved_tld("djs.source_urls", wildcard)
    return (
        "NOT ("
        f"EXISTS (SELECT 1 FROM dj_channels dc "
        f"WHERE dc.dj_id = djs.dj_id AND ({channel_value} OR {channel_source})) "
        f"OR {source_urls} "
        f"OR LOWER(COALESCE(djs.name, '')) LIKE 'test-dj-{wildcard}' "
        f"OR LOWER(COALESCE(djs.name, '')) LIKE '{wildcard} e2e {wildcard}' "
        f"OR LOWER(COALESCE(djs.stage_name, '')) LIKE 'test-dj-{wildcard}')")


def _outreach_dev_fixture_exclusion_sql(wildcard: str = "%") -> str:
    """Read-path quarantine for outreach rows (mirrors the station rule).

    An outreach row is a dev artifact when any destination/identity field
    carries a reserved-TLD suffix or an unmistakable test marker (an ``e2e``
    body/recipient, or a ``test-dj-`` recipient/organization). Storage is
    never mutated — the row simply never surfaces in the production listing.
    """
    email = _dev_reserved_tld("email", wildcard)
    source = _dev_reserved_tld("source_url", wildcard)
    submission = _dev_reserved_tld("submission_url", wildcard)
    body = ("LOWER(COALESCE(subject, '') || ' ' || "
            "COALESCE(message, '') || ' ' || COALESCE(recipient_name, ''))")
    return (
        "NOT ("
        f"{email} OR {source} OR {submission} "
        f"OR ({body} LIKE '{wildcard} e2e {wildcard}' AND "
        f"{body} LIKE '{wildcard}test{wildcard}') "
        f"OR LOWER(COALESCE(recipient_name, '')) LIKE 'test-dj-{wildcard}' "
        f"OR LOWER(COALESCE(organization, '')) LIKE 'test-dj-{wildcard}')")


_DEV_DJ_FIXTURE_EXCLUSION_SQL = _dj_dev_fixture_exclusion_sql("%")
_DEV_OUTREACH_FIXTURE_EXCLUSION_SQL = _outreach_dev_fixture_exclusion_sql("%")

# Read-path exclusion for DJs deterministically classified as NOT a DJ
# (rejected once, hidden from the normal listing forever).
_DJ_REJECTED_SQL = (
    "COALESCE(json_extract(verification, '$.classification.verdict'), '') "
    "= 'rejected'")


class ValidationError(Exception):
    """Raised per-record during validation; captured into the report."""


@dataclass
class IngestionReport:
    run_id: str
    source: str
    started_at: str
    completed_at: str | None = None
    records_accepted: int = 0
    records_failed: int = 0
    stations_upserted: int = 0
    contacts_upserted: int = 0
    submissions_stored: int = 0
    failures: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "source": self.source,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "records_accepted": self.records_accepted,
            "records_failed": self.records_failed,
            "stations_upserted": self.stations_upserted,
            "contacts_upserted": self.contacts_upserted,
            "submissions_stored": self.submissions_stored,
            "failures": [dict(f) for f in self.failures],
        }


def validate_intelligence_record(record: object) -> dict:
    """Structural validation; returns the record unchanged or raises.

    Only shape/type/range checks happen here — no value invention, no
    coercion beyond what normalization does afterwards.
    """
    if not isinstance(record, dict):
        raise ValidationError("record must be a JSON object")
    name = record.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("'name' must be a non-empty string")
    org_type = record.get("organization_type")
    if org_type is not None and org_type != "radio_station":
        raise ValidationError(
            f"unsupported organization_type {org_type!r}; "
            f"Phase 4 persists radio stations only")
    website = record.get("website")
    if website is not None and (
            not isinstance(website, str)
            or not website.startswith(("http://", "https://"))):
        raise ValidationError("'website' must be an http(s) URL or null")
    for field_name in _FLOAT_UNIT_FIELDS:
        value = record.get(field_name)
        if value is not None and (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not 0.0 <= float(value) <= 1.0):
            raise ValidationError(
                f"'{field_name}' must be a number in [0, 1] or null")
    for list_field in ("emails", "phone_numbers", "contacts", "source_urls",
                       "fetches"):
        value = record.get(list_field)
        if value is not None and not isinstance(value, list):
            raise ValidationError(f"'{list_field}' must be a list")
    for contact in record.get("contacts") or []:
        if not isinstance(contact, dict):
            raise ValidationError("every contact must be a JSON object")
        cscore = contact.get("confidence_score")
        if cscore is not None and (
                not isinstance(cscore, (int, float))
                or isinstance(cscore, bool)
                or not 0.0 <= float(cscore) <= 1.0):
            raise ValidationError(
                "contact 'confidence_score' must be a number in [0, 1]")
    submission = record.get("submission")
    if submission is not None and not isinstance(submission, dict):
        raise ValidationError("'submission' must be a JSON object or null")
    return record


def normalize_intelligence_record(record: dict) -> tuple[dict, str, str]:
    """Return (normalized_copy, identity_key, identity_kind).

    Works on a deep copy; the caller's dict is never modified. Missing
    optional fields stay missing — normalization never invents values.
    """
    clean = copy.deepcopy(record)
    kind, value = identity_key(clean)          # shared with discovery dedupe
    stable_id = f"{kind}:{value}"

    if clean.get("domain") is None and clean.get("website"):
        try:
            clean["domain"] = canonical_domain(clean["website"])
        except ValueError:
            clean["domain"] = None

    for field_name in _JSON_LIST_FIELDS:
        clean[field_name] = list(clean.get(field_name) or [])
    for field_name in _JSON_DICT_FIELDS:
        clean[field_name] = dict(clean.get(field_name) or {})
    for field_name in _FLOAT_UNIT_FIELDS:
        if clean.get(field_name) is not None:
            clean[field_name] = round(float(clean[field_name]), 2)

    for contact in clean.get("contacts") or []:
        if contact.get("confidence_score") is not None:
            contact["confidence_score"] = round(
                float(contact["confidence_score"]), 2)
        contact["provenance"] = list(contact.get("provenance") or [])
        contact["confidence_reasons"] = list(
            contact.get("confidence_reasons") or [])
    return clean, stable_id, kind


def contact_uid(identity: str, contact: dict) -> str:
    """Deterministic content identity for one contact row."""
    name = str(contact.get("name") or "")
    name = name.replace("\xa0", " ").replace("\u200b", "")
    parts = [
        identity,
        str(contact.get("email") or "").strip().lower(),
        str(contact.get("phone") or "").strip(),
        " ".join(name.split()).strip().lower().rstrip(",."),
        str(contact.get("role") or ""),
        str(contact.get("source_url") or "").strip(),
    ]
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest[:32]


def _merge_list(existing, incoming) -> list:
    out = list(existing or [])
    for item in incoming or []:
        if item not in out:
            out.append(item)
    return out


def _merge_dict(existing, incoming) -> dict:
    out = dict(existing or {})
    for key, value in (incoming or {}).items():
        out.setdefault(key, value)
    return out


class PersistenceService:
    """SQLite-backed storage boundary over produced intelligence."""

    def __init__(self, db_path: str, logger=None) -> None:
        self.db_path = db_path
        self.logger = logger or get_logger("mie.storage")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self.version = apply_migrations(self._conn)

    # -- ingestion -----------------------------------------------------------

    def ingest_intelligence(self, records: list[dict], *,
                            source: str = "api") -> IngestionReport:
        """Persist already-produced intelligence records; idempotent."""
        if not isinstance(records, list):
            raise TypeError("records must be a list of intelligence dicts")
        report = IngestionReport(
            run_id=str(uuid.uuid4()),
            source=str(source),
            started_at=utc_now_iso(),
        )
        log_event(self.logger, EVENT_INGESTION_STARTED,
                  run_id=report.run_id, count=len(records), source=source)
        with self._lock:
            self._conn.execute(
                "INSERT INTO ingestion_runs(run_id, source, started_at) "
                "VALUES (?, ?, ?)",
                (report.run_id, report.source, report.started_at))
            for position, raw in enumerate(records):
                try:
                    self._ingest_one(validate_intelligence_record(raw),
                                     report)
                    report.records_accepted += 1
                except Exception as exc:
                    report.records_failed += 1
                    failure = {
                        "stage": "validation" if isinstance(exc, ValidationError)
                        else "storage",
                        "error_kind": type(exc).__name__,
                        "message": str(exc),
                        "url": (raw.get("website")
                                if isinstance(raw, dict) else None),
                        "position": position,
                    }
                    report.failures.append(failure)
                    log_event(self.logger, EVENT_RECORD_REJECTED,
                              run_id=report.run_id,
                              reason=f"{failure['error_kind']}: "
                                     f"{failure['message']}")
            self._conn.execute(
                "UPDATE ingestion_runs SET completed_at=?, "
                "records_accepted=?, records_failed=? WHERE run_id=?",
                (utc_now_iso(), report.records_accepted,
                 report.records_failed, report.run_id))
        report.completed_at = utc_now_iso()
        log_event(self.logger, EVENT_INGESTION_COMPLETED,
                  run_id=report.run_id,
                  accepted=report.records_accepted,
                  failed=report.records_failed)
        return report

    def _ingest_one(self, record: dict, report: IngestionReport) -> None:
        clean, stable_id, kind = normalize_intelligence_record(record)
        now = utc_now_iso()
        with self._conn:   # one transaction per record
            row = self._conn.execute(
                "SELECT * FROM stations WHERE identity_key=?",
                (stable_id,)).fetchone()
            # Decode stored JSON columns before merging: _merge_station_row
            # unions lists/dicts, which requires decoded values, not raw
            # serialized TEXT from the row.
            existing = self._station_from_row(row) if row else None
            merged = self._merge_station_row(existing, clean, now)
            self._upsert_station(stable_id, kind, merged)
            self._sync_facts(stable_id, clean.get("emails") or [],
                             table="station_emails")
            self._sync_facts(stable_id, clean.get("phone_numbers") or [],
                             table="station_phones")
            report.contacts_upserted += self._upsert_contacts(
                stable_id, clean.get("contacts") or [], now)
            if clean.get("submission") is not None:
                self._upsert_submission(stable_id, clean["submission"], now)
                report.submissions_stored += 1
            self._replace_fetches(stable_id, clean.get("fetches") or [])
        report.stations_upserted += 1
        log_event(self.logger, EVENT_RECORD_STORED,
                  run_id=report.run_id, station=stable_id,
                  contacts=len(clean.get("contacts") or []))

    @staticmethod
    def _merge_station_row(existing, incoming: dict, now: str) -> dict:
        """Merge policy: newest non-null scalar wins; lists/dicts union;
        earliest discovery & first storage, latest observation kept.

        The merge iterates the UNION of old and incoming keys so a field
        absent from an incoming record is treated like null (kept, never
        erased) rather than dropped from the UPDATE.
        """
        if existing is None:
            merged = dict(incoming)
            merged["first_stored_at"] = now
            merged["last_stored_at"] = now
            return merged
        old = dict(existing)
        merged: dict = {}
        for key in set(old.keys()) | set(incoming.keys()):
            if key in ("first_stored_at", "last_stored_at"):
                continue
            previous = old.get(key)
            value = incoming.get(key) if key in incoming else None
            if key == "discovered_at":
                candidates = [v for v in (previous, value) if v]
                merged[key] = min(candidates) if candidates else None
            elif key in ("last_observed_at", "last_verified_at"):
                candidates = [v for v in (previous, value) if v]
                merged[key] = max(candidates) if candidates else None
            elif key in ("source_urls", "genres", "formats",
                         "confidence_reasons", "classification_evidence"):
                merged[key] = _merge_list(previous, value)
            elif key in ("social_urls", "genre_evidence"):
                merged[key] = _merge_dict(previous, value)
            elif key == "raw_metadata":
                fresh = dict(previous or {})
                fresh.update(value or {})
                merged[key] = fresh
            else:
                # Ingest evidence is additive and NULL-only: an existing
                # fact is NEVER overwritten — re-enrichment only fills
                # still-empty columns. ``status`` is workflow state (not a
                # stored fact) so it may still progress (new -> enriched).
                if key == "status":
                    merged[key] = value if value is not None else previous
                else:
                    merged[key] = previous if previous is not None else value
        merged["first_stored_at"] = old["first_stored_at"]
        merged["last_stored_at"] = now
        return merged

    def _upsert_station(self, stable_id: str, kind: str, row: dict) -> None:
        self._conn.execute(
            """
            INSERT INTO stations (
                identity_key, identity_kind, name, organization_type,
                website, domain, country, state_or_region, city, market_area,
                station_type, classification_confidence,
                classification_evidence, formats, genres, genre_evidence,
                language, description, social_urls, source_urls,
                discovered_at, last_verified_at, last_observed_at,
                confidence_score, confidence_reasons, status, raw_metadata,
                first_stored_at, last_stored_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(identity_key) DO UPDATE SET
                identity_kind=excluded.identity_kind,
                name=excluded.name,
                organization_type=excluded.organization_type,
                website=excluded.website,
                domain=excluded.domain,
                country=excluded.country,
                state_or_region=excluded.state_or_region,
                city=excluded.city,
                market_area=excluded.market_area,
                station_type=excluded.station_type,
                classification_confidence=excluded.classification_confidence,
                classification_evidence=excluded.classification_evidence,
                formats=excluded.formats,
                genres=excluded.genres,
                genre_evidence=excluded.genre_evidence,
                language=excluded.language,
                description=excluded.description,
                social_urls=excluded.social_urls,
                source_urls=excluded.source_urls,
                discovered_at=excluded.discovered_at,
                last_verified_at=excluded.last_verified_at,
                last_observed_at=excluded.last_observed_at,
                confidence_score=excluded.confidence_score,
                confidence_reasons=excluded.confidence_reasons,
                status=excluded.status,
                raw_metadata=excluded.raw_metadata,
                last_stored_at=excluded.last_stored_at
            """,
            (
                stable_id, kind, row.get("name"),
                row.get("organization_type"), row.get("website"),
                row.get("domain"), row.get("country"),
                row.get("state_or_region"), row.get("city"),
                row.get("market_area"), row.get("station_type"),
                row.get("classification_confidence"),
                _dumps(row.get("classification_evidence")),
                _dumps(row.get("formats")), _dumps(row.get("genres")),
                _dumps(row.get("genre_evidence")), row.get("language"),
                row.get("description"), _dumps(row.get("social_urls")),
                _dumps(row.get("source_urls")), row.get("discovered_at"),
                row.get("last_verified_at"), row.get("last_observed_at"),
                row.get("confidence_score"),
                _dumps(row.get("confidence_reasons")), row.get("status"),
                _dumps(row.get("raw_metadata")), row["first_stored_at"],
                row["last_stored_at"],
            ))

    def _sync_facts(self, stable_id: str, facts: list[dict],
                    table: str) -> None:
        for fact in facts:
            value = fact.get("value")
            if value is None:
                continue
            self._conn.execute(
                f"INSERT INTO {table}(identity_key, value, fact) "
                f"VALUES (?, ?, ?) "
                f"ON CONFLICT(identity_key, value) DO UPDATE SET "
                f"fact=excluded.fact",
                (stable_id, str(value), _dumps(fact)))

    def _upsert_contacts(self, stable_id: str, contacts: list[dict],
                         now: str) -> int:
        count = 0
        for contact in contacts:
            uid = contact_uid(stable_id, contact)
            existing = self._conn.execute(
                "SELECT provenance, first_stored_at FROM contacts "
                "WHERE contact_uid=?", (uid,)).fetchone()
            provenance = _dumps(_merge_provenance(
                _loads(existing["provenance"], default=[]) if existing else [],
                contact.get("provenance") or []))
            first_stored = existing["first_stored_at"] if existing else now
            self._conn.execute(
                """
                INSERT INTO contacts (
                    contact_uid, identity_key, engine_contact_id, name, role,
                    email, phone, source_url, confidence_score,
                    confidence_reasons, preferred_for_submissions,
                    verified_at, provenance, first_stored_at, last_stored_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(contact_uid) DO UPDATE SET
                    engine_contact_id=excluded.engine_contact_id,
                    name=excluded.name,
                    role=excluded.role,
                    email=excluded.email,
                    phone=excluded.phone,
                    source_url=excluded.source_url,
                    confidence_score=excluded.confidence_score,
                    confidence_reasons=excluded.confidence_reasons,
                    preferred_for_submissions=
                        excluded.preferred_for_submissions,
                    verified_at=COALESCE(excluded.verified_at,
                                         contacts.verified_at),
                    provenance=excluded.provenance,
                    last_stored_at=excluded.last_stored_at
                """,
                (
                    uid, stable_id, str(contact.get("id") or "") or None,
                    contact.get("name"), contact.get("role"),
                    contact.get("email"), contact.get("phone"),
                    contact.get("source_url"), contact.get("confidence_score"),
                    _dumps(contact.get("confidence_reasons")),
                    1 if contact.get("preferred_for_submissions") else 0,
                    contact.get("verified_at"), provenance,
                    first_stored, now,
                ))
            count += 1
        # Reconciliation preserves stored facts: an intake delivers the
        # COMPLETE, freshly enriched contact set, so any contact_uid already
        # stored for this station that is NOT in the fresh result is stale
        # (superseded extraction, garbage from older code, a DJ-list flood)
        # and is removed — but ONLY when the fresh set is non-empty. A
        # partial/empty intake (fetch failure, robots-blocked page, budget
        # limit) must NEVER erase previously stored contact facts; if the
        # intake genuinely has zero contacts it simply leaves the stored set
        # untouched. Mirrors PostgresStorage._upsert_contacts exactly.
        incoming_uids = {contact_uid(stable_id, c) for c in contacts}
        if incoming_uids:
            marks = ",".join(["?"] * len(incoming_uids))
            self._conn.execute(
                f"DELETE FROM contacts WHERE identity_key=? "
                f"AND contact_uid NOT IN ({marks})",
                [stable_id, *sorted(incoming_uids)])
        return count

    def _upsert_submission(self, stable_id: str, payload: dict,
                           now: str) -> None:
        existing = self._conn.execute(
            "SELECT first_stored_at FROM submission_paths WHERE identity_key=?",
            (stable_id,)).fetchone()
        first_stored = existing["first_stored_at"] if existing else now
        self._conn.execute(
            "INSERT OR REPLACE INTO submission_paths "
            "(identity_key, payload, first_stored_at, last_stored_at) "
            "VALUES (?, ?, ?, ?)",
            (stable_id, _dumps(payload), first_stored, now))

    def _replace_fetches(self, stable_id: str, fetches: list[dict]) -> None:
        self._conn.execute(
            "DELETE FROM source_fetches WHERE identity_key=?", (stable_id,))
        for fetch in fetches:
            self._conn.execute(
                "INSERT INTO source_fetches(identity_key, url, ok, status, "
                "error_kind, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
                (stable_id, fetch.get("url"),
                 1 if fetch.get("ok") else 0, fetch.get("status"),
                 fetch.get("error_kind"), fetch.get("fetched_at")))

    # -- read side (API support) ---------------------------------------------

    def list_stations(self, limit: int = 50, offset: int = 0,
                      q: str | None = None,
                      status: str | None = None,
                      genre: str | None = None,
                      format_filter: str | None = None,
                      country: str | None = None,
                      min_confidence: float | None = None,
                      exclude_dev: bool = False
                      ) -> tuple[list[dict], int] | tuple[list[dict], int, int]:
        clauses, params = [], []
        if q:
            clauses.append("name LIKE ? ESCAPE '\\'")
            params.append("%" + _like_escape(q) + "%")
        if status:
            clauses.append("status = ?")
            params.append(status)
        # Phase 6 additive filters (Phase 7 preview). JSON-array columns are
        # matched as text substrings — portable across SQLite and PostgreSQL.
        if genre:
            clauses.append("genres LIKE ? ESCAPE '\\'")
            params.append("%" + _like_escape(genre) + "%")
        if format_filter:
            clauses.append("formats LIKE ? ESCAPE '\\'")
            params.append("%" + _like_escape(format_filter) + "%")
        if country:
            clauses.append("country = ?")
            params.append(country)
        if min_confidence is not None:
            clauses.append("confidence_score >= ?")
            params.append(float(min_confidence))
        base_where = " AND ".join(clauses)
        where = f"WHERE {base_where}" if base_where else ""
        if exclude_dev:
            dev_clause = "(" + _DEV_FIXTURE_EXCLUSION_SQL + ")"
            dev_where = (f"WHERE {base_where} AND {dev_clause}"
                         if base_where else f"WHERE {dev_clause}")
            with self._lock:
                visible_total = self._conn.execute(
                    f"SELECT COUNT(*) AS n FROM stations {dev_where}",
                    params).fetchone()["n"]
                full_total = self._conn.execute(
                    f"SELECT COUNT(*) AS n FROM stations {where}",
                    params).fetchone()["n"]
                rows = self._conn.execute(
                    f"SELECT * FROM stations {dev_where} "
                    "ORDER BY name COLLATE NOCASE, identity_key "
                    "LIMIT ? OFFSET ?",
                    [*params, int(limit), int(offset)]).fetchall()
            return ([self._station_from_row(r) for r in rows],
                    int(visible_total), int(full_total - visible_total))
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM stations {where}",
                params).fetchone()["n"]
            rows = self._conn.execute(
                f"SELECT * FROM stations {where} "
                "ORDER BY name COLLATE NOCASE, identity_key "
                "LIMIT ? OFFSET ?",
                [*params, int(limit), int(offset)]).fetchall()
        return [self._station_from_row(r) for r in rows], int(total)

    def get_station(self, identity_key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM stations WHERE identity_key=?",
                (identity_key,)).fetchone()
        return self._station_from_row(row) if row else None

    def get_station_emails(self, identity_key: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT fact FROM station_emails WHERE identity_key=? "
                "ORDER BY value", (identity_key,)).fetchall()
        return [_loads(r["fact"], default={}) for r in rows]

    def get_station_phones(self, identity_key: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT fact FROM station_phones WHERE identity_key=? "
                "ORDER BY value", (identity_key,)).fetchall()
        return [_loads(r["fact"], default={}) for r in rows]

    def get_station_contacts(self, identity_key: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM contacts WHERE identity_key=? "
                "ORDER BY role, name COLLATE NOCASE",
                (identity_key,)).fetchall()
        return [self._contact_from_row(r) for r in rows]

    def get_submission(self, identity_key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM submission_paths WHERE identity_key=?",
                (identity_key,)).fetchone()
        return _loads(row["payload"], default=None) if row else None

    def get_fetches(self, identity_key: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT url, ok, status, error_kind, fetched_at "
                "FROM source_fetches WHERE identity_key=? ORDER BY fetch_id",
                (identity_key,)).fetchall()
        return [{
            "url": r["url"], "ok": bool(r["ok"]), "status": r["status"],
            "error_kind": r["error_kind"], "fetched_at": r["fetched_at"],
        } for r in rows]

    # -- verification persistence (Phase 6, append-only) ----------------------

    def persist_verification(self, records: list[dict], report: dict, *,
                             source: str = "api") -> dict:
        """Persist a ``verify_records()`` report next to its source records.

        Append-only: every run inserts new history rows; nothing is
        updated or deleted. Results whose station is unknown to storage
        are skipped and counted — verification never creates stations.
        """
        if not isinstance(report, dict) \
                or not isinstance(report.get("records"), list):
            raise TypeError("report must be a verify_records() report dict")
        run_id = str(uuid.uuid4())
        stored = skipped = 0
        dict_records = [r for r in records if isinstance(r, dict)]
        with self._lock:
            self._conn.execute(
                "INSERT INTO verification_runs(run_id, started_at, "
                "completed_at, summary, source) VALUES (?, ?, ?, ?, ?)",
                (run_id, str(report.get("started_at") or utc_now_iso()),
                 str(report.get("completed_at") or ""),
                 _dumps(report.get("summary") or {}), str(source)))
            for entry, record in zip(report["records"], dict_records):
                try:
                    _, stable_id, _ = normalize_intelligence_record(record)
                except Exception:
                    skipped += len(entry.get("results") or [])
                    continue
                known = self._conn.execute(
                    "SELECT 1 FROM stations WHERE identity_key=?",
                    (stable_id,)).fetchone()
                if not known:
                    skipped += len(entry.get("results") or [])
                    continue
                for result in entry.get("results") or []:
                    self._conn.execute(
                        "INSERT INTO verification_results(run_id, "
                        "identity_key, claim, status, method, verifier, "
                        "evidence, reasons, checked_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (run_id, stable_id, str(result.get("claim")),
                         str(result.get("status")), result.get("method"),
                         result.get("verifier"),
                         _dumps(result.get("evidence") or []),
                         _dumps(result.get("reasons") or []),
                         str(result.get("checked_at") or "")))
                    stored += 1
            self._conn.execute(
                "UPDATE verification_runs SET completed_at=? WHERE run_id=?",
                (str(report.get("completed_at") or utc_now_iso()), run_id))
        return {"run_id": run_id, "stored": stored, "skipped": skipped}

    def get_verification(self, identity_key: str) -> dict | None:
        """Append-only verification history for one station."""
        with self._lock:
            run_rows = self._conn.execute(
                "SELECT * FROM verification_runs WHERE run_id IN "
                "(SELECT DISTINCT run_id FROM verification_results "
                "WHERE identity_key=?) ORDER BY started_at DESC",
                (identity_key,)).fetchall()
            result_rows = self._conn.execute(
                "SELECT * FROM verification_results WHERE identity_key=? "
                "ORDER BY checked_at DESC, result_id DESC",
                (identity_key,)).fetchall()
        if not run_rows:
            return None
        return {
            "runs": [{
                "run_id": r["run_id"],
                "started_at": r["started_at"],
                "completed_at": r["completed_at"],
                "summary": _loads(r["summary"], default={}),
                "source": r["source"],
            } for r in run_rows],
            "results": [{
                "claim": r["claim"], "status": r["status"],
                "method": r["method"], "verifier": r["verifier"],
                "evidence": _loads(r["evidence"], default=[]),
                "reasons": _loads(r["reasons"], default=[]),
                "checked_at": r["checked_at"], "run_id": r["run_id"],
            } for r in result_rows],
        }

    def get_ingestion_run(self, run_id: str) -> dict | None:
        """One ingestion run with its failure ledger (or None)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM ingestion_runs WHERE run_id=?",
                (run_id,)).fetchone()
            if not row:
                return None
            failures = self._conn.execute(
                "SELECT stage, error_kind, message, url FROM "
                "ingestion_failures WHERE run_id=? ORDER BY failure_id",
                (run_id,)).fetchall()
        return {
            "run_id": row["run_id"], "source": row["source"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "records_accepted": row["records_accepted"],
            "records_failed": row["records_failed"],
            "failures": [dict(f) for f in failures],
        }

    # -- Phase 11: automation discovery jobs ----------------------------------

    def record_discovery_job(self, report: dict) -> str:
        """Persist one automation discovery job run; returns its run_id.

        The report dict is the same shape the discovery job endpoint returns,
        so stored metadata and API responses stay in sync. Timestamps and
        counters are coerced defensively — a job that failed before running
        still gets an honest row.
        """
        now = utc_now_iso()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO discovery_jobs (
                    run_id, organization_type, config, provider, status,
                    queries_run, candidates_found, records_ingested,
                    duplicates, failures, error_message, started_at,
                    completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (str(report["run_id"]),
                 str(report.get("organization_type") or "dj"),
                 _dumps(report.get("config") or {}),
                 report.get("provider"),
                 str(report.get("status") or "failed"),
                 int(report.get("queries_run") or 0),
                 int(report.get("candidates_found") or 0),
                 int(report.get("records_ingested") or 0),
                 int(report.get("duplicates") or 0),
                 int(report.get("failures") or 0),
                 report.get("error"),
                 str(report.get("started_at") or now),
                 str(report.get("completed_at") or "")))
        return str(report["run_id"])

    def get_discovery_job(self, run_id: str) -> dict | None:
        """One stored automation discovery job run (or None)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM discovery_jobs WHERE run_id=?",
                (run_id,)).fetchone()
        if not row:
            return None
        return {
            "run_id": row["run_id"],
            "organization_type": row["organization_type"],
            "config": _loads(row["config"], default={}),
            "provider": row["provider"],
            "status": row["status"],
            "queries_run": int(row["queries_run"]),
            "candidates_found": int(row["candidates_found"]),
            "records_ingested": int(row["records_ingested"]),
            "duplicates": int(row["duplicates"]),
            "failures": int(row["failures"]),
            "error": row["error_message"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"] or None,
        }

    # -- submission assets + link accessibility (Phase 8) ----------------------
    # track_id ('sha256:<hex>') is the only asset identifier at this
    # boundary; storage locations are owned by the submissions storage
    # backend and NEVER persisted or returned here.

    @staticmethod
    def _track_from_row(row) -> dict:
        return {
            "track_id": row["track_id"],
            "sha256": row["sha256"],
            "original_filename": row["original_filename"],
            "size_bytes": int(row["size_bytes"]),
            "content_type": row["content_type"],
            "status": row["status"],
            "reject_reason": row["reject_reason"],
            "notes": row["notes"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def save_track(self, track: dict) -> dict:
        """Insert or update one track row; returns the stored projection.

        Mutable fields are status/reject_reason/notes/updated_at;
        created_at survives updates. Unknown statuses are rejected by the
        schema CHECK; callers validate before reaching this layer.
        """
        now = utc_now_iso()
        with self._lock, self._conn:      # commit-on-exit: durable rows
            existing = self._conn.execute(
                "SELECT created_at FROM tracks WHERE track_id=?",
                (track["track_id"],)).fetchone()
            created = existing["created_at"] if existing \
                else str(track.get("created_at") or now)
            self._conn.execute(
                """
                INSERT INTO tracks(track_id, sha256, original_filename,
                                   size_bytes, content_type, status,
                                   reject_reason, notes,
                                   created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(track_id) DO UPDATE SET
                    original_filename=excluded.original_filename,
                    content_type=excluded.content_type,
                    status=excluded.status,
                    reject_reason=excluded.reject_reason,
                    notes=excluded.notes,
                    updated_at=excluded.updated_at
                """,
                (track["track_id"], track["sha256"],
                 track.get("original_filename"), int(track["size_bytes"]),
                 track.get("content_type") or "audio/mpeg",
                 track["status"], track.get("reject_reason"),
                 track.get("notes"), created,
                 str(track.get("updated_at") or now)))
        return self.get_track(track["track_id"])

    def get_track(self, track_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tracks WHERE track_id=?",
                (track_id,)).fetchone()
        return self._track_from_row(row) if row else None

    def list_tracks(self, limit: int = 50, offset: int = 0,
                    status: str | None = None) -> tuple[list[dict], int]:
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM tracks {where}",
                params).fetchone()["n"]
            rows = self._conn.execute(
                f"SELECT * FROM tracks {where} "
                "ORDER BY created_at DESC, track_id LIMIT ? OFFSET ?",
                [*params, int(limit), int(offset)]).fetchall()
        return [self._track_from_row(r) for r in rows], int(total)

    def delete_track(self, track_id: str) -> str | None:
        """Delete one stored asset RECORD; returns the deleted id or None.

        Only the database record is removed — never the uploaded file bytes
        in the asset store (the existing app defines no file-level removal).
        """
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM tracks WHERE track_id=?", (track_id,))
            return track_id if cur.rowcount else None

    def record_link_check(self, identity_key: str, entry: dict) -> None:
        """Append one accessibility check row; history is never rewritten."""
        with self._lock, self._conn:      # commit-on-exit: durable history

            self._conn.execute(
                "INSERT INTO submission_link_checks(identity_key, url, "
                "target_kind, ok, status, error_kind, latency_ms, "
                "checked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (identity_key, entry["url"], entry["target_kind"],
                 1 if entry.get("ok") else 0, entry.get("status"),
                 entry.get("error_kind"), entry.get("latency_ms"),
                 str(entry.get("checked_at") or utc_now_iso())))

    def get_link_checks(self, identity_key: str,
                        limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT url, target_kind, ok, status, error_kind, "
                "latency_ms, checked_at FROM submission_link_checks "
                "WHERE identity_key=? ORDER BY check_id DESC LIMIT ?",
                (identity_key, int(limit))).fetchall()
        return [{
            "url": r["url"], "target_kind": r["target_kind"],
            "ok": bool(r["ok"]), "status": r["status"],
            "error_kind": r["error_kind"], "latency_ms": r["latency_ms"],
            "checked_at": r["checked_at"],
        } for r in rows]

    # -- outreach messages + attempts (Phase 9) --------------------------------

    @staticmethod
    def _outreach_from_row(row) -> dict:
        return {
            "outreach_id": row["outreach_id"],
            "contact_uid": row["contact_uid"],
            "identity_key": row["identity_key"],
            "target_type": row["target_type"] or "station",
            "recipient_name": row["recipient_name"],
            "recipient_role": row["recipient_role"],
            "organization": row["organization"],
            "email": row["email"],
            "source_url": row["source_url"],
            "track_id": row["track_id"],
            "track": _loads(row["track"]),
            "context": _loads(row["context"]),
            "subject": row["subject"],
            "message": row["message"],
            "from_email": row["from_email"],
            "sharing": _loads(row["sharing"]),
            "status": row["status"],
            "provider": row["provider"],
            "outreach_class": row["outreach_class"],
            "submission_url": row["submission_url"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def save_outreach(self, record: dict) -> dict:
        """Insert or overwrite one outreach message row."""
        now = utc_now_iso()
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT created_at FROM outreach_messages WHERE outreach_id=?",
                (record["outreach_id"],)).fetchone()
            created = existing["created_at"] if existing \
                else str(record.get("created_at") or now)
            self._conn.execute(
                """
                INSERT INTO outreach_messages(
                    outreach_id, contact_uid, identity_key,
                    recipient_name, recipient_role, organization,
                    email, source_url, track_id, track, context,
                    subject, message, from_email, sharing,
                    status, provider, created_at, updated_at,
                    outreach_class, submission_url, target_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(outreach_id) DO UPDATE SET
                    contact_uid=excluded.contact_uid,
                    identity_key=excluded.identity_key,
                    recipient_name=excluded.recipient_name,
                    recipient_role=excluded.recipient_role,
                    organization=excluded.organization,
                    email=excluded.email,
                    source_url=excluded.source_url,
                    track_id=excluded.track_id,
                    track=excluded.track,
                    context=excluded.context,
                    subject=excluded.subject,
                    message=excluded.message,
                    from_email=excluded.from_email,
                    sharing=excluded.sharing,
                    status=excluded.status,
                    provider=excluded.provider,
                    outreach_class=excluded.outreach_class,
                    submission_url=excluded.submission_url,
                    target_type=excluded.target_type,
                    updated_at=excluded.updated_at
                """,
                (record["outreach_id"], record.get("contact_uid"),
                 record.get("identity_key"), record.get("recipient_name"),
                 record.get("recipient_role"), record.get("organization"),
                 record["email"], record.get("source_url"),
                 record.get("track_id"), _dumps(record.get("track")),
                 _dumps(record.get("context")), record.get("subject"),
                 record.get("message"), record.get("from_email"),
                 _dumps(record.get("sharing")), record["status"],
                 record.get("provider") or "local", created,
                 str(record.get("updated_at") or now),
                 record.get("outreach_class"),
                 record.get("submission_url"),
                 record.get("target_type") or "station"))
        return self.get_outreach(record["outreach_id"])

    def get_outreach(self, outreach_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM outreach_messages WHERE outreach_id=?",
                (outreach_id,)).fetchone()
        return self._outreach_from_row(row) if row else None

    def list_outreach(self, limit: int = 50, offset: int = 0,
                      status: str | None = None,
                      exclude_dev: bool = False
                      ) -> tuple[list[dict], int] \
            | tuple[list[dict], int, int]:
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        if exclude_dev:
            dev_clause = "(" + _DEV_OUTREACH_FIXTURE_EXCLUSION_SQL + ")"
            dev_where = (f"WHERE {' AND '.join(clauses)} AND {dev_clause}"
                         if clauses else f"WHERE {dev_clause}")
            with self._lock:
                visible_total = self._conn.execute(
                    f"SELECT COUNT(*) AS n FROM outreach_messages {dev_where}",
                    params).fetchone()["n"]
                full_total = self._conn.execute(
                    f"SELECT COUNT(*) AS n FROM outreach_messages {where}",
                    params).fetchone()["n"]
                rows = self._conn.execute(
                    f"SELECT * FROM outreach_messages {dev_where} "
                    "ORDER BY created_at DESC, outreach_id LIMIT ? OFFSET ?",
                    [*params, int(limit), int(offset)]).fetchall()
            return ([self._outreach_from_row(r) for r in rows],
                    int(visible_total), int(full_total - visible_total))
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM outreach_messages {where}",
                params).fetchone()["n"]
            rows = self._conn.execute(
                f"SELECT * FROM outreach_messages {where} "
                "ORDER BY created_at DESC, outreach_id LIMIT ? OFFSET ?",
                [*params, int(limit), int(offset)]).fetchall()
        return [self._outreach_from_row(r) for r in rows], int(total)

    def append_outreach_attempt(self, outreach_id: str,
                                attempt: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO outreach_attempts(outreach_id, event, provider,"
                " at, meta) VALUES (?, ?, ?, ?, ?)",
                (outreach_id, attempt["event"],
                 attempt.get("provider") or "local",
                 str(attempt.get("at") or utc_now_iso()),
                 _dumps(attempt.get("meta"))))

    def set_outreach_status(self, outreach_id: str, status: str,
                            at: str | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE outreach_messages SET status=?, updated_at=? "
                "WHERE outreach_id=?",
                (status, at or utc_now_iso(), outreach_id))

    def get_outreach_attempts(self, outreach_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT event, provider, at, meta FROM outreach_attempts "
                "WHERE outreach_id=? ORDER BY attempt_id ASC",
                (outreach_id,)).fetchall()
        return [{
            "event": r["event"], "provider": r["provider"],
            "at": r["at"], "meta": _loads(r["meta"]),
        } for r in rows]

    def delete_outreach(self, outreach_id: str) -> str | None:
        """Delete one outreach record; returns the deleted id or None.

        The connected schema cascades the delete to the record's attempt
        history (``outreach_attempts`` → ``outreach_messages``); the raw
        station/contact data that produced the record is untouched.
        """
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM outreach_messages WHERE outreach_id=?",
                (outreach_id,))
            return outreach_id if cur.rowcount else None

    # -- Phase 4: DJ intelligence ---------------------------------------------

    def list_djs(self, limit: int = 50, offset: int = 0,
                 q: str | None = None,
                 genre: str | None = None,
                 country: str | None = None,
                 location: str | None = None,
                 station: str | None = None,
                 dj_type: str | None = None,
                 platform: str | None = None,
                 has_contact: bool | None = None,
                 sort: str | None = None,
                 order: str | None = None,
                 exclude_dev: bool = False,
                 exclude_rejected: bool = False
                 ) -> tuple[list[dict], int] | tuple[list[dict], int, int] \
                   | tuple[list[dict], int, int, int]:
        clauses, params = [], []
        if q:
            clauses.append(
                "(name LIKE ? ESCAPE '\\' OR stage_name LIKE ? ESCAPE '\\'"
                " OR program LIKE ? ESCAPE '\\'"
                " OR station_name LIKE ? ESCAPE '\\')")
            like = "%" + _like_escape(q) + "%"
            params.extend([like, like, like, like])
        if genre:
            clauses.append("genres LIKE ? ESCAPE '\\'")
            params.append("%" + _like_escape(genre) + "%")
        if country:
            clauses.append("country = ?")
            params.append(country)
        if location:
            clauses.append(
                "(city LIKE ? ESCAPE '\\' OR state_or_region LIKE ? "
                "ESCAPE '\\')")
            like = "%" + _like_escape(location) + "%"
            params.extend([like, like])
        if station:
            # Station affiliation is optional metadata on an INDEPENDENT DJ
            # record — searching it never adds or implies radio-station links.
            clauses.append(
                "(station_key LIKE ? ESCAPE '\\' OR station_name LIKE ? "
                "ESCAPE '\\')")
            like = "%" + _like_escape(station) + "%"
            params.extend([like, like])
        if dj_type:
            clauses.append("role LIKE ? ESCAPE '\\'")
            params.append("%" + _like_escape(dj_type) + "%")
        if platform:
            clauses.append("platform LIKE ? ESCAPE '\\'")
            params.append("%" + _like_escape(platform) + "%")
        has_contact_sql = (
            "EXISTS (SELECT 1 FROM dj_channels c WHERE c.dj_id = djs.dj_id)")
        if has_contact is True:
            clauses.append(has_contact_sql)
        elif has_contact is False:
            clauses.append("NOT " + has_contact_sql)
        order_by = "name COLLATE NOCASE, dj_id"
        if sort == "station":
            order_by = ("station_name COLLATE NOCASE, name COLLATE NOCASE, "
                        "dj_id")
        elif sort == "discovered":
            order_by = "discovered_at, dj_id"
        direction = "DESC" if order == "desc" else "ASC"
        order_clause = ", ".join(
            f"{col} {direction}" for col in order_by.split(", "))
        where = " AND ".join(clauses)
        where_sql = f"WHERE {where}" if where else ""
        where = " AND ".join(clauses)
        where_sql = f"WHERE {where}" if where else ""
        visible_clauses = list(clauses)
        if exclude_dev:
            visible_clauses.append("(" + _DEV_DJ_FIXTURE_EXCLUSION_SQL + ")")
        if exclude_rejected:
            visible_clauses.append("NOT (" + _DJ_REJECTED_SQL + ")")
        visible_where = (
            "WHERE " + " AND ".join(visible_clauses)
            if visible_clauses else "")

        with self._lock:
            full_total = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM djs {where_sql}",
                params).fetchone()["n"]
            visible_total = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM djs {visible_where}",
                params).fetchone()["n"]
            rows = self._conn.execute(
                f"SELECT * FROM djs {visible_where} "
                f"ORDER BY {order_clause} "
                "LIMIT ? OFFSET ?",
                [*params, int(limit), int(offset)]).fetchall()
            dev_excluded = 0
            if exclude_dev:
                dev_where = (
                    f"WHERE {where} AND NOT ("
                    f"{_DEV_DJ_FIXTURE_EXCLUSION_SQL})"
                    if where
                    else f"WHERE NOT ({_DEV_DJ_FIXTURE_EXCLUSION_SQL})")
                dev_excluded = self._conn.execute(
                    f"SELECT COUNT(*) AS n FROM djs {dev_where}",
                    params).fetchone()["n"]
            rejected_excluded = 0
            if exclude_rejected:
                rej_where = (
                    f"WHERE {where} AND {_DJ_REJECTED_SQL}"
                    if where else f"WHERE {_DJ_REJECTED_SQL}")
                rejected_excluded = self._conn.execute(
                    f"SELECT COUNT(*) AS n FROM djs {rej_where}",
                    params).fetchone()["n"]
        djs = [self._dj_from_row(r) for r in rows]
        self._decorate_dj_contact_flag(djs)
        if exclude_dev and exclude_rejected:
            return (djs, int(visible_total), int(dev_excluded),
                    int(rejected_excluded))
        if exclude_rejected:
            return djs, int(visible_total), int(rejected_excluded)
        if exclude_dev:
            return djs, int(visible_total), int(dev_excluded)
        return djs, int(visible_total)

    def get_dj(self, dj_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM djs WHERE dj_id=?",
                (dj_id,)).fetchone()
        return self._dj_from_row(row) if row else None

    def get_dj_channels(self, dj_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT channel, value, source_url, verified_at "
                "FROM dj_channels WHERE dj_id=? "
                "ORDER BY channel, value", (dj_id,)).fetchall()
        return [self._dj_channel_from_row(r) for r in rows]

    def _decorate_dj_contact_flag(self, djs: list[dict]) -> None:
        """Set ``has_contact`` on each list row (batched, read-path only)."""
        if not djs:
            return
        with self._lock:
            seen = {row["dj_id"] for row in djs}
            batch = ",".join("?" * len(seen))
            rows = self._conn.execute(
                f"SELECT DISTINCT dj_id FROM dj_channels "
                f"WHERE dj_id IN ({batch})", list(seen)).fetchall()
        contact_ids = {row["dj_id"] for row in rows}
        for row in djs:
            row["has_contact"] = row["dj_id"] in contact_ids

    def save_dj(self, record: dict, channels: list[dict] | None = None) -> None:
        """Upsert one DJ row and replace its channel rows (transactional).

        Additive semantics: the DJ row is INSERT OR REPLACE; channel rows
        are delete-then-insert within the same transaction so a re-save
        never accumulates stale facts. Nothing else is touched.
        """
        now = utc_now_iso()
        channels = channels or []
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT first_stored_at FROM djs WHERE dj_id=?",
                (record["dj_id"],)).fetchone()
            first_stored = existing["first_stored_at"] if existing else now
            self._conn.execute(
                """
                INSERT OR REPLACE INTO djs(
                    dj_id, name, stage_name, role, program, station_key,
                    station_name, platform, country, state_or_region, city,
                    genres, formats, source_urls, verification,
                    discovered_at, last_observed_at,
                    first_stored_at, last_stored_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?)
                """,
                (record["dj_id"], record.get("name"),
                 record.get("stage_name"), record.get("role"),
                 record.get("program"), record.get("station_key"),
                 record.get("station_name"), record.get("platform"),
                 record.get("country"), record.get("state_or_region"),
                 record.get("city"), _dumps(record.get("genres") or []),
                 _dumps(record.get("formats") or []),
                 _dumps(record.get("source_urls") or []),
                 _dumps(record.get("verification")),
                 record.get("discovered_at") or now,
                 record.get("last_observed_at") or now,
                 first_stored, str(record.get("last_stored_at") or now)))
            self._conn.execute(
                "DELETE FROM dj_channels WHERE dj_id=?", (record["dj_id"],))
            for channel in channels:
                self._conn.execute(
                    "INSERT INTO dj_channels(dj_id, channel, value, "
                    "source_url, verified_at) VALUES (?, ?, ?, ?, ?)",
                    (record["dj_id"], channel.get("channel"),
                     channel.get("value"), channel.get("source_url"),
                     channel.get("verified_at")))

    def delete_dj(self, dj_id: str) -> str | None:
        """Delete one DJ row (channels cascade); returns the id or None.

        Outreach records created for the DJ are preserved (target_type dj);
        only the DJ profile and its source-backed channels are removed.
        """
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM djs WHERE dj_id=?", (dj_id,))
            return dj_id if cur.rowcount else None

    def update_dj_verification(self, dj_id: str, verification: dict) -> bool:
        """Replace one DJ's ``verification`` JSON; touch ``last_stored_at``.

        Narrow update used by data cleanup to persist classification
        verdicts — no other DJ column is touched. Returns False for an
        unknown dj_id.
        """
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE djs SET verification=?, last_stored_at=? "
                "WHERE dj_id=?",
                (_dumps(verification), utc_now_iso(), dj_id))
            return cur.rowcount > 0

    @staticmethod
    def _dj_from_row(row) -> dict:
        """Decode one DJ row; optional fields that are absent stay None."""
        return {
            "dj_id": row["dj_id"],
            "name": row["name"],
            "stage_name": row["stage_name"],
            "role": row["role"],
            "program": row["program"],
            "station_key": row["station_key"],
            "station_name": row["station_name"],
            "platform": row["platform"],
            "country": row["country"],
            "state_or_region": row["state_or_region"],
            "city": row["city"],
            "genres": _loads(row["genres"]),
            "formats": _loads(row["formats"]),
            "source_urls": _loads(row["source_urls"]),
            "verification": _loads(row["verification"], default={}),
            "discovered_at": row["discovered_at"],
            "last_observed_at": row["last_observed_at"],
            "first_stored_at": row["first_stored_at"],
            "last_stored_at": row["last_stored_at"],
        }

    @staticmethod
    def _dj_channel_from_row(row) -> dict:
        return {
            "channel": row["channel"],
            "value": row["value"],
            "source_url": row["source_url"],
            "verified_at": row["verified_at"],
        }


    # -- row shaping -------------------------------------------------------------

    @staticmethod
    def _station_from_row(row) -> dict:
        data = {
            "identity_key": row["identity_key"],
            "identity_kind": row["identity_kind"],
            "name": row["name"],
            "organization_type": row["organization_type"],
            "website": row["website"],
            "domain": row["domain"],
            "country": row["country"],
            "state_or_region": row["state_or_region"],
            "city": row["city"],
            "market_area": row["market_area"],
            "station_type": row["station_type"],
            "classification_confidence": row["classification_confidence"],
            "classification_evidence": _loads(
                row["classification_evidence"]),
            "formats": _loads(row["formats"]),
            "genres": _loads(row["genres"]),
            "genre_evidence": _loads(row["genre_evidence"], default={}),
            "language": row["language"],
            "description": row["description"],
            "social_urls": _loads(row["social_urls"], default={}),
            "source_urls": _loads(row["source_urls"]),
            "discovered_at": row["discovered_at"],
            "last_verified_at": row["last_verified_at"],
            "last_observed_at": row["last_observed_at"],
            "confidence_score": row["confidence_score"],
            "confidence_reasons": _loads(row["confidence_reasons"]),
            "status": row["status"],
            "raw_metadata": _loads(row["raw_metadata"], default={}),
            "first_stored_at": row["first_stored_at"],
            "last_stored_at": row["last_stored_at"],
        }
        return data

    @staticmethod
    def _contact_from_row(row) -> dict:
        return {
            "contact_uid": row["contact_uid"],
            "engine_contact_id": row["engine_contact_id"],
            "name": row["name"],
            "role": row["role"],
            "email": row["email"],
            "phone": row["phone"],
            "source_url": row["source_url"],
            "confidence_score": row["confidence_score"],
            "confidence_reasons": _loads(row["confidence_reasons"]),
            "preferred_for_submissions": bool(
                row["preferred_for_submissions"]),
            "verified_at": row["verified_at"],
            "provenance": _loads(row["provenance"]),
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ---------------------------------------------------------------------------
# helpers + CLI
# ---------------------------------------------------------------------------

def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=False)


def _loads(raw, default=None):
    if raw is None or raw == "":
        return [] if default is None else default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return [] if default is None else default


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _merge_provenance(existing: list, incoming: list) -> list:
    seen = {json.dumps(p, sort_keys=True) for p in existing}
    merged = list(existing)
    for prov in incoming:
        token = json.dumps(prov, sort_keys=True)
        if token not in seen:
            seen.add(token)
            merged.append(prov)
    return merged


def load_records_file(path: str) -> list[dict]:
    """Accepts EnrichmentResult JSON ({records:[...]}) or a bare array."""
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict) and isinstance(data.get("records"), list):
        return [r for r in data["records"] if isinstance(r, dict)]
    raise ValueError(
        "input must be a JSON array or an object with a 'records' array")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m database.service",
        description="Initialize the storage DB and/or ingest produced "
                    "intelligence JSON.")
    parser.add_argument("--db", required=True, help="SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create/migrate the database, then exit")
    ingest = sub.add_parser("ingest", help="ingest intelligence JSON")
    ingest.add_argument("input", help="path to enrichment output JSON")
    ingest.add_argument("--source", default="cli")
    args = parser.parse_args(argv)

    service = PersistenceService(args.db)
    try:
        if args.command == "init":
            print(json.dumps({"database": "ready",
                              "schema_version": service.version}))
            return 0
        records = load_records_file(args.input)
        report = service.ingest_intelligence(records, source=args.source)
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
        return 0 if report.records_failed == 0 else 1
    finally:
        service.close()


if __name__ == "__main__":
    sys.exit(main())
