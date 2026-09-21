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
import re
import sys
import threading
import uuid
from contextlib import contextmanager

# A plain PostgreSQL identifier used as an isolated schema name; anything
# with quotes/dots/whitespace would be a SQL-injection vector if inlined into
# ``SET search_path TO "..."``, so only these are accepted.
_SQL_IDENT_FULLMATCH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\Z")

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
    _dj_dev_fixture_exclusion_sql,
    _outreach_dev_fixture_exclusion_sql,
)

# Development-fixture quarantine, mirroring database.service so the
# PostgreSQL stack hides the same artifacts from production display. A row is
# a dev artifact when its identity is a geo-name seed, or when its host lives
# on a reserved-TLD suffix or is absent.
_DEV_HOST_SUFFIXES = (".example", ".test", ".invalid", ".localhost", ".local")
_DEV_HOST = "LOWER(COALESCE(NULLIF(domain, ''), NULLIF(website, '')))"
# psycopg parses ``%`` in statement text as a placeholder marker; a literal
# LIKE wildcard must be escaped as ``%%`` (which psycopg passes through as a
# single ``%`` to the server). The SQLite twin in database.service carries the
# same patterns with single ``%`` because sqlite3 has no placeholder grammar.
_DEV_FIXTURE_EXCLUSION_SQL = (
    "identity_key NOT LIKE 'namegeo:%%' AND "
    + " AND ".join(
        f"{_DEV_HOST} IS NOT NULL AND {_DEV_HOST} NOT LIKE '%%{suffix}' "
        "ESCAPE '\\'"
        for suffix in _DEV_HOST_SUFFIXES))

# DJ and outreach read-path quarantines mirror the station rule; psycopg
# placeholder grammar requires the ``%%`` wildcard (see note above).
_DEV_DJ_FIXTURE_EXCLUSION_SQL = _dj_dev_fixture_exclusion_sql("%%")
_DEV_OUTREACH_FIXTURE_EXCLUSION_SQL = _outreach_dev_fixture_exclusion_sql("%%")

# Read-path exclusion for DJs deterministically classified as NOT a DJ
# (rejected once, hidden from the normal listing forever).
_DJ_REJECTED_SQL = (
    "COALESCE(verification::jsonb->'classification'->>'verdict', '') "
    "= 'rejected'")

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


def _dj_from_row(row: dict) -> dict:
    """Payload keys identical to PersistenceService._dj_from_row."""
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
        "genres": _j(row["genres"], None),
        "formats": _j(row["formats"], None),
        "source_urls": _j(row["source_urls"], None),
        "verification": _j(row["verification"], {}),
        "discovered_at": row["discovered_at"],
        "last_observed_at": row["last_observed_at"],
        "first_stored_at": row["first_stored_at"],
        "last_stored_at": row["last_stored_at"],
    }


def _dj_channel_from_row(row: dict) -> dict:
    return {
        "channel": row["channel"],
        "value": row["value"],
        "source_url": row["source_url"],
        "verified_at": row["verified_at"],
    }


class PostgresStorage:
    """PostgreSQL backend for the shared intelligence repository surface."""

    # Statement/network guard: production connections are created with a
    # finite statement_timeout so a single slow or client-aborted query can
    # never block the whole API forever. Because all storage operations share
    # one connection and a coarse lock, a query that never returns would
    # otherwise hang every subsequent request (observed as the frontend
    # sitting on "connecting..."). A bounded timeout lets the server abort the
    # stuck query, release the lock, and recover instead of wedging the API.
    _STATEMENT_TIMEOUT_MS = 8000

    def __init__(self, dsn: str | None = None, conn=None, logger=None,
                 search_path: str | None = None) -> None:
        self.logger = logger or get_logger("mie.storage.pg")
        self._lock = threading.RLock()
        # Only a connection created from *dsn* is "owned": it may be closed and
        # replaced to recover from a poisoned/aborted transaction. Injected
        # connections (structural tests) are never auto-closed so the
        # connection-reuse contracts hold unchanged.
        self._owns_conn = conn is None
        self._dsn = dsn
        if search_path is not None:
            if not _SQL_IDENT_FULLMATCH.search(search_path):
                raise ValueError(
                    "search_path must be a plain SQL identifier "
                    f"(got {search_path!r})")
        self._search_path = search_path
        if conn is not None:            # injectable for structural tests
            self._conn = conn
        else:
            self._conn = self._connect()
        self.version = apply_pg_migrations(self._conn)

    def _connect(self):
        """Open a new production connection (owned connections only)."""
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise ImportError(
                "psycopg is required for the PostgreSQL backend "
                '(pip install "psycopg[binary]"); configure it via '
                "MIE_PG_DSN or pass an existing connection") from exc
        if not self._dsn:
            raise ValueError("PostgresStorage requires a DSN")
        # The connect-time ``-c`` option is honored by self-hosted PostgreSQL
        # but silently IGNORED by Supabase's transaction pooler (which pins
        # its own 2-minute default). Because every read shares one connection
        # and one coarse lock, this guard is the only thing that prevents a
        # single stuck query from wedging the whole API; so instead of
        # relying on the startup option we issue an explicit ``SET`` after
        # connecting, which the pooler *does* honor (verified live). The ``-c``
        # option is kept as a belt-and-suspenders for direct connections.
        opts = f"-c statement_timeout={self._STATEMENT_TIMEOUT_MS}"
        conn = psycopg.connect(self._dsn, row_factory=dict_row, options=opts)
        try:
            cur = conn.cursor()
            # SET does not accept bind parameters, so the timeout is inlined
            # as a literal; it is a fixed integer constant, not user input.
            cur.execute(
                f"SET statement_timeout = {self._STATEMENT_TIMEOUT_MS}")
            if self._search_path:
                # Scope every unqualified table reference to the isolated
                # schema (persists on this connection across transactions;
                # verified live on Supabase). Used to run parity tests / ad-
                # hoc ingestion against an ephemeral schema instead of public.
                cur.execute(f'SET search_path TO "{self._search_path}"')
            conn.commit()
        except Exception:
            # A host that forbids client-side SET (rare) still has the ``-c``
            # option and the pooler's own default as fallback; do not let the
            # guard setup itself break connection creation.
            try:
                conn.rollback()
            except Exception:
                pass
        return conn

    def _recover_connection(self) -> None:
        """Discard a poisoned connection and reopen a fresh one.

        Only owned (production) connections are replaced. Injected test
        connections are left untouched so the connection-reuse tests
        continue to assert the persistent-connection contract. Called from
        operation error paths so a single bad/failed query can never leave
        the shared connection stuck for every later request.
        """
        if not self._owns_conn or self._conn is None:
            return
        try:
            self._conn.close()
        except Exception:
            pass
        try:
            self._conn = self._connect()
        except Exception:
            self._conn = None

    def _ensure_connection(self) -> None:
        """Verify the connection is alive; recover transparently if stale.

        Runs a lightweight ``SELECT 1`` under the coarse lock. If the
        connection is closed or the server has dropped it (idle-timeout,
        PgBouncer reclaim, network blip), ``_recover_connection`` opens a
        fresh one so the caller never sees a 500 on the first request.
        Must be called with ``self._lock`` held.
        """
        if not self._owns_conn or self._conn is None:
            return
        try:
            cur = self._conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
        except Exception:
            self._recover_connection()

    @contextmanager
    def _guard(self):
        """Run an operation under the coarse lock, recovering on any error.

        Every read currently shares the single connection and the single
        coarse lock. If one query errors (a dropped connection, an aborted
        transaction from an earlier failure, a statement-timeout abort), the
        connection can become poisoned and — because all reads share it —
        every subsequent request blocks forever under the lock (the frontend
        site on "connecting..."). This guard recovers an owned connection on
        any exception so the API degrades to a fast error instead of a
        permanent hang. Injected connections (tests) are never touched.

        A proactive ``SELECT 1`` health check runs *before* yielding the
        connection so stale/idle connections (e.g. Supabase/PgBouncer idle
        timeouts) are recovered transparently instead of causing a first-
        request 500.
        """
        try:
            with self._lock:
                self._ensure_connection()
                yield self._conn
        except Exception:
            self._recover_connection()
            raise

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
            self._ensure_connection()
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
        incoming_uids: list[str] = []
        for contact in contacts:
            uid = contact_uid(stable_id, contact)
            incoming_uids.append(uid)
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

        # Reconciliation preserves stored facts: an intake delivers the
        # COMPLETE, freshly enriched contact set, so any contact_uid already
        # stored for this station that is NOT in the fresh result is stale
        # (superseded extraction, garbage from older code, a DJ-list flood)
        # and is removed — but ONLY when the fresh set is non-empty. A
        # partial/empty intake (fetch failure, robots-blocked page, budget
        # limit) must NEVER erase previously stored contact facts; if the
        # intake genuinely has zero contacts it simply leaves the stored set
        # untouched. Mirrors PersistenceService._upsert_contacts exactly.
        if incoming_uids:
            cur.execute(
                "DELETE FROM contacts WHERE identity_key=%s "
                "AND contact_uid != ALL(%s::text[])",
                (stable_id, incoming_uids))
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
                      min_confidence: float | None = None,
                      exclude_dev: bool = False
                      ) -> tuple[list[dict], int] | tuple[list[dict], int, int]:
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
        base_where = " AND ".join(clauses)
        where = f"WHERE {base_where}" if base_where else ""
        if exclude_dev:
            dev_clause = "(" + _DEV_FIXTURE_EXCLUSION_SQL + ")"
            dev_where = (f"WHERE {base_where} AND {dev_clause}"
                         if base_where else f"WHERE {dev_clause}")
            with self._guard() as conn:
                cur = conn.cursor()
                cur.execute(f"SELECT COUNT(*) AS n FROM organizations {dev_where}",
                            params)
                visible_total = int(cur.fetchone()["n"])
                cur.execute(f"SELECT COUNT(*) AS n FROM organizations {where}",
                            params)
                full_total = int(cur.fetchone()["n"])
                cur.execute(
                    f"SELECT * FROM organizations {dev_where} "
                    "ORDER BY lower(name), identity_key LIMIT %s OFFSET %s",
                    [*params, int(limit), int(offset)])
                rows = [_org_from_row(r) for r in cur.fetchall()]
            return rows, visible_total, full_total - visible_total
        with self._guard() as conn:
            cur = conn.cursor()
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
        with self._guard() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM organizations WHERE identity_key=%s",
                        (identity_key,))
            row = cur.fetchone()
        return _org_from_row(row) if row else None

    def _facts(self, table: str, identity_key: str) -> list[dict]:
        with self._guard() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT fact FROM {table} WHERE identity_key=%s "
                "ORDER BY value", (identity_key,))
            return [_j(r["fact"], {}) for r in cur.fetchall()]

    def get_station_emails(self, identity_key: str) -> list[dict]:
        return self._facts("organization_emails", identity_key)

    def get_station_phones(self, identity_key: str) -> list[dict]:
        return self._facts("organization_phones", identity_key)

    def get_station_contacts(self, identity_key: str) -> list[dict]:
        with self._guard() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM contacts WHERE identity_key=%s "
                "ORDER BY role, lower(name)", (identity_key,))
            return [_contact_from_row(r) for r in cur.fetchall()]

    def get_submission(self, identity_key: str) -> dict | None:
        with self._guard() as conn:
            cur = conn.cursor()
            cur.execute("SELECT payload FROM submission_paths "
                        "WHERE identity_key=%s", (identity_key,))
            row = cur.fetchone()
        return _j(row["payload"], None) if row else None

    def get_fetches(self, identity_key: str) -> list[dict]:
        with self._guard() as conn:
            cur = conn.cursor()
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
            self._ensure_connection()
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
            self._ensure_connection()
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
            self._ensure_connection()
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

    # -- Phase 11: automation discovery jobs ----------------------------------

    def record_discovery_job(self, report: dict) -> str:
        """Persist (upsert by run_id) one automation discovery job run.

        Upserting on the PRIMARY KEY ``run_id`` lets the async worker
        transition one row through queued -> running -> terminal without
        leaving duplicate ledger rows.
        """
        with self._lock:
            self._ensure_connection()
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO discovery_jobs (
                    run_id, organization_type, config, provider, status,
                    queries_run, candidates_found, records_ingested,
                    duplicates, failures, error_message, started_at,
                    completed_at
                ) VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s)
                ON CONFLICT (run_id) DO UPDATE SET
                    organization_type = EXCLUDED.organization_type,
                    config = EXCLUDED.config,
                    provider = EXCLUDED.provider,
                    status = EXCLUDED.status,
                    queries_run = EXCLUDED.queries_run,
                    candidates_found = EXCLUDED.candidates_found,
                    records_ingested = EXCLUDED.records_ingested,
                    duplicates = EXCLUDED.duplicates,
                    failures = EXCLUDED.failures,
                    error_message = EXCLUDED.error_message,
                    started_at = EXCLUDED.started_at,
                    completed_at = EXCLUDED.completed_at
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
                 str(report.get("started_at") or utc_now_iso()),
                 str(report.get("completed_at") or "")))
            self._conn.commit()
        return str(report["run_id"])

    def clone(self) -> "PostgresStorage":
        """An independent storage bound to the same database (own connection).

        Used by the background discovery worker so an in-flight job never
        holds the shared request-thread connection.
        """
        if not self._dsn:
            raise ValueError(
                "cannot clone connection-injected storage; a DSN is required")
        return PostgresStorage(dsn=self._dsn)

    def get_discovery_job(self, run_id: str) -> dict | None:
        """One stored automation discovery job run (or None)."""
        with self._lock:
            self._ensure_connection()
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM discovery_jobs WHERE run_id=%s",
                        (run_id,))
            row = cur.fetchone()
            self._conn.commit()
        if not row:
            return None
        return {
            "run_id": row["run_id"],
            "organization_type": row["organization_type"],
            "config": _j(row["config"], {}),
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
            self._ensure_connection()
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
            self._ensure_connection()
            cur = self._conn.cursor()
            cur.execute(f"SELECT COUNT(*) AS n FROM tracks {where}", params)
            total = int(cur.fetchone()["n"])
            cur.execute(
                f"SELECT * FROM tracks {where} "
                "ORDER BY created_at DESC, track_id LIMIT %s OFFSET %s",
                [*params, int(limit), int(offset)])
            rows = [self._track_from_row(r) for r in cur.fetchall()]
        return rows, total

    def delete_track(self, track_id: str) -> str | None:
        """Delete one stored asset RECORD; returns the deleted id or None.

        Only the database record is removed — never the uploaded file bytes
        in the asset store (the existing app defines no file-level removal).
        """
        with self._lock:
            self._ensure_connection()
            cur = self._conn.cursor()
            cur.execute("DELETE FROM tracks WHERE track_id=%s", (track_id,))
        return track_id if cur.rowcount else None

    def record_link_check(self, identity_key: str, entry: dict) -> None:
        with self._lock:
            self._ensure_connection()
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
            self._ensure_connection()
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

    # -- outreach messages + attempts (Phase 9) --------------------------------

    @staticmethod
    def _outreach_from_row(row: dict) -> dict:
        """Keys identical to PersistenceService._outreach_from_row; JSONB
        columns arrive pre-decoded from psycopg, so _j keeps NULL as None."""
        return {
            "outreach_id": row["outreach_id"],
            "contact_uid": row["contact_uid"],
            "identity_key": row["identity_key"],
            "recipient_name": row["recipient_name"],
            "recipient_role": row["recipient_role"],
            "organization": row["organization"],
            "email": row["email"],
            "source_url": row["source_url"],
            "outreach_class": row["outreach_class"],
            "submission_url": row["submission_url"],
            "target_type": row.get("target_type") or "station",
            "track_id": row["track_id"],
            "track": _j(row["track"], None),
            "context": _j(row["context"], None),
            "subject": row["subject"],
            "message": row["message"],
            "from_email": row["from_email"],
            "sharing": _j(row["sharing"], None),
            "status": row["status"],
            "provider": row["provider"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def save_outreach(self, record: dict) -> dict:
        """Insert or overwrite one outreach message row."""
        now = utc_now_iso()
        with self._lock:
            self._ensure_connection()
            with self._conn:
                cur = self._conn.cursor()
                cur.execute(
                    "SELECT created_at FROM outreach_messages "
                    "WHERE outreach_id=%s",
                    (record["outreach_id"],))
                existing = cur.fetchone()
                created = existing["created_at"] if existing \
                    else str(record.get("created_at") or now)
                cur.execute(
                    """
                    INSERT INTO outreach_messages(
                        outreach_id, contact_uid, identity_key,
                        recipient_name, recipient_role, organization,
                        email, source_url, outreach_class, submission_url,
                        target_type, track_id, track, context,
                        subject, message, from_email, sharing,
                        status, provider, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s,
                            %s::jsonb, %s, %s, %s, %s)
                    ON CONFLICT(outreach_id) DO UPDATE SET
                        contact_uid=EXCLUDED.contact_uid,
                        identity_key=EXCLUDED.identity_key,
                        recipient_name=EXCLUDED.recipient_name,
                        recipient_role=EXCLUDED.recipient_role,
                        organization=EXCLUDED.organization,
                        email=EXCLUDED.email,
                        source_url=EXCLUDED.source_url,
                        outreach_class=EXCLUDED.outreach_class,
                        submission_url=EXCLUDED.submission_url,
                        target_type=EXCLUDED.target_type,
                        track_id=EXCLUDED.track_id,
                        track=EXCLUDED.track,
                        context=EXCLUDED.context,
                        subject=EXCLUDED.subject,
                        message=EXCLUDED.message,
                        from_email=EXCLUDED.from_email,
                        sharing=EXCLUDED.sharing,
                        status=EXCLUDED.status,
                        provider=EXCLUDED.provider,
                        updated_at=EXCLUDED.updated_at
                    """,
                    (record["outreach_id"], record.get("contact_uid"),
                     record.get("identity_key"), record.get("recipient_name"),
                     record.get("recipient_role"), record.get("organization"),
                     record["email"], record.get("source_url"),
                     record.get("outreach_class") or "email",
                     record.get("submission_url"),
                     record.get("target_type") or "station",
                     record.get("track_id"),
                     _dumps(record.get("track")),
                     _dumps(record.get("context")),
                     record.get("subject"), record.get("message"),
                     record.get("from_email"),
                     _dumps(record.get("sharing")),
                     record["status"], record.get("provider") or "local",
                     created, str(record.get("updated_at") or now)))
        return self.get_outreach(record["outreach_id"])

    def get_outreach(self, outreach_id: str) -> dict | None:
        with self._lock:
            self._ensure_connection()
            cur = self._conn.cursor()
            cur.execute(
                "SELECT * FROM outreach_messages WHERE outreach_id=%s",
                (outreach_id,))
            row = cur.fetchone()
        return self._outreach_from_row(row) if row else None

    def list_outreach(self, limit: int = 50, offset: int = 0,
                      status: str | None = None,
                      exclude_dev: bool = False
                      ) -> tuple[list[dict], int] | tuple[list[dict], int, int]:
        clauses, params = [], []
        if status:
            clauses.append("status = %s")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        if exclude_dev:
            dev_clause = "(" + _DEV_OUTREACH_FIXTURE_EXCLUSION_SQL + ")"
            dev_where = (f"WHERE {' AND '.join(clauses)} AND {dev_clause}"
                         if clauses else f"WHERE {dev_clause}")
            with self._lock:
                self._ensure_connection()
                cur = self._conn.cursor()
                cur.execute(
                    f"SELECT COUNT(*) AS n FROM outreach_messages {dev_where}",
                    params)
                visible_total = int(cur.fetchone()["n"])
                cur.execute(
                    f"SELECT COUNT(*) AS n FROM outreach_messages {where}",
                    params)
                full_total = int(cur.fetchone()["n"])
                cur.execute(
                    f"SELECT * FROM outreach_messages {dev_where} "
                    "ORDER BY created_at DESC, outreach_id LIMIT %s OFFSET %s",
                    [*params, int(limit), int(offset)])
                rows = [self._outreach_from_row(r) for r in cur.fetchall()]
            return rows, visible_total, full_total - visible_total
        with self._lock:
            self._ensure_connection()
            cur = self._conn.cursor()
            cur.execute(
                f"SELECT COUNT(*) AS n FROM outreach_messages {where}",
                params)
            total = int(cur.fetchone()["n"])
            cur.execute(
                f"SELECT * FROM outreach_messages {where} "
                "ORDER BY created_at DESC, outreach_id LIMIT %s OFFSET %s",
                [*params, int(limit), int(offset)])
            rows = [self._outreach_from_row(r) for r in cur.fetchall()]
        return rows, total

    def append_outreach_attempt(self, outreach_id: str,
                                attempt: dict) -> None:
        with self._lock:
            self._ensure_connection()
            with self._conn:
                cur = self._conn.cursor()
                cur.execute(
                    "INSERT INTO outreach_attempts(outreach_id, event, "
                    "provider, \"at\", meta) VALUES (%s, %s, %s, %s, %s)",
                    (outreach_id, attempt["event"],
                     attempt.get("provider") or "local",
                     str(attempt.get("at") or utc_now_iso()),
                     _dumps(attempt.get("meta"))))

    def set_outreach_status(self, outreach_id: str, status: str,
                            at: str | None = None) -> None:
        with self._lock:
            self._ensure_connection()
            with self._conn:
                cur = self._conn.cursor()
                cur.execute(
                    "UPDATE outreach_messages SET status=%s, updated_at=%s "
                    "WHERE outreach_id=%s",
                    (status, at or utc_now_iso(), outreach_id))

    def get_outreach_attempts(self, outreach_id: str) -> list[dict]:
        with self._lock:
            self._ensure_connection()
            cur = self._conn.cursor()
            cur.execute(
                "SELECT event, provider, \"at\" AS at, meta "
                "FROM outreach_attempts WHERE outreach_id=%s "
                "ORDER BY attempt_id ASC",
                (outreach_id,))
            rows = cur.fetchall()
        return [{
            "event": r["event"], "provider": r["provider"],
            "at": r["at"], "meta": _j(r["meta"], None),
        } for r in rows]

    def delete_outreach(self, outreach_id: str) -> str | None:
        """Delete one outreach record; returns the deleted id or None.

        The connected schema cascades the delete to the record's attempt
        history (``outreach_attempts`` → ``outreach_messages``); the raw
        station/contact data that produced the record is untouched.
        """
        with self._lock:
            self._ensure_connection()
            cur = self._conn.cursor()
            cur.execute(
                "DELETE FROM outreach_messages WHERE outreach_id=%s",
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
                "(lower(name) ILIKE lower(%s) "
                "OR lower(coalesce(stage_name,'')) ILIKE lower(%s) "
                "OR lower(coalesce(program,'')) ILIKE lower(%s) "
                "OR lower(coalesce(station_name,'')) ILIKE lower(%s))")
            like = f"%{q}%"
            params.extend([like, like, like, like])
        if genre:
            clauses.append("genres::text ILIKE %s")
            params.append(f"%{genre}%")
        if country:
            clauses.append("country = %s")
            params.append(country)
        if location:
            clauses.append(
                "(lower(coalesce(city,'')) ILIKE lower(%s) "
                "OR lower(coalesce(state_or_region,'')) ILIKE lower(%s))")
            like = f"%{location}%"
            params.extend([like, like])
        if station:
            # Station affiliation is optional metadata on an INDEPENDENT DJ
            # record — searching it never adds or implies radio-station links.
            clauses.append(
                "(lower(coalesce(station_key,'')) ILIKE lower(%s) "
                "OR lower(coalesce(station_name,'')) ILIKE lower(%s))")
            like = f"%{station}%"
            params.extend([like, like])
        if dj_type:
            clauses.append("lower(coalesce(role,'')) ILIKE lower(%s)")
            params.append(f"%{dj_type}%")
        if platform:
            clauses.append("lower(coalesce(platform,'')) ILIKE lower(%s)")
            params.append(f"%{platform}%")
        has_contact_sql = (
            "EXISTS (SELECT 1 FROM dj_channels c WHERE c.dj_id = djs.dj_id)")
        if has_contact is True:
            clauses.append(has_contact_sql)
        elif has_contact is False:
            clauses.append("NOT " + has_contact_sql)
        order_by = "lower(name), dj_id"
        if sort == "station":
            order_by = "lower(coalesce(station_name,'')), lower(name), dj_id"
        elif sort == "discovered":
            order_by = "discovered_at, dj_id"
        direction = "DESC" if order == "desc" else "ASC"
        order_clause = ", ".join(
            f"{col} {direction}" for col in order_by.split(", "))
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

        with self._guard() as conn:
            cur = conn.cursor()

            def _count_where(where_fragment: str) -> int:
                cur.execute(
                    f"SELECT COUNT(*) AS n FROM djs {where_fragment}", params)
                return int(cur.fetchone()["n"])

            full_total = _count_where(where_sql)
            visible_total = _count_where(visible_where)
            cur.execute(
                f"SELECT * FROM djs {visible_where} "
                f"ORDER BY {order_clause} LIMIT %s OFFSET %s",
                [*params, int(limit), int(offset)])
            rows = [_dj_from_row(r) for r in cur.fetchall()]
            dev_excluded = 0
            if exclude_dev:
                dev_where = (
                    f"WHERE {where} AND NOT ("
                    f"{_DEV_DJ_FIXTURE_EXCLUSION_SQL})"
                    if where
                    else f"WHERE NOT ({_DEV_DJ_FIXTURE_EXCLUSION_SQL})")
                dev_excluded = _count_where(dev_where)
            rejected_excluded = 0
            if exclude_rejected:
                rej_where = (
                    f"WHERE {where} AND {_DJ_REJECTED_SQL}"
                    if where else f"WHERE {_DJ_REJECTED_SQL}")
                rejected_excluded = _count_where(rej_where)
        self._decorate_dj_contact_flag(rows)
        if exclude_dev and exclude_rejected:
            return (rows, visible_total, int(dev_excluded or 0),
                    int(rejected_excluded or 0))
        if exclude_rejected:
            return rows, visible_total, int(rejected_excluded or 0)
        if exclude_dev:
            return rows, visible_total, int(dev_excluded or 0)
        return rows, visible_total

    def get_dj(self, dj_id: str) -> dict | None:
        with self._guard() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM djs WHERE dj_id=%s", (dj_id,))
            row = cur.fetchone()
        return _dj_from_row(row) if row else None

    def get_dj_channels(self, dj_id: str) -> list[dict]:
        with self._guard() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT channel, value, source_url, verified_at "
                "FROM dj_channels WHERE dj_id=%s "
                "ORDER BY channel, value", (dj_id,))
            return [_dj_channel_from_row(r) for r in cur.fetchall()]

    def _decorate_dj_contact_flag(self, djs: list[dict]) -> None:
        """Set ``has_contact`` on each list row (batched, read-path only)."""
        if not djs:
            return
        seen = [row["dj_id"] for row in djs]
        with self._guard() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT DISTINCT dj_id FROM dj_channels "
                "WHERE dj_id = ANY(%s)", (seen,))
            contact_ids = {row["dj_id"] for row in cur.fetchall()}
        for row in djs:
            row["has_contact"] = row["dj_id"] in contact_ids

    def save_dj(self, record: dict, channels: list[dict] | None = None) -> None:
        now = utc_now_iso()
        channels = channels or []
        with self._lock:
            self._ensure_connection()
            cur = self._conn.cursor()
            cur.execute(
                "SELECT first_stored_at FROM djs WHERE dj_id=%s",
                (record["dj_id"],))
            existing = cur.fetchone()
            first_stored = existing["first_stored_at"] if existing else now
            cur.execute(
                """
                INSERT INTO djs(
                    dj_id, name, stage_name, role, program, station_key,
                    station_name, platform, country, state_or_region, city,
                    genres, formats, source_urls, verification,
                    discovered_at, last_observed_at,
                    first_stored_at, last_stored_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb,
                        %s, %s, %s, %s)
                ON CONFLICT(dj_id) DO UPDATE SET
                    name=EXCLUDED.name, stage_name=EXCLUDED.stage_name,
                    role=EXCLUDED.role, program=EXCLUDED.program,
                    station_key=EXCLUDED.station_key,
                    station_name=EXCLUDED.station_name,
                    platform=EXCLUDED.platform, country=EXCLUDED.country,
                    state_or_region=EXCLUDED.state_or_region,
                    city=EXCLUDED.city, genres=EXCLUDED.genres,
                    formats=EXCLUDED.formats,
                    source_urls=EXCLUDED.source_urls,
                    verification=EXCLUDED.verification,
                    discovered_at=EXCLUDED.discovered_at,
                    last_observed_at=EXCLUDED.last_observed_at,
                    first_stored_at=EXCLUDED.first_stored_at,
                    last_stored_at=EXCLUDED.last_stored_at
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
            cur.execute(
                "DELETE FROM dj_channels WHERE dj_id=%s",
                (record["dj_id"],))
            for ch in channels:
                cur.execute(
                    "INSERT INTO dj_channels(dj_id, channel, value, "
                    "source_url, verified_at) VALUES (%s, %s, %s, %s, %s)",
                    (record["dj_id"], ch.get("channel"),
                     ch.get("value"), ch.get("source_url"),
                     ch.get("verified_at")))

    def delete_dj(self, dj_id: str) -> str | None:
        with self._lock:
            self._ensure_connection()
            cur = self._conn.cursor()
            cur.execute("DELETE FROM djs WHERE dj_id=%s", (dj_id,))
        return dj_id if cur.rowcount else None

    def update_dj_verification(self, dj_id: str, verification: dict) -> bool:
        """Replace one DJ's ``verification`` JSON; touch ``last_stored_at``.

        Narrow update used by data cleanup to persist classification
        verdicts — no other DJ column is touched. Returns False for an
        unknown dj_id.
        """
        with self._lock:
            self._ensure_connection()
            cur = self._conn.cursor()
            cur.execute(
                "UPDATE djs SET verification=%s::jsonb, last_stored_at=%s "
                "WHERE dj_id=%s",
                (_dumps(verification), utc_now_iso(), dj_id))
            self._conn.commit()
            return cur.rowcount > 0

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
