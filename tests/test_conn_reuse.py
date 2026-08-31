"""Focused connection reuse tests for PostgresStorage."""
import sys
import os
import threading

os.environ.setdefault("MIE_TEST_MARKER", "1")

from database.pg_store import PostgresStorage, _ORG_COLUMNS
from database.service import normalize_intelligence_record, contact_uid


class _Row(dict):
    pass


class _RecordingCursor:
    def __init__(self, state):
        self._state = state

    def execute(self, sql, params=None):
        lowered = sql.strip().lower()
        recorded = (sql, tuple(params)) if params is not None else (sql, None)
        self._state.statements.append(recorded)
        if lowered.startswith("select version from schema_migrations"):
            self._rows = [{"version": v}
                          for v in sorted(self._state.applied)]
        elif lowered.startswith("select coalesce(max"):
            self._row = {"v": max(self._state.applied)
                         if self._state.applied else 0}
        elif lowered.startswith("insert into schema_migrations"):
            self._state.applied.add(int(params[0]))
        elif lowered.startswith("select * from organizations"):
            key = params[0]
            self._row = self._state.orgs.get(key)
        elif lowered.startswith(
                "select provenance, first_stored_at from contacts"):
            self._row = self._state.contacts.get(params[0])
        elif lowered.startswith(
                "select first_stored_at from submission_paths"):
            self._row = self._state.submissions.get(params[0])
        elif lowered.startswith("select count(*)"):
            self._row = {"n": len(self._state.orgs)}
        else:
            self._row = None
            if lowered.startswith("insert into organizations"):
                self._state.org_inserts.append(recorded)
                if params is not None:
                    self._state.orgs[params[0]] = True
            elif lowered.startswith("insert into organization_emails"):
                self._state.email_inserts.append(recorded)
            elif lowered.startswith("insert into organization_phones"):
                self._state.phone_inserts.append(recorded)
            elif lowered.startswith("insert into contacts"):
                self._state.contact_inserts.append(recorded)
            elif lowered.startswith("insert into submission_paths"):
                self._state.submission_inserts.append(recorded)
            elif lowered.startswith("delete from source_fetches"):
                self._state.fetch_deletes += 1
            elif lowered.startswith("insert into source_fetches"):
                self._state.fetch_inserts.append(recorded)
            elif lowered.startswith("insert into ingestion_runs"):
                self._state.run_inserts.append(recorded)
        return self

    def fetchone(self):
        row, self._row = getattr(self, "_row", None), None
        return row

    def fetchall(self):
        rows = getattr(self, "_rows", [])
        self._rows = []
        return rows


class _RecordingConn:
    def __init__(self):
        self.statements = []
        self.applied = set()
        self.orgs = {}
        self.contacts = {}
        self.submissions = {}
        self.org_inserts = []
        self.email_inserts = []
        self.phone_inserts = []
        self.contact_inserts = []
        self.submission_inserts = []
        self.fetch_deletes = 0
        self.fetch_inserts = []
        self.run_inserts = []
        self.commits = 0
        self.rollbacks = 0
        self.cursor_count = 0
        self.ctx_count = 0

    def cursor(self):
        self.cursor_count += 1
        return _RecordingCursor(self)

    def __enter__(self):
        self.ctx_count += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


def _discovery_record():
    return {
        "id": "engine-run-uuid-1",
        "organization_type": "radio_station",
        "name": "KQWX 91.5 FM",
        "website": "https://kqwx.example/",
        "country": None,
        "state_or_region": "Washington",
        "city": "Seattle",
        "station_type": "community",
        "classification_confidence": 0.7,
        "classification_evidence": [
            {"value": "community", "source_url": "https://kqwx.example/about",
             "source_type": "official_website_page", "method": "rule"}],
        "formats": ["college"],
        "genres": ["indie", "local"],
        "genre_evidence": {"indie": [
            {"value": "indie rock", "method": "rule"}]},
        "language": "en",
        "description": "Community station.",
        "social_urls": {"instagram": "https://instagram.com/kqwx"},
        "source_urls": ["https://kqwx.example/",
                        "https://kqwx.example/contact"],
        "website_reachable": True,
        "emails": [{
            "value": "jane@kqwx.example",
            "source_url": "https://kqwx.example/contact",
            "source_type": "official_website_page",
            "method": "text_rule",
            "discovered_at": "2026-08-24T00:00:00+00:00",
            "also_seen_at": []}],
        "phone_numbers": [],
        "contacts": [{
            "id": "contact-uuid-1", "name": "Jane Doe",
            "role": "music_director", "email": "jane@kqwx.example",
            "phone": None, "source_url": "https://kqwx.example/contact",
            "confidence_score": 0.3, "verified_at": None,
            "provenance": [
                {"value": "jane@kqwx.example",
                 "source_url": "https://kqwx.example/contact",
                 "source_type": "official_website_page",
                 "method": "text_rule", "discovered_at": "",
                 "also_seen_at": []},
                {"value": "Music Director",
                 "source_url": "https://kqwx.example/contact",
                 "source_type": "official_website_page",
                 "method": "role_label_rule", "discovered_at": "",
                 "also_seen_at": []}]}],
        "submission": {"url": "https://kqwx.example/submit",
                       "instructions": "MP3 preferred.",
                       "methods": [{"kind": "email",
                                    "inference": "evidenced"}]},
        "fetches": [{"url": "https://kqwx.example/", "ok": True,
                     "status": 200, "error_kind": None,
                     "fetched_at": "2026-08-24T00:00:01+00:00"}],
        "discovered_at": "2026-08-24T00:00:00+00:00",
        "last_observed_at": "2026-08-24T00:00:00+00:00",
        "confidence_score": 0.8,
        "confidence_reasons": ["official website reachable"],
        "status": "active",
        "raw_metadata": {},
    }


import unittest
import json


class TestConnectionReuse(unittest.TestCase):
    def setUp(self):
        self.conn = _RecordingConn()
        self.storage = PostgresStorage(conn=self.conn)

    def test_single_cursor_created_inside_transaction(self):
        before = self.conn.cursor_count
        self.storage.ingest_intelligence(
            [_discovery_record(), _discovery_record()], source="reuse-test")
        self.assertEqual(self.conn.cursor_count - before, 1)

    def test_context_manager_called_exactly_once(self):
        self.storage.ingest_intelligence(
            [_discovery_record(), _discovery_record()], source="reuse-test")
        self.assertEqual(self.conn.ctx_count, 1)

    def test_connection_not_closed_between_records(self):
        report = self.storage.ingest_intelligence(
            [_discovery_record()], source="reuse-test")
        self.assertEqual(report.records_accepted, 1)
        self.assertGreater(self.conn.cursor_count, 0)
        _, total = self.storage.list_stations()
        self.assertEqual(total, 1)

    def test_failed_record_does_not_close_connection(self):
        bad = {"name": "", "organization_type": "radio_station"}
        report = self.storage.ingest_intelligence(
            [bad, _discovery_record()], source="reuse-test")
        self.assertEqual(report.records_accepted, 1)
        self.assertEqual(report.records_failed, 1)
        self.assertGreater(self.conn.cursor_count, 0)
        _, total = self.storage.list_stations()
        self.assertEqual(total, 1)

    def test_operations_after_ingest_still_use_connection(self):
        self.storage.ingest_intelligence(
            [_discovery_record()], source="reuse-test")
        _, total = self.storage.list_stations()
        self.assertEqual(total, 1)
        self.storage.ingest_intelligence(
            [_discovery_record()], source="reuse-test")
        _, total = self.storage.list_stations()
        self.assertEqual(total, 1)


class TestExistingIngestBehavior(unittest.TestCase):
    """Ensure the refactored code preserves all existing ingest behavior."""

    def setUp(self):
        self.conn = _RecordingConn()
        self.storage = PostgresStorage(conn=self.conn)

    def _jsonb(self, value):
        return json.loads(value) if isinstance(value, str) else value

    def test_discovery_record_maps_to_expected_tables(self):
        report = self.storage.ingest_intelligence([_discovery_record()],
                                                  source="tests")
        self.assertEqual(report.records_accepted, 1)
        clean, stable_id, kind = normalize_intelligence_record(
            _discovery_record())
        self.assertEqual(len(self.conn.org_inserts), 1)
        org_sql, org_params = self.conn.org_inserts[0]
        self.assertIn("ON CONFLICT(identity_key) DO UPDATE", org_sql)
        by_col = dict(zip(["identity_key", *_ORG_COLUMNS], org_params))
        self.assertEqual(by_col["identity_key"], stable_id)
        self.assertEqual(by_col["identity_kind"], kind)
        self.assertEqual(by_col["name"], "KQWX 91.5 FM")
        self.assertEqual(self._jsonb(by_col["genres"]), ["indie", "local"])
        self.assertEqual(len(self.conn.email_inserts), 1)
        self.assertEqual(len(self.conn.contact_inserts), 1)
        self.assertEqual(len(self.conn.submission_inserts), 1)
        self.assertEqual(self.conn.fetch_deletes, 1)
        self.assertEqual(len(self.conn.fetch_inserts), 1)

    def test_reingest_is_upsert(self):
        record = _discovery_record()
        _, stable_id, _ = normalize_intelligence_record(record)
        self.storage.ingest_intelligence([record], source="tests")
        self.conn.orgs[stable_id] = _Row(
            {col: None for col in _ORG_COLUMNS})
        self.conn.orgs[stable_id].update({
            "identity_key": stable_id,
            "first_stored_at": "2026-01-01T00:00:00+00:00",
            "last_stored_at": "2026-01-01T00:00:00+00:00",
        })
        report = self.storage.ingest_intelligence([record], source="tests")
        self.assertEqual(report.records_accepted, 1)
        self.assertEqual(report.stations_upserted, 1)
        inserts = [sql for sql, _ in self.conn.org_inserts]
        self.assertTrue(all("ON CONFLICT(identity_key)" in s
                            for s in inserts))


class TestPostgresResilience(unittest.TestCase):
    """Recovery when the shared PostgreSQL connection is poisoned.

    The whole API shares one connection under one coarse lock; a single
    failed/stuck query could otherwise wedge every later request (the
    frontend hangs on "connecting..."). These tests assert that an owned
    connection is discarded/reopened on error while injected (test)
    connections are left untouched.
    """

    def _blank(self, owns):
        storage = object.__new__(PostgresStorage)
        storage._owns_conn = owns
        storage._lock = threading.RLock()
        return storage

    def test_recover_connection_closes_and_reconnects_owned(self):
        storage = self._blank(True)
        closed = []
        conn = type("Fake", (), {"close": lambda self: closed.append(1)})()
        storage._conn = conn
        storage._connect = lambda: "fresh"
        storage._recover_connection()
        self.assertEqual(closed, [1])
        self.assertEqual(storage._conn, "fresh")

    def test_guard_recovers_owned_connection_on_error(self):
        storage = self._blank(True)
        recovered = []
        storage._conn = type(
            "Bad", (), {"cursor": lambda self: (_ for _ in ()).throw(
                RuntimeError("poisoned"))})()
        storage._recover_connection = lambda: recovered.append("recovered")
        with self.assertRaises(RuntimeError):
            with storage._guard() as conn:
                conn.cursor()
        self.assertIn("recovered", recovered)

    def test_guard_does_not_recover_injected_connection(self):
        storage = self._blank(False)
        bad_conn = type(
            "Bad", (), {"cursor": lambda self: (_ for _ in ()).throw(
                RuntimeError("boom"))})()
        storage._conn = bad_conn
        with self.assertRaises(RuntimeError):
            with storage._guard() as conn:
                conn.cursor()
        # The injected (test) connection is never closed or replaced, so the
        # persistent-connection contract holds even under an error.
        self.assertIs(storage._conn, bad_conn)


if __name__ == "__main__":
    unittest.main(verbosity=2)
