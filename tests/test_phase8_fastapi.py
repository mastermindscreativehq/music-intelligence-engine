"""Phase 8 FastAPI adapter parity (backend.app).

Proves the six Phase 8 routes are registered on the FastAPI application
and that they DELEGATE to the same submissions domain functions and
contract projections as the stdlib servers (backend.routes), producing
identical envelopes/codes:

    upload -> 201 ready | 413 payload_too_large | 422 track_rejected
    listing/detail -> 200 | 400 bad_request | 404 track_not_found
    submission view/history -> 200 | 404 station_not_found
    checks run -> 200 (ssrf_blocked entries recorded WITHOUT contact)

Network is never touched: fetches go through a deterministic double and
every target host is an IP literal so the SSRF guard short-circuits
without DNS (production default ``allow_private=False`` is in effect).
"""

import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.contracts import TRACK_FIELDS
from crawler.http import FetchResult
from database.schema import SCHEMA_VERSION
from database.service import PersistenceService
from submissions.storage import LocalTrackStore
from tests.test_phase6_database_api import kzow
from tests.test_phase8_submissions import FakeFetcher, _identity_key, \
    _submission_with, mp3_bytes, sha256_of, track_id_for

PUBLIC_OK = "http://93.184.216.34/submit"
PUBLIC_404 = "http://93.184.216.34/guide"


class TestFastAPIPhaseParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.tmp_root = tmp.name
        cls.store_root = os.path.join(tmp.name, "assets")
        cls.storage = PersistenceService(
            os.path.join(tmp.name, "db.sqlite"))
        record = kzow()
        cls.storage.ingest_intelligence([record], source="tests")
        cls.kzow_key = _identity_key(record)
        cls.fetcher = FakeFetcher(results={
            PUBLIC_OK: FetchResult(url=PUBLIC_OK, status=200,
                                   content_type="text/html",
                                   body="<html>ok</html>"),
            PUBLIC_404: FetchResult(url=PUBLIC_404,
                                    error_kind="http_status", status=404),
        })
        cls.client = TestClient(
            create_app(cls.storage,
                       track_store=LocalTrackStore(cls.store_root),
                       link_fetcher=cls.fetcher),
            raise_server_exceptions=False)
        # LIFO: the DB connection closes before the temp dir is removed.
        cls.addClassCleanup(tmp.cleanup)
        cls.addClassCleanup(cls.storage.close)

    # -- uploads -------------------------------------------------------------

    def test_upload_returns_created_opaque_projection(self):
        payload = mp3_bytes(pad=2048)
        response = self.client.post(
            "/api/v1/tracks", content=payload,
            params={"filename": "parity demo.mp3"})
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertTrue(body["ok"])
        data = body["data"]
        self.assertEqual(data["track_id"],
                         track_id_for(sha256_of(payload)))
        self.assertEqual(data["status"], "ready")
        self.assertEqual(data["original_filename"], "parity demo.mp3")
        self.assertEqual(data["links"]["self"],
                         f"/api/v1/tracks/{data['track_id']}")
        self.assertTrue(LocalTrackStore(self.store_root).contains(
            data["track_id"]))
        text = repr(body)
        for fragment in (self.tmp_root, self.store_root, "storage_path"):
            self.assertNotIn(fragment, text)

    def test_duplicate_upload_is_idempotent(self):
        payload = mp3_bytes(pad=512)
        first = self.client.post("/api/v1/tracks",
                                 content=payload).json()["data"]
        second = self.client.post(
            "/api/v1/tracks", content=payload,
            params={"filename": "again.mp3"}).json()["data"]
        self.assertEqual(first["track_id"], second["track_id"])

    def test_wrong_content_maps_to_track_rejected_and_quarantines(self):
        response = self.client.post("/api/v1/tracks",
                                    content=b"<html>no audio</html>")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"],
                         "track_rejected")
        listing = self.client.get(
            "/api/v1/tracks", params={"status": "quarantined"})
        self.assertEqual(listing.status_code, 200)
        quarantined = listing.json()["data"]
        self.assertGreaterEqual(quarantined["total"], 1)
        self.assertIn("MP3", quarantined["tracks"][0]["reject_reason"])

    def test_empty_body_maps_to_track_rejected(self):
        response = self.client.post("/api/v1/tracks")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"],
                         "track_rejected")

    def test_oversize_maps_to_payload_too_large_without_row(self):
        before = self.client.get("/api/v1/tracks").json()["data"]["total"]
        response = self.client.post(
            "/api/v1/tracks", content=bytes(20 * 1024 * 1024 + 1))
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"]["code"],
                         "payload_too_large")
        after = self.client.get("/api/v1/tracks").json()["data"]["total"]
        self.assertEqual(after, before)

    # -- listings + details ----------------------------------------------------

    def test_listing_envelope_shape_and_projection_fields(self):
        response = self.client.get("/api/v1/tracks",
                                   params={"limit": 2})
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["limit"], 2)
        self.assertLessEqual(len(data["tracks"]), 2)
        for track in data["tracks"]:
            self.assertEqual(set(track) - {"links"}, set(TRACK_FIELDS))

    def test_bad_status_filter_maps_to_bad_request(self):
        response = self.client.get("/api/v1/tracks",
                                   params={"status": "bogus"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "bad_request")

    def test_bad_limit_values_map_to_bad_request(self):
        for params in ({"limit": "abc"}, {"limit": 0}, {"limit": 5000}):
            with self.subTest(params=params):
                response = self.client.get("/api/v1/tracks",
                                           params=params)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"],
                                 "bad_request")

    def test_unknown_or_malformed_track_ids_map_to_track_not_found(self):
        # Intentional FastAPI divergence: any single-segment id reaches
        # the handler, so malformed ids answer track_not_found (the
        # stdlib dispatcher answers route_not_found for the same path).
        for track_id in (track_id_for("c" * 64), "not-a-hash"):
            with self.subTest(track_id=track_id):
                response = self.client.get(f"/api/v1/tracks/{track_id}")
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json()["error"]["code"],
                                 "track_not_found")

    # -- submission view + accessibility ---------------------------------------

    def test_submission_view_for_known_station(self):
        response = self.client.get(
            f"/api/v1/stations/{self.kzow_key}/submission")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["identity_key"], self.kzow_key)
        self.assertTrue(data["submission"]["submission_url"]["value"])
        self.assertEqual(data["last_checks"], [])

    def test_submission_view_unknown_station_maps_to_station_not_found(self):
        response = self.client.get(
            "/api/v1/stations/domain:missing.example/submission")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"],
                         "station_not_found")

    def test_checks_run_delegates_through_guard_and_records_history(self):
        record = _submission_with(
            PUBLIC_OK, PUBLIC_404,
            name="Parity Checks Radio",
            website="https://parity-checks.example/")
        self.storage.ingest_intelligence([record], source="tests")
        key = _identity_key(record)

        response = self.client.post(
            f"/api/v1/stations/{key}/submission/checks")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["targets"], 2)
        self.assertEqual(data["reachable"], 1)
        by_kind = {c["target_kind"]: c for c in data["checks"]}
        self.assertTrue(by_kind["submission_url"]["ok"])
        self.assertEqual(by_kind["submission_url"]["status"], 200)
        self.assertFalse(by_kind["instructions_page"]["ok"])
        self.assertEqual(by_kind["instructions_page"]["status"], 404)
        self.assertEqual(by_kind["instructions_page"]["error_kind"],
                         "http_status")
        self.assertEqual(set(self.fetcher.calls),
                         {PUBLIC_OK, PUBLIC_404})

        history = self.client.get(
            f"/api/v1/stations/{key}/submission/checks",
            params={"limit": 10}).json()["data"]
        self.assertEqual(len(history["checks"]), 2)
        self.assertEqual(history["checks"][0]["url"], PUBLIC_404)
        last_urls = {c["url"] for c in history["last_by_target"]}
        self.assertEqual(last_urls, {PUBLIC_OK, PUBLIC_404})

    def test_checks_refuse_private_targets_without_contact(self):
        record = _submission_with(
            "http://127.0.0.1:9/private",
            name="Parity Ssrf Radio",
            website="https://parity-ssrf.example/")
        self.storage.ingest_intelligence([record], source="tests")
        key = _identity_key(record)
        self.fetcher.calls.clear()
        response = self.client.post(
            f"/api/v1/stations/{key}/submission/checks")
        self.assertEqual(response.status_code, 200)
        for entry in response.json()["data"]["checks"]:
            self.assertFalse(entry["ok"])
            self.assertEqual(entry["error_kind"], "ssrf_blocked")
            self.assertIsNone(entry["latency_ms"])
        self.assertEqual(self.fetcher.calls, [])

    def test_checks_on_unknown_station_map_to_station_not_found(self):
        for method, call in (
                ("POST", lambda: self.client.post(
                    "/api/v1/stations/domain:nope.example/submission"
                    "/checks")),
                ("GET", lambda: self.client.get(
                    "/api/v1/stations/domain:nope.example/submission"
                    "/checks"))):
            with self.subTest(method=method):
                response = call()
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json()["error"]["code"],
                                 "station_not_found")

    # -- cross-cutting -----------------------------------------------------------

    def test_unsupported_verb_maps_to_method_not_allowed(self):
        response = self.client.put("/api/v1/tracks")
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.json()["error"]["code"],
                         "method_not_allowed")

    def test_health_still_reports_current_schema_version(self):
        response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["schema_version"],
                         SCHEMA_VERSION)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
