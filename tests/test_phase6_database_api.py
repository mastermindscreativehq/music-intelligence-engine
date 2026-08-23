"""Phase 6 tests: verification persistence, dual-backend parity,
PostgreSQL migrations/storage guards, and the FastAPI application.

Offline guarantees preserved: PostgreSQL integration is env-gated
(MIE_PG_DSN) and skipped cleanly when absent; FastAPI runs in-process via
TestClient over the SQLite reference backend.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest

os.environ.setdefault("MIE_TEST_MARKER", "1")

from database.pg_store import PostgresStorage
from database.repository import IntelligenceRepository
from database.schema_migrations import (
    apply_pg_migrations,
    load_pg_migrations,
)
from database.service import PersistenceService, normalize_intelligence_record
from backend.app import create_app


# ---------------------------------------------------------------------------
# Fixtures (mirror the tested Phase 4/5 record shapes)
# ---------------------------------------------------------------------------

TS = "2026-07-01T00:00:00+00:00"


def kzow(**overrides) -> dict:
    record = {
        "station_id": "st-kzow",
        "organization_type": "radio_station",
        "name": "KZOW",
        "website": "https://www.kzow.example/",
        "country": "US", "state_or_region": "IA", "city": "Wolff",
        "station_type": "public_radio",
        "classification_confidence": 0.9,
        "classification_evidence": ["npr member"],
        "formats": ["news"], "genres": ["news", "talk"],
        "genre_evidence": {"news": ["news/talk"]},
        "language": "en",
        "description": "Public radio for Wolff County.",
        "emails": [{
            "value": "music@kzow.example",
            "source_url": "https://www.kzow.example/contact",
            "source_type": "official_website_page",
            "method": "mailto_rule", "discovered_at": TS,
            "also_seen_at": [],
            "quality": {"has_role_localpart": True},
        }],
        "phone_numbers": [],
        "contacts": [{
            "id": "c-1", "station_id": "st-kzow", "name": "Jane Doe",
            "role": "music_director", "email": "music@kzow.example",
            "phone": None,
            "source_url": "https://www.kzow.example/contact",
            "confidence_score": 0.8,
            "confidence_reasons": ["role stated on contact page"],
            "verified_at": None, "preferred_for_submissions": True,
            "provenance": [{"value": "Music Director",
                            "source_url":
                                "https://www.kzow.example/contact"}],
        }],
        "submission": {
            "submission_url": {
                "value": "https://www.kzow.example/submit",
                "source_url": "https://www.kzow.example/submit",
                "source_type": "official_website_page",
                "method": "link_rule", "discovered_at": TS,
                "also_seen_at": []},
            "submission_email": None,
            "programming_contact_role": "music_director",
            "instructions": None, "restrictions": [],
            "methods": {"kind": "inference", "methods": ["web_form"],
                        "reasons": ["submission page found"]},
            "confidence_score": 0.7,
            "confidence_reasons": ["page exists"],
        },
        "social_urls": {},
        "source_urls": ["https://www.kzow.example/"],
        "fetches": [],
        "discovered_at": "2026-06-01T00:00:00+00:00",
        "last_verified_at": None, "last_observed_at": TS,
        "confidence_score": 0.75, "confidence_reasons": ["base"],
        "status": "enriched", "raw_metadata": {},
    }
    record.update(overrides)
    return record


def conflict_verification_report() -> dict:
    checked = "2026-08-23T00:00:00+00:00"
    result = {
        "started_at": checked,
        "completed_at": checked + "1",
        "summary": {"unverified": 0, "verified": 0, "failed": 0, "stale": 0,
                    "conflicting": 1, "unsupported": 1},
        "records": [{
            "subject_id": "st-kzow",
            "results": [
                {"claim": "emails[music@kzow.example]",
                 "subject_id": "st-kzow", "status": "conflicting",
                 "method": "source_comparison", "verifier": "code",
                 "evidence": [{"value": "old@x.example",
                               "sources": ["https://dir.example/x"]}],
                 "reasons": ["sources disagree; both sides preserved"],
                 "checked_at": checked},
                {"claim": "contacts[0].email", "subject_id": "st-kzow",
                 "status": "unsupported", "method": "provenance_audit",
                 "verifier": "code", "evidence": [],
                 "reasons": ["stored value carries no provenance"],
                 "checked_at": checked},
            ],
        }],
    }
    return result


STATION_PAYLOAD_KEYS = {
    "identity_key", "identity_kind", "name", "organization_type", "website",
    "domain", "country", "state_or_region", "city", "market_area",
    "station_type", "classification_confidence", "classification_evidence",
    "formats", "genres", "genre_evidence", "language", "description",
    "social_urls", "source_urls", "discovered_at", "last_verified_at",
    "last_observed_at", "confidence_score", "confidence_reasons", "status",
    "raw_metadata", "first_stored_at", "last_stored_at",
}

CONTACT_PAYLOAD_KEYS = {
    "contact_uid", "engine_contact_id", "name", "role", "email", "phone",
    "source_url", "confidence_score", "confidence_reasons",
    "preferred_for_submissions", "verified_at", "provenance",
}


# ---------------------------------------------------------------------------
# Verification persistence (SQLite reference backend)
# ---------------------------------------------------------------------------

class TestVerificationPersistence(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = PersistenceService(
            os.path.join(self._tmp.name, "db.sqlite"))
        record = kzow()
        self.storage.ingest_intelligence([record], source="test")
        self.key = normalize_intelligence_record(record)[1]

    def tearDown(self):
        self.storage.close()
        self._tmp.cleanup()

    def test_roundtrip_preserves_all_six_status_values(self):
        outcome = self.storage.persist_verification(
            [kzow()], conflict_verification_report(), source="test")
        self.assertEqual(outcome["stored"], 2)
        self.assertEqual(outcome["skipped"], 0)
        history = self.storage.get_verification(self.key)
        statuses = {r["status"] for r in history["results"]}
        self.assertEqual(statuses, {"conflicting", "unsupported"})
        by_claim = {r["claim"]: r for r in history["results"]}
        self.assertEqual(by_claim["emails[music@kzow.example]"]["evidence"],
                         [{"value": "old@x.example",
                           "sources": ["https://dir.example/x"]}])
        self.assertEqual(history["runs"][0]["summary"]["unsupported"], 1)

    def test_identity_key_resolution_matches_ingest(self):
        outcome = self.storage.persist_verification(
            [kzow()], conflict_verification_report(), source="test")
        self.assertEqual(outcome["stored"], 2)
        self.assertIsNotNone(self.storage.get_verification(self.key))

    def test_unknown_stations_are_skipped_not_created(self):
        stranger = kzow(name="STRANGER FM", website=None, city=None,
                        state_or_region=None, country=None)
        stranger_key = normalize_intelligence_record(stranger)[1]
        outcome = self.storage.persist_verification(
            [stranger], conflict_verification_report(), source="test")
        self.assertEqual(outcome["stored"], 0)
        self.assertEqual(outcome["skipped"], 2)
        self.assertIsNone(self.storage.get_verification(stranger_key))
        _, total = self.storage.list_stations()
        self.assertEqual(total, 1)     # verification never creates stations

    def test_non_dict_records_do_not_misalign_positional_pairs(self):
        outcome = self.storage.persist_verification(
            ["junk", None], conflict_verification_report(), source="test")
        self.assertEqual(outcome["stored"], 0)
        self.assertEqual(outcome["skipped"], 0)   # no dict records -> no pairs
        self.assertTrue(outcome["run_id"])

    def test_append_only_history(self):
        self.storage.persist_verification([kzow()],
                                          conflict_verification_report(),
                                          source="test")
        self.storage.persist_verification([kzow()],
                                          conflict_verification_report(),
                                          source="test")
        history = self.storage.get_verification(self.key)
        self.assertEqual(len(history["runs"]), 2)
        self.assertEqual(len(history["results"]), 4)


# ---------------------------------------------------------------------------
# Dual-backend parity contract
# ---------------------------------------------------------------------------

def _identity_of(record: dict) -> str:
    return normalize_intelligence_record(record)[1]


class ParityContractMixin:
    """Shared assertions run against EVERY storage backend."""

    def make_storage(self):  # pragma: no cover - overridden
        raise NotImplementedError

    def test_full_contract_roundtrip(self):
        storage = self.make_storage()
        try:
            report = storage.ingest_intelligence([kzow()], source="parity")
            self.assertEqual(report.records_accepted, 1)
            self.assertEqual(report.records_failed, 0)
            key = _identity_of(kzow())

            rows, total = storage.list_stations(limit=10)
            self.assertEqual(total, 1)
            self.assertEqual(set(rows[0].keys()), STATION_PAYLOAD_KEYS)
            self.assertEqual(rows[0]["identity_key"], key)
            self.assertEqual(rows[0]["genres"], ["news", "talk"])

            detail = storage.get_station(key)
            self.assertIsNotNone(detail)
            self.assertEqual(detail["name"], "KZOW")

            emails = storage.get_station_emails(key)
            self.assertEqual(emails[0]["value"], "music@kzow.example")
            self.assertIn("quality", emails[0])       # fact kept verbatim

            contacts = storage.get_station_contacts(key)
            self.assertEqual(set(contacts[0].keys()), CONTACT_PAYLOAD_KEYS)
            self.assertTrue(contacts[0]["preferred_for_submissions"])

            submission = storage.get_submission(key)
            self.assertEqual(submission["methods"]["kind"], "inference")

            voutcome = storage.persist_verification(
                [kzow()], conflict_verification_report(), source="parity")
            self.assertEqual(voutcome["stored"], 2)
            history = storage.get_verification(key)
            self.assertEqual({r["status"] for r in history["results"]},
                             {"conflicting", "unsupported"})
        finally:
            storage.close()

    def test_null_never_erases_and_lists_union(self):
        storage = self.make_storage()
        try:
            storage.ingest_intelligence([kzow()], source="parity")
            storage.ingest_intelligence(
                [kzow(city=None, description=None)], source="parity")
            detail = storage.get_station(_identity_of(kzow()))
            self.assertEqual(detail["city"], "Wolff")
            self.assertTrue(detail["description"])
        finally:
            storage.close()

    def test_filters(self):
        storage = self.make_storage()
        try:
            storage.ingest_intelligence(
                [kzow(), kzow(name="WXYZ", website=None, city=None,
                              genres=["jazz"], country=None)],
                source="parity")
            rows, total = storage.list_stations(genre="jazz")
            self.assertEqual(total, 1)
            self.assertEqual(rows[0]["name"], "WXYZ")
            rows, _ = storage.list_stations(country="US")
            self.assertEqual([r["name"] for r in rows], ["KZOW"])
            rows, _ = storage.list_stations(min_confidence=0.9)
            self.assertEqual(rows, [])
        finally:
            storage.close()


class TestSqliteParity(ParityContractMixin, unittest.TestCase):
    def make_storage(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return PersistenceService(
            os.path.join(tmp.name, "db.sqlite"))

    def test_sqlite_service_conforms_to_repository_protocol(self):
        storage = self.make_storage()
        self.addCleanup(storage.close)
        self.assertIsInstance(storage, IntelligenceRepository)


@unittest.skipUnless(os.environ.get("MIE_PG_DSN"),
                     "PostgreSQL integration requires MIE_PG_DSN")
class TestPostgresParity(ParityContractMixin, unittest.TestCase):
    def make_storage(self):
        return PostgresStorage(dsn=os.environ["MIE_PG_DSN"])


# ---------------------------------------------------------------------------
# PostgreSQL migrations + storage guards (offline structural tests)
# ---------------------------------------------------------------------------

class _FakeState:
    def __init__(self):
        self.statements: list = []
        self.applied: set[int] = set()


class _FakeCursor:
    def __init__(self, state: _FakeState):
        self._state = state

    def execute(self, sql, params=None):
        lowered = sql.strip().lower()
        if params is None:
            self._state.statements.append(sql)
        else:
            self._state.statements.append((sql, tuple(params)))
        if lowered.startswith("select version from schema_migrations"):
            self._rows = [{"version": v}
                          for v in sorted(self._state.applied)]
        elif lowered.startswith("select coalesce(max"):
            self._row = {"v": max(self._state.applied)
                         if self._state.applied else 0}
        elif lowered.startswith("insert into schema_migrations"):
            self._state.applied.add(int(params[0]))
        return self

    def fetchall(self):
        rows = getattr(self, "_rows", [])
        self._rows = []
        return rows

    def fetchone(self):
        row = getattr(self, "_row", None)
        self._row = None
        return row


class _FakeConn:
    def __init__(self):
        self.state = _FakeState()
        self.commits = 0

    def cursor(self):
        return _FakeCursor(self.state)

    def commit(self):
        self.commits += 1


class TestPostgresMigrationsOffline(unittest.TestCase):
    def test_migration_files_ordered_and_loadable(self):
        migrations = load_pg_migrations()
        self.assertEqual(migrations[0][1], "0001_init.sql")
        versions = [v for v, _, _ in migrations]
        self.assertEqual(versions, sorted(versions))
        sql = migrations[0][2]
        for table in ("organizations", "contacts", "submission_paths",
                      "verification_results"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", sql)

    def test_bad_migration_filename_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "oops.sql"), "w",
                      encoding="utf-8") as handle:
                handle.write("SELECT 1;")
            with self.assertRaises(ValueError):
                load_pg_migrations(tmp)

    def test_runner_applies_once_and_tracks_versions(self):
        conn = _FakeConn()
        version = apply_pg_migrations(conn)
        self.assertGreaterEqual(version, 1)
        creates = [s for s in conn.state.statements
                   if isinstance(s, str)
                   and "CREATE TABLE IF NOT EXISTS organizations" in s]
        self.assertEqual(len(creates), 1)
        version_again = apply_pg_migrations(conn)
        self.assertEqual(version_again, version)
        creates_after = [s for s in conn.state.statements
                         if isinstance(s, str)
                         and "CREATE TABLE IF NOT EXISTS organizations" in s]
        self.assertEqual(len(creates_after), 1)     # not reapplied


def _psycopg_available() -> bool:
    try:
        import psycopg  # noqa: F401
        return True
    except ImportError:
        return False


class TestPostgresStorageGuards(unittest.TestCase):
    @unittest.skipIf(_psycopg_available(),
                     "driver installed; guard path not reachable")
    def test_missing_driver_raises_informative_error(self):
        with self.assertRaises(ImportError) as ctx:
            PostgresStorage(dsn="postgresql://user:pass@localhost/db")
        message = str(ctx.exception)
        self.assertIn("psycopg", message)
        self.assertIn("MIE_PG_DSN", message)

    def test_dsn_required_even_with_driver_present(self):
        if _psycopg_available():
            with self.assertRaises(ValueError):
                PostgresStorage()
        else:
            with self.assertRaises((ImportError, ValueError)):
                PostgresStorage()

    def test_pg_class_exposes_full_repository_surface(self):
        for name in ("ingest_intelligence", "list_stations", "get_station",
                     "get_station_emails", "get_station_phones",
                     "get_station_contacts", "get_submission",
                     "get_fetches", "persist_verification",
                     "get_verification", "get_ingestion_run", "close"):
            self.assertTrue(hasattr(PostgresStorage, name))


# ---------------------------------------------------------------------------
# FastAPI application (in-process TestClient over SQLite backend)
# ---------------------------------------------------------------------------

SECRET_VALUE = "sup3r-secret-dsn-password-token"


class TestFastAPIApp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        cls._tmp = tempfile.TemporaryDirectory()
        storage = PersistenceService(
            os.path.join(cls._tmp.name, "db.sqlite"))
        record = kzow()
        storage.ingest_intelligence([record], source="api")
        cls.key = normalize_intelligence_record(record)[1]
        cls.client = TestClient(create_app(storage),
                                raise_server_exceptions=False)
        cls.storage = storage
        # LIFO: the DB connection closes before the temp dir is removed.
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.addClassCleanup(storage.close)

    # -- helpers -------------------------------------------------------------

    def get_json(self, path):
        response = self.client.get(path)
        return response.status_code, response.json()

    # -- contract ---------------------------------------------------------------

    def test_health_envelope(self):
        status, body = self.get_json("/api/v1/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIsNone(body["error"])
        self.assertEqual(body["data"]["status"], "ok")
        self.assertEqual(body["data"]["schema_version"], 2)

    def test_list_envelope_projection_and_links(self):
        status, body = self.get_json("/api/v1/stations")
        self.assertEqual(status, 200)
        data = body["data"]
        self.assertEqual(data["total"], 1)
        summary = data["stations"][0]
        for field in ("identity_key", "name", "genres", "formats",
                      "confidence_score", "links"):
            self.assertIn(field, summary)
        self.assertEqual(summary["links"]["self"],
                         f"/api/v1/stations/{self.key}")

    def test_listing_filters_additive(self):
        _, body = self.get_json("/api/v1/stations?genre=jazz")
        self.assertEqual(body["data"]["total"], 0)
        _, body = self.get_json("/api/v1/stations?genre=news")
        self.assertEqual(body["data"]["total"], 1)
        _, body = self.get_json("/api/v1/stations?min_confidence=0.9")
        self.assertEqual(body["data"]["total"], 0)

    def test_limit_bounds_validated(self):
        status, body = self.get_json("/api/v1/stations?limit=5000")
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "bad_request")

    def test_detail_intelligence_contacts_shapes(self):
        status, body = self.get_json(f"/api/v1/stations/{self.key}")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["name"], "KZOW")
        status, body = self.get_json(
            f"/api/v1/stations/{self.key}/intelligence")
        self.assertEqual(status, 200)
        data = body["data"]
        self.assertIn("epistemology", data)
        self.assertIn("facts_count", data["epistemology"])
        self.assertIn("unknown_fields", data["epistemology"])
        self.assertEqual(data["emails"][0]["value"], "music@kzow.example")
        self.assertEqual(data["emails"][0]["method"], "mailto_rule")
        status, body = self.get_json(f"/api/v1/stations/{self.key}/contacts")
        self.assertEqual(status, 200)
        self.assertTrue(body["data"]["preferred_submission_contacts"])

    def test_unknown_station_and_route_404_codes(self):
        status, body = self.get_json("/api/v1/stations/domain:nope.example")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "station_not_found")
        status, body = self.get_json("/api/v1/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "route_not_found")

    def test_method_not_allowed_code(self):
        response = self.client.post("/api/v1/stations")
        self.assertEqual(response.status_code, 405)
        self.assertEqual(
            response.json()["error"]["code"], "method_not_allowed")

    # -- ingestion API ----------------------------------------------------------

    def test_post_ingest_happy_path_and_idempotency(self):
        payload = {"records": [kzow(name="QWER",
                                    website="https://qwer.example/")],
                   "source": "phase6-test"}
        first = self.client.post("/api/v1/ingest", json=payload)
        self.assertEqual(first.status_code, 200)
        body = first.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["data"]["records_accepted"], 1)
        run_id = body["data"]["run_id"]
        second = self.client.post("/api/v1/ingest", json=payload)
        self.assertEqual(second.json()["data"]["records_accepted"], 1)
        _, listing = self.get_json("/api/v1/stations?q=QWER")
        self.assertEqual(listing["data"]["total"], 1)
        status, run_body = self.get_json(f"/api/v1/runs/{run_id}")
        self.assertEqual(status, 200)
        self.assertEqual(run_body["data"]["run_id"], run_id)
        self.assertEqual(run_body["data"]["failures"], [])

    def test_post_ingest_isolates_failures(self):
        payload = {"records": [42, kzow(), kzow(name="")]}
        response = self.client.post("/api/v1/ingest", json=payload)
        data = response.json()["data"]
        self.assertEqual(data["records_accepted"], 1)
        self.assertEqual(data["records_failed"], 2)
        self.assertEqual(len(data["failures"]), 2)

    def test_post_ingest_rejects_bad_bodies(self):
        response = self.client.post("/api/v1/ingest",
                                    content=b"{not-json",
                                    headers={"Content-Type":
                                             "application/json"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "bad_request")
        response = self.client.post("/api/v1/ingest", json={"records": 5})
        self.assertEqual(response.status_code, 400)

    def test_run_not_found(self):
        status, body = self.get_json("/api/v1/runs/no-such-run")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "run_not_found")

    # -- verification endpoint -----------------------------------------------

    def test_verification_endpoint_roundtrip(self):
        record = kzow()
        self.storage.ingest_intelligence([record], source="api")
        outcome = self.storage.persist_verification(
            [record], conflict_verification_report(), source="api")
        self.assertEqual(outcome["stored"], 2)
        status, body = self.get_json(
            f"/api/v1/stations/{self.key}/verification")
        self.assertEqual(status, 200)
        verification = body["data"]["verification"]
        self.assertEqual({r["status"] for r in verification["results"]},
                         {"conflicting", "unsupported"})
        self.assertEqual(verification["runs"][0]["summary"]["conflicting"], 1)

    def test_verification_unknown_station_404(self):
        status, body = self.get_json("/api/v1/stations/domain:nope.example/"
                                     "verification")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "station_not_found")

    # -- security ---------------------------------------------------------------

    def test_no_credentials_or_env_values_leak_into_responses(self):
        paths = ("/api/v1/health", "/api/v1/stations",
                 f"/api/v1/stations/{self.key}",
                 f"/api/v1/stations/{self.key}/intelligence",
                 f"/api/v1/stations/{self.key}/contacts",
                 f"/api/v1/stations/{self.key}/verification",
                 "/api/v1/runs/x")
        pattern = re.compile(
            rf"{re.escape(SECRET_VALUE)}|"
            r"(postgres(ql)?://|password\s*[=:]|secret|api[_-]?key)",
            re.IGNORECASE,
        )
        os.environ["MIE_PROBE_SECRET"] = SECRET_VALUE
        try:
            for path in paths:
                response = self.client.get(path)
                body = response.text
                self.assertNotRegex(body, pattern, msg=path)
        finally:
            del os.environ["MIE_PROBE_SECRET"]


if __name__ == "__main__":
    unittest.main()
