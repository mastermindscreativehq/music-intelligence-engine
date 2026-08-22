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
_FLOAT_UNIT_FIELDS = ("confidence_score", "classification_confidence")


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
    parts = [
        identity,
        str(contact.get("email") or "").strip().lower(),
        str(contact.get("phone") or "").strip(),
        str(contact.get("name") or "").strip().lower(),
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
                # Newest evidence wins; incoming None never erases a fact.
                merged[key] = value if value is not None else previous
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
                      status: str | None = None) -> tuple[list[dict], int]:
        clauses, params = [], []
        if q:
            clauses.append("name LIKE ? ESCAPE '\\'")
            params.append("%" + _like_escape(q) + "%")
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
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
