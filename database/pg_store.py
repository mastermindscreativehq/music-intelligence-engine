"""PostgreSQL/Supabase persistence (Phase 6).

Second implementation of the storage surface documented in
``database/repository.py``, mirroring the tested SQLite
``PersistenceService`` semantics exactly:

- SAME validation gate and normalization (imported from database.service);
- SAME merge policy: literally the same functions
  (``PersistenceService._merge_station_row``, ``_merge_provenance``,
  ``contact_uid``), so both backends merge identically by construction;
- SAME payload shapes on every read;
- DIFFERENT only in driver details: ``%s`` placeholders, JSONB columns
  (written as json.dumps text with an explicit ``::jsonb`` cast, returned
  pre-decoded by psycopg's dict_row factory), BOOLEAN instead of INTEGER.

psycopg is imported LAZILY inside :class:`PostgresStorage.__init__`, so
Phases 1-5 modules and their tests never require it. Integration against a
live server is env-gated (MIE_PG_DSN); everything else here is exercised
structurally offline.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import uuid

from discovery.events import get_logger, log_event
from discovery.models import utc_now_iso

from database.schema_migrations import apply_pg_migrations
from database.service import (
    EVENT_INGESTION_COMPLETED,
    EVENT_INGESTION_STARTED,
    EVENT_RECORD_REJECTED,
    EVENT_RECORD_STORED,
    IngestionReport,
    PersistenceService,
    ValidationError,
    _merge_provenance,
    contact_uid,
    load_records_file,
    normalize_intelligence_record,
    validate_intelligence_record,
)

_JSON = frozenset({
    "classification_evidence", "formats", "genres", "genre_evidence",
    "social_urls", "source_urls", "confidence_reasons", "raw_metadata",
})

_ORG_COLUMNS = (
    "identity_kind", "name", "organization_type", "website", "domain",
    "country", "state_or_region", "city", "market_area", "station_type",
    "classification_confidence", "classification_evidence", "formats",
    "genres", "genre_evidence", "language", "description", "social_urls",
    "source_urls", "discovered_at", "last_verified_at", "last_observed_at",
    "confidence_score", "confidence_reasons", "status", "raw_metadata",
    "first_stored_at", "last_stored_at",
)


def _j(value, default):
    """JSONB columns arrive decoded from psycopg; NULL becomes default."""
    return value if value is not None else default


def _dumps(value):
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _org_from_row(row: dict) -> dict:
    """Payload keys identical to PersistenceService._station_from_row."""
    return {
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
        "classification_evidence": _j(row["classification_evidence"], None),
        "formats": _j(row["formats"], None),
        "genres": _j(row["genres"], None),
        "genre_evidence": _j(row["genre_evidence"], {}),
        "language": row["language"],
        "description": row["description"],
        "social_urls": _j(row["social_urls"], {}),
        "source_urls": _j(row["source_urls"], None),
        "discovered_at": row["discovered_at"],
        "last_verified_at": row["last_verified_at"],
        "last_observed_at": row["last_observed_at"],
        "confidence_score": row["confidence_score"],
        "confidence_reasons": _j(row["confidence_reasons"], None),
        "status": row["status"],
        "raw_metadata": _j(row["raw_metadata"], {}),
        "first_stored_at": row["first_stored_at"],
        "last_stored_at": row["last_stored_at"],
    }


def _contact_from_row(row: dict) -> dict:
    return {
        "contact_uid": row["contact_uid"],
        "engine_contact_id": row["engine_contact_id"],
        "name": row["name"],
        "role": row["role"],
        "email": row["email"],
        "phone": row["phone"],
        "source_url": row["source_url"],
        "confidence_score": row["confidence_score"],
        "confidence_reasons": _j(row["confidence_reasons"], None),
        "preferred_for_submissions": bool(row["preferred_for_submissions"]),
        "verified_at": row["verified_at"],
        "provenance": _j(row["provenance"], None),
    }


class PostgresStorage:
    """PostgreSQL backend for the shared intelligence repository surface."""

    def __init__(self, dsn: str | None = None, conn=None, logger=None) -> None:
        self.logger = logger or get_logger("mie.storage.pg")
        self._lock = threading.RLock()
        if conn is not None:            # injectable for structural tests
            self._conn = conn
        else:
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError as exc:  # pragma: no cover - env-dependent
                raise ImportError(
                    "psycopg is required for the PostgreSQL backend "
                    '(pip install "psycopg[binary]"); configure it via '
                    "MIE_PG_DSN or pass an existing connection") from exc
            if not dsn:
                raise ValueError("PostgresStorage requires a DSN")
            self._conn = psycopg.connect(dsn, row_factory=dict_row)
        self.version = apply_pg_migrations(self._conn)

    # -- ingestion -------------------------------------------------------------

    def ingest_intelligence(self, records: list[dict], *,
                            source: str = "api") -> IngestionReport:
        if not isinstance(records, list):
            raise TypeError("records must be a list of intelligence dicts")
        report = IngestionReport(run_id=str(uuid.uuid4()), source=str(source),
                                 started_at=utc_now_iso())
        log_event(self.logger, EVENT_INGESTION_STARTED,
                  run_id=report.run_id, count=len(records), source=source)
        with self._lock:
            with self._conn:
                cur = self._conn.cursor()
                cur.execute(
                    "INSERT INTO ingestion_runs(run_id, source, started_at) "
                    "VALUES (%s, %s, %s)",
                    (report.run_id, report.source, report.started_at))
                for position, raw in enumerate(records):
                    try:
                        self._ingest_one(cur, validate_intelligence_record(raw),
                                         report)
                        report.records_accepted += 1
                    except Exception as exc:
                        report.records_failed += 1
                        report.failures.append({
                            "stage": "validation"
                            if isinstance(exc, ValidationError) else "storage",
                            "error_kind": type(exc).__name__,
                            "message": str(exc),
                            "url": (raw.get("website")
                                    if isinstance(raw, dict) else None),
                            "position": position,
                        })
                        log_event(self.logger, EVENT_RECORD_REJECTED,
                                  run_id=report.run_id,
                                  reason=f"{type(exc).__name__}: {exc}")
                cur.execute(
                    "UPDATE ingestion_runs SET completed_at=%s, "
                    "records_accepted=%s, records_failed=%s WHERE run_id=%s",
                    (utc_now_iso(), report.records_accepted,
                     report.records_failed, report.run_id))
        report.completed_at = utc_now_iso()
        log_event(self.logger, EVENT_INGESTION_COMPLETED,
                  run_id=report.run_id, accepted=report.records_accepted,
                  failed=report.records_failed)
        return report

    def _ingest_one(self, cur, record: dict, report: IngestionReport) -> None:
        clean, stable_id, kind = normalize_intelligence_record(record)
        now = utc_now_iso()
        cur.execute("SELECT * FROM organizations WHERE identity_key=%s",
                    (stable_id,))
        row = cur.fetchone()
        existing = _org_from_row(row) if row else None
        merged = PersistenceService._merge_station_row(existing, clean, now)
        self._upsert_org(cur, stable_id, kind, merged)
        self._sync_facts(cur, stable_id, clean.get("emails") or [],
                         table="organization_emails")
        self._sync_facts(cur, stable_id, clean.get("phone_numbers") or [],
                         table="organization_phones")
        report.contacts_upserted += self._upsert_contacts(
            cur, stable_id, clean.get("contacts") or [], now)
        if clean.get("submission") is not None:
            self._upsert_submission(cur, stable_id, clean["submission"], now)
            report.submissions_stored += 1
        self._replace_fetches(cur, stable_id, clean.get("fetches") or [])
        report.stations_upserted += 1
        log_event(self.logger, EVENT_RECORD_STORED, run_id=report.run_id,
                  station=stable_id,
                  contacts=len(clean.get("contacts") or []))

    def _upsert_org(self, cur, stable_id: str, kind: str, row: dict) -> None:
        """Column-for-column mirror of PersistenceService._upsert_station.

        On conflict every column is overwritten from the merged row except
        identity_key and first_stored_at — exactly like SQLite. ``kind``
        is authoritative for identity_kind (mirrors the SQLite param
        order stable_id, kind, row...).
        """
        row = dict(row)
        row["identity_kind"] = kind
        columns = ["identity_key", *_ORG_COLUMNS]
        params: list = [stable_id]
        for col in _ORG_COLUMNS:
            value = row.get(col)
            params.append(_dumps(value) if col in _JSON else value)
        placeholders = ", ".join(
            "%s::jsonb" if col in _JSON else "%s" for col in columns)
        updates = ", ".join(
            f"{col}=EXCLUDED.{col}" for col in columns[1:]
            if col != "first_stored_at")
        cur.execute(
            f"INSERT INTO organizations ({', '.join(columns)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT(identity_key) DO UPDATE SET {updates}",
            params)

    def _sync_facts(self, cur, stable_id: str, facts: list[dict],
                    table: str) -> None:
        for fact in facts:
            value = fact.get("value")
            if value is None:
                continue
            cur.execute(
                f"INSERT INTO {table}(identity_key, value, fact) "
                f"VALUES (%s, %s, %s::jsonb) "
                f"ON CONFLICT(identity_key, value) DO UPDATE SET "
                f"fact=EXCLUDED.fact",
                (stable_id, str(value), _dumps(fact)))

    def _upsert_contacts(self, cur, stable_id: str, contacts: list[dict],
                         now: str) -> int:
        count = 0
        for contact in contacts:
            uid = contact_uid(stable_id, contact)
            cur.execute(
                "SELECT provenance, first_stored_at FROM contacts "
                "WHERE contact_uid=%s", (uid,))
            existing = cur.fetchone()
            provenance = _merge_provenance(
                _j(existing["provenance"], []) if existing else [],
                contact.get("provenance") or [])
            first_stored = existing["first_stored_at"] if existing else now
            cur.execute(
                """
                INSERT INTO contacts (
                    contact_uid, identity_key, engine_contact_id, name, role,
                    email, phone, source_url, confidence_score,
                    confidence_reasons, preferred_for_submissions,
                    verified_at, provenance, first_stored_at, last_stored_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                          %s, %s, %s::jsonb, %s, %s)
                ON CONFLICT(contact_uid) DO UPDATE SET
                    engine_contact_id=EXCLUDED.engine_contact_id,
                    name=EXCLUDED.name,
                    role=EXCLUDED.role,
                    email=EXCLUDED.email,
                    phone=EXCLUDED.phone,
                    source_url=EXCLUDED.source_url,
                    confidence_score=EXCLUDED.confidence_score,
                    confidence_reasons=EXCLUDED.confidence_reasons,
                    preferred_for_submissions=
                        EXCLUDED.preferred_for_submissions,
                    verified_at=COALESCE(EXCLUDED.verified_at,
                                         contacts.verified_at),
                    provenance=EXCLUDED.provenance,
                    last_stored_at=EXCLUDED.last_stored_at
                """,
                (uid, stable_id, str(contact.get("id") or "") or None,
                 contact.get("name"), contact.get("role"),
                 contact.get("email"), contact.get("phone"),
                 contact.get("source_url"),
                 contact.get("confidence_score"),
                 _dumps(contact.get("confidence_reasons")),
                 bool(contact.get("preferred_for_submissions")),
                 contact.get("verified_at"), _dumps(provenance),
                 first_stored, now))
            count += 1
        return count

    def _upsert_submission(self, cur, stable_id: str, payload: dict,
                           now: str) -> None:
        cur.execute(
            "SELECT first_stored_at FROM submission_paths "
            "WHERE identity_key=%s", (stable_id,))
        existing = cur.fetchone()
        first_stored = existing["first_stored_at"] if existing else now
        cur.execute(
            "INSERT INTO submission_paths "
            "(identity_key, payload, first_stored_at, last_stored_at) "
            "VALUES (%s, %s::jsonb, %s, %s) "
            "ON CONFLICT(identity_key) DO UPDATE SET payload=EXCLUDED.payload,"
            " last_stored_at=EXCLUDED.last_stored_at",
            (stable_id, _dumps(payload), first_stored, now))

    def _replace_fetches(self, cur, stable_id: str,
                         fetches: list[dict]) -> None:
        cur.execute("DELETE FROM source_fetches WHERE identity_key=%s",
                    (stable_id,))
        for fetch in fetches:
            cur.execute(
                "INSERT INTO source_fetches(identity_key, url, ok, status, "
                "error_kind, fetched_at) VALUES (%s, %s, %s, %s, %s, %s)",
                (stable_id, fetch.get("url"),
                 bool(fetch["ok"]) if fetch.get("ok") is not None else None,
                 fetch.get("status"), fetch.get("error_kind"),
                 fetch.get("fetched_at")))

    # -- read side ---------------------------------------------------------------

    def list_stations(self, limit: int = 50, offset: int = 0,
                      q: str | None = None,
                      status: str | None = None,
                      genre: str | None = None,
                      format_filter: str | None = None,
                      country: str | None = None,
                      min_confidence: float | None = None
                      ) -> tuple[list[dict], int]:
        clauses, params = [], []
        if q:
            clauses.append("name ILIKE %s")
            params.append(f"%{q}%")
        if status:
            clauses.append("status = %s")
            params.append(status)
        if genre:
            clauses.append("genres::text ILIKE %s")
            params.append(f"%{genre}%")
        if format_filter:
            clauses.append("formats::text ILIKE %s")
            params.append(f"%{format_filter}%")
        if country:
            clauses.append("country = %s")
            params.append(country)
        if min_confidence is not None:
            clauses.append("confidence_score >= %s")
            params.append(float(min_confidence))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(f"SELECT COUNT(*) AS n FROM organizations {where}",
                        params)
            total = int(cur.fetchone()["n"])
            cur.execute(
                f"SELECT * FROM organizations {where} "
                "ORDER BY lower(name), identity_key LIMIT %s OFFSET %s",
                [*params, int(limit), int(offset)])
            rows = [_org_from_row(r) for r in cur.fetchall()]
        return rows, total

    def get_station(self, identity_key: str) -> dict | None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM organizations WHERE identity_key=%s",
                        (identity_key,))
            row = cur.fetchone()
        return _org_from_row(row) if row else None

    def _facts(self, table: str, identity_key: str) -> list[dict]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                f"SELECT fact FROM {table} WHERE identity_key=%s "
                "ORDER BY value", (identity_key,))
            return [_j(r["fact"], {}) for r in cur.fetchall()]

    def get_station_emails(self, identity_key: str) -> list[dict]:
        return self._facts("organization_emails", identity_key)

    def get_station_phones(self, identity_key: str) -> list[dict]:
        return self._facts("organization_phones", identity_key)

    def get_station_contacts(self, identity_key: str) -> list[dict]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT * FROM contacts WHERE identity_key=%s "
                "ORDER BY role, lower(name)", (identity_key,))
            return [_contact_from_row(r) for r in cur.fetchall()]

    def get_submission(self, identity_key: str) -> dict | None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT payload FROM submission_paths "
                        "WHERE identity_key=%s", (identity_key,))
            row = cur.fetchone()
        return _j(row["payload"], None) if row else None

    def get_fetches(self, identity_key: str) -> list[dict]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT url, ok, status, error_kind, fetched_at "
                "FROM source_fetches WHERE identity_key=%s ORDER BY fetch_id",
                (identity_key,))
            return [{
                "url": r["url"],
                "ok": bool(r["ok"]) if r["ok"] is not None else False,
                "status": r["status"], "error_kind": r["error_kind"],
                "fetched_at": r["fetched_at"],
            } for r in cur.fetchall()]

    # -- verification persistence (append-only) ---------------------------------

    def persist_verification(self, records: list[dict], report: dict, *,
                             source: str = "api") -> dict:
        if not isinstance(report, dict) \
                or not isinstance(report.get("records"), list):
            raise TypeError("report must be a verify_records() report dict")
        run_id = str(uuid.uuid4())
        stored = skipped = 0
        dict_records = [r for r in records if isinstance(r, dict)]
        with self._lock:
            with self._conn:
                cur = self._conn.cursor()
                cur.execute(
                    "INSERT INTO verification_runs(run_id, started_at, "
                    "completed_at, summary, source) VALUES (%s, %s, %s, "
                    "%s::jsonb, %s)",
                    (run_id, str(report.get("started_at") or utc_now_iso()),
                     str(report.get("completed_at") or ""),
                     _dumps(report.get("summary") or {}), str(source)))
                for entry, record in zip(report["records"], dict_records):
                    try:
                        _, stable_id, _ = normalize_intelligence_record(record)
                    except Exception:
                        skipped += len(entry.get("results") or [])
                        continue
                    cur.execute("SELECT 1 FROM organizations "
                                "WHERE identity_key=%s", (stable_id,))
                    if not cur.fetchone():
                        skipped += len(entry.get("results") or [])
                        continue
                    for result in entry.get("results") or []:
                        cur.execute(
                            "INSERT INTO verification_results(run_id, "
                            "identity_key, claim, status, method, verifier, "
                            "evidence, reasons, checked_at) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, "
                            "%s::jsonb, %s)",
                            (run_id, stable_id, str(result.get("claim")),
                             str(result.get("status")),
                             result.get("method"), result.get("verifier"),
                             _dumps(result.get("evidence") or []),
                             _dumps(result.get("reasons") or []),
                             str(result.get("checked_at") or "")))
                        stored += 1
                cur.execute(
                    "UPDATE verification_runs SET completed_at=%s "
                    "WHERE run_id=%s",
                    (str(report.get("completed_at") or utc_now_iso()),
                     run_id))
        return {"run_id": run_id, "stored": stored, "skipped": skipped}

    def get_verification(self, identity_key: str) -> dict | None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT * FROM verification_runs WHERE run_id IN "
                "(SELECT DISTINCT run_id FROM verification_results "
                "WHERE identity_key=%s) ORDER BY started_at DESC",
                (identity_key,))
            runs = cur.fetchall()
            if not runs:
                return None
            cur.execute(
                "SELECT * FROM verification_results WHERE identity_key=%s "
                "ORDER BY checked_at DESC, result_id DESC", (identity_key,))
            results = cur.fetchall()
        return {
            "runs": [{
                "run_id": r["run_id"], "started_at": r["started_at"],
                "completed_at": r["completed_at"],
                "summary": _j(r["summary"], {}), "source": r["source"],
            } for r in runs],
            "results": [{
                "claim": r["claim"], "status": r["status"],
                "method": r["method"], "verifier": r["verifier"],
                "evidence": _j(r["evidence"], []),
                "reasons": _j(r["reasons"], []),
                "checked_at": r["checked_at"], "run_id": r["run_id"],
            } for r in results],
        }

    def get_ingestion_run(self, run_id: str) -> dict | None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM ingestion_runs WHERE run_id=%s",
                        (run_id,))
            row = cur.fetchone()
            if not row:
                return None
            cur.execute(
                "SELECT stage, error_kind, message, url FROM "
                "ingestion_failures WHERE run_id=%s ORDER BY failure_id",
                (run_id,))
            failures = cur.fetchall()
        return {
            "run_id": row["run_id"], "source": row["source"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "records_accepted": row["records_accepted"],
            "records_failed": row["records_failed"],
            "failures": [dict(f) for f in failures],
        }

    # -- submission assets + link accessibility (Phase 8) -----------------------

    @staticmethod
    def _track_from_row(row: dict) -> dict:
        return {
            "track_id": row["track_id"],
            "sha256": row["sha256"],
            "original_filename": row["original_filename"],
            "size_bytes": int(row["size_bytes"]),
            "content_type": row["content_type"],
            "status": row["status"],
            "reject_reason": row["reject_reason"],
            "notes": row["notes"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def save_track(self, track: dict) -> dict:
        now = utc_now_iso()
        with self._lock:
            with self._conn:
                cur = self._conn.cursor()
                cur.execute(
                    "SELECT created_at FROM tracks WHERE track_id=%s",
                    (track["track_id"],))
                existing = cur.fetchone()
                created = existing["created_at"] if existing \
                    else str(track.get("created_at") or now)
                cur.execute(
                    """
                    INSERT INTO tracks(track_id, sha256, original_filename,
                                       size_bytes, content_type, status,
                                       reject_reason, notes,
                                       created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(track_id) DO UPDATE SET
                        original_filename=EXCLUDED.original_filename,
                        content_type=EXCLUDED.content_type,
                        status=EXCLUDED.status,
                        reject_reason=EXCLUDED.reject_reason,
                        notes=EXCLUDED.notes,
                        updated_at=EXCLUDED.updated_at
                    """,
                    (track["track_id"], track["sha256"],
                     track.get("original_filename"),
                     int(track["size_bytes"]),
                     track.get("content_type") or "audio/mpeg",
                     track["status"], track.get("reject_reason"),
                     track.get("notes"), created,
                     str(track.get("updated_at") or now)))
        return self.get_track(track["track_id"])

    def get_track(self, track_id: str) -> dict | None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM tracks WHERE track_id=%s",
                        (track_id,))
            row = cur.fetchone()
        return self._track_from_row(row) if row else None

    def list_tracks(self, limit: int = 50, offset: int = 0,
                    status: str | None = None) -> tuple[list[dict], int]:
        clauses, params = [], []
        if status:
            clauses.append("status = %s")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(f"SELECT COUNT(*) AS n FROM tracks {where}", params)
            total = int(cur.fetchone()["n"])
            cur.execute(
                f"SELECT * FROM tracks {where} "
                "ORDER BY created_at DESC, track_id LIMIT %s OFFSET %s",
                [*params, int(limit), int(offset)])
            rows = [self._track_from_row(r) for r in cur.fetchall()]
        return rows, total

    def record_link_check(self, identity_key: str, entry: dict) -> None:
        with self._lock:
            with self._conn:
                cur = self._conn.cursor()
                cur.execute(
                    "INSERT INTO submission_link_checks(identity_key, url, "
                    "target_kind, ok, status, error_kind, latency_ms, "
                    "checked_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (identity_key, entry["url"], entry["target_kind"],
                     bool(entry.get("ok")), entry.get("status"),
                     entry.get("error_kind"), entry.get("latency_ms"),
                     str(entry.get("checked_at") or utc_now_iso())))

    def get_link_checks(self, identity_key: str,
                        limit: int = 50) -> list[dict]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT url, target_kind, ok, status, error_kind, "
                "latency_ms, checked_at FROM submission_link_checks "
                "WHERE identity_key=%s ORDER BY check_id DESC LIMIT %s",
                (identity_key, int(limit)))
            return [{
                "url": r["url"], "target_kind": r["target_kind"],
                "ok": bool(r["ok"]), "status": r["status"],
                "error_kind": r["error_kind"],
                "latency_ms": r["latency_ms"],
                "checked_at": r["checked_at"],
            } for r in cur.fetchall()]


    def close(self) -> None:
        with self._lock:
            try:
                self._conn.rollback()   # release any idle read transaction
            except Exception:
                pass
            self._conn.close()


def main(argv: list[str] | None = None,
         storage: "PostgresStorage | None" = None) -> int:
    """CLI twin of ``python -m database.service`` for PostgreSQL/Supabase.

    The DSN comes from --dsn or the MIE_PG_DSN environment variable. The
    optional *storage* parameter is an injection seam for offline tests
    (mirrors PostgresStorage(conn=...)); production callers never pass it.
    """
    parser = argparse.ArgumentParser(
        prog="python -m database.pg_store",
        description="Initialize the PostgreSQL/Supabase schema and/or "
                    "ingest produced intelligence JSON.")
    parser.add_argument(
        "--dsn", default=os.environ.get("MIE_PG_DSN"),
        help="PostgreSQL DSN (defaults to MIE_PG_DSN)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="apply pending migrations, then exit")
    ingest = sub.add_parser("ingest", help="ingest intelligence JSON")
    ingest.add_argument("input", help="path to enrichment output JSON")
    ingest.add_argument("--source", default="cli")
    ls = sub.add_parser("list", help="list stored organizations and contacts")
    ls.add_argument("--limit", type=int, default=50,
                    help="max organizations to show (default 50)")
    ls.add_argument("--contacts", action="store_true", default=False,
                    help="also list contacts per organization")
    args = parser.parse_args(argv)

    if storage is None:
        if not args.dsn:
            parser.error("--dsn or MIE_PG_DSN is required")
        storage = PostgresStorage(dsn=args.dsn)
    try:
        if args.command == "init":
            print(json.dumps({"database": "ready",
                              "schema_version": storage.version}))
            return 0
        if args.command == "list":
            rows, total = storage.list_stations(limit=args.limit)
            result = {"total": total, "stations": []}
            for row in rows:
                entry = {
                    "identity_key": row["identity_key"],
                    "name": row.get("name"),
                    "status": row.get("status"),
                    "country": row.get("country"),
                }
                if args.contacts:
                    entry["contacts"] = storage.get_station_contacts(
                        row["identity_key"])
                result["stations"].append(entry)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        records = load_records_file(args.input)
        report = storage.ingest_intelligence(records, source=args.source)
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
        return 0 if report.records_failed == 0 else 1
    finally:
        storage.close()


if __name__ == "__main__":
    sys.exit(main())
