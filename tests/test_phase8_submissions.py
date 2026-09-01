"""Phase 8: music submission assets + link accessibility.

Covers the approved boundary corrections:

- Assets are addressed by OPAQUE keys only (``sha256:<hex>``); no test
  may observe a filesystem path crossing the repository/API boundary,
  and every envelope is scanned to enforce that.
- The ``submissions/`` domain owns validation, storage, metadata, and
  reachability checks; nothing here composes or sends outreach
  (that is Phase 9 territory and stays out of scope).
- Link accessibility is bounded + polite (crawler fetcher, robots.txt)
  and refuses private addresses WITHOUT contacting them.

Layers exercised, mirroring the suite architecture of phases 4-7:
validation/storage/links units -> SQLite repository contract ->
dispatcher (backend.routes) contract with injected doubles -> live
stdlib webapp over real HTTP against a local stub site.
"""

import hashlib
import http.server
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from backend.contracts import TRACK_FIELDS
from backend.routes import ROUTE_TABLE, dispatch
from backend.webapp import DEFAULT_STATIC_ROOT, create_server
from crawler.http import FetchResult, StdlibHttpFetcher
from database.repository import IntelligenceRepository
from database.schema import SCHEMA_VERSION
from database.schema_migrations import load_pg_migrations
from database.service import PersistenceService, \
    normalize_intelligence_record
from submissions import service as sub_service
from submissions.links import SsrfBlocked, assert_public_url, \
    extract_check_targets
from submissions.storage import DEFAULT_STORAGE_ROOT, LocalTrackStore, \
    track_id_for
from submissions.validation import DEFAULT_MAX_BYTES, looks_like_mp3, \
    sanitize_filename, validate_upload
from tests.test_phase6_database_api import kzow


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def mp3_bytes(*, tag=True, pad=1024):
    """Minimal payload that passes MP3 validation."""
    head = b"ID3\x04\x00\x00\x00\x00\x00\x00" if tag else b"\xff\xfb\x90\x44"
    return head + bytes(pad)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeFetcher:
    """Deterministic fetcher double that never touches the network."""

    def __init__(self, results=None):
        self.calls = []
        self.results = results or {}

    def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        result = self.results.get(url)
        if result is None:
            result = FetchResult(url=url, status=200,
                                 content_type="text/html", body="ok")
        return result


def _submission_with(submission_url, instructions_source=None, *,
                     name="Submission Fixture Radio",
                     website="https://fixture.example/"):
    """A kzow-shaped record whose submission targets are overridden.

    *name*/*website* choose the identity, so callers that share one
    database must use distinct domains to avoid merge collisions.
    """
    record = kzow()
    record["name"] = name
    record["website"] = website
    submission = dict(record.get("submission") or {})
    url_fact = dict(submission.get("submission_url") or {})
    url_fact["value"] = submission_url
    if url_fact.get("source_url"):
        url_fact["source_url"] = submission_url
    submission["submission_url"] = url_fact
    if instructions_source is not None:
        instructions = dict(submission.get("instructions") or {})
        instructions["source_url"] = instructions_source
        submission["instructions"] = instructions
    record["submission"] = submission
    return record


def _identity_key(record) -> str:
    return normalize_intelligence_record(record)[1]


class _StubHandler(http.server.BaseHTTPRequestHandler):
    """Loopback site for live accessibility checks."""

    def do_GET(self):
        pages = {
            "/robots.txt": ("text/plain",
                            b"User-agent: *\nDisallow: /blocked\n"),
            "/ok": ("text/html", b"<html>music submissions</html>"),
            "/binary": ("application/octet-stream", b"\x00\x01\x02"),
            "/missing": (None, b"gone"),
            "/blocked": ("text/html", b"<html>hidden</html>"),
        }
        content_type, payload = pages.get(self.path, (None, b"nope"))
        status = 200 if content_type is not None else (
            404 if self.path == "/missing" else 404)
        if self.path == "/missing":
            status = 404
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


# ---------------------------------------------------------------------------
# upload validation (submissions.validation)
# ---------------------------------------------------------------------------

class TestUploadValidation(unittest.TestCase):
    def test_valid_id3_and_sync_headers_accepted(self):
        self.assertTrue(looks_like_mp3(mp3_bytes(tag=True)))
        self.assertTrue(looks_like_mp3(mp3_bytes(tag=False)))

    def test_non_mp3_payloads_rejected(self):
        for payload in (b"", b"<html>not music</html>",
                        b"RIFF....WAVE", bytes(64)):
            with self.subTest(payload=payload[:12]):
                self.assertFalse(looks_like_mp3(payload))

    def test_free_and_reserved_bitrate_frames_rejected(self):
        # bitrate index 0000 (free) and 1111 (reserved) are invalid
        self.assertFalse(looks_like_mp3(b"\xff\xfb\x00\x44" + bytes(64)))
        self.assertFalse(looks_like_mp3(b"\xff\xfb\xf0\x44" + bytes(64)))

    def test_validate_upload_accepts_small_mp3(self):
        verdict = validate_upload(mp3_bytes())
        self.assertTrue(verdict["accepted"])
        self.assertIsNone(verdict["reason"])

    def test_validate_upload_reports_oversize_before_content(self):
        verdict = validate_upload(bytes(DEFAULT_MAX_BYTES + 1))
        self.assertFalse(verdict["accepted"])
        self.assertIn("exceeds", verdict["reason"])
        self.assertIn(str(DEFAULT_MAX_BYTES), verdict["reason"])

    def test_validate_upload_reports_wrong_content(self):
        verdict = validate_upload(b"<html>nope</html>")
        self.assertFalse(verdict["accepted"])
        self.assertIn("MP3", verdict["reason"])

    def test_sanitize_filename_strips_paths(self):
        self.assertEqual(sanitize_filename("../../etc/passwd"), "passwd")
        self.assertEqual(sanitize_filename("..\\..\\win.ini"), "win.ini")
        self.assertEqual(sanitize_filename("a/b\\c.mp3"), "c.mp3")

    def test_sanitize_filename_fallback_and_trimming(self):
        self.assertEqual(sanitize_filename(""), "upload.mp3")
        self.assertEqual(sanitize_filename("   "), "upload.mp3")
        long_name = "x" * 300 + ".mp3"
        cleaned = sanitize_filename(long_name)
        self.assertEqual(len(cleaned), 200)
        self.assertTrue(cleaned.endswith(".mp3"))

    def test_sanitize_filename_preserves_plain_names(self):
        self.assertEqual(sanitize_filename("demo song.mp3"),
                         "demo song.mp3")


# ---------------------------------------------------------------------------
# opaque asset storage (submissions.storage)
# ---------------------------------------------------------------------------

class TestLocalTrackStore(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.join(tmp.name, "assets")
        self.store = LocalTrackStore(self.root)

    def test_put_read_roundtrip(self):
        payload = mp3_bytes(pad=2048)
        key = self.store.put(payload)
        self.assertTrue(key.startswith("sha256:"))
        self.assertEqual(key, track_id_for(sha256_of(payload)))
        self.assertEqual(self.store.read(key), payload)

    def test_identical_bytes_deduplicate_to_one_blob(self):
        payload = mp3_bytes(pad=512)
        first = self.store.put(payload)
        second = self.store.put(payload)
        self.assertEqual(first, second)
        blobs = []
        for base, _dirs, files in os.walk(self.root):
            blobs.extend(files)
        self.assertEqual(len(blobs), 1)

    def test_blob_layout_is_internal_shard(self):
        digest = sha256_of(mp3_bytes(pad=64))
        key = self.store.put(mp3_bytes(pad=64))
        relative = os.path.relpath(
            self.store._blob_path(digest), self.root)
        parts = relative.split(os.sep)
        self.assertEqual(parts[0], "blobs")
        self.assertEqual(parts[1], digest[:2])
        self.assertEqual(parts[2], f"{digest}.mp3")

    def test_contains_tracks_presence(self):
        key = self.store.put(mp3_bytes(pad=32))
        self.assertTrue(self.store.contains(key))
        self.assertFalse(
            self.store.contains(track_id_for("f" * 64)))

    def test_unknown_or_malformed_keys_raise_keyerror(self):
        good_but_missing = track_id_for("a" * 64)
        with self.assertRaises(KeyError):
            self.store.read(good_but_missing)
        for bad in ("nonsense", "md5:" + "a" * 64, "sha256:xyz",
                    "sha256:" + "g" * 64):
            with self.subTest(key=bad):
                with self.assertRaises(KeyError):
                    self.store.read(bad)
                with self.assertRaises(KeyError):
                    self.store.contains(bad)

    def test_atomic_write_leaves_no_part_files(self):
        self.store.put(mp3_bytes(pad=16))
        leftovers = []
        for base, _dirs, files in os.walk(self.root):
            leftovers.extend(f for f in files if f.endswith(".part"))
        self.assertEqual(leftovers, [])

    def test_default_root_points_into_repo_data_tree(self):
        store = LocalTrackStore()
        self.assertEqual(store._root, DEFAULT_STORAGE_ROOT)

# ---------------------------------------------------------------------------
# link targets + SSRF guard (submissions.links)
# ---------------------------------------------------------------------------

class TestLinkTargets(unittest.TestCase):
    def test_submission_url_and_instructions_page_extracted_in_order(self):
        targets = extract_check_targets({
            "submission_url": {"value": "https://a.example/submit"},
            "instructions": {"source_url": "https://a.example/how-to",
                             "value": "send mp3"},
        })
        self.assertEqual(targets, [
            ("https://a.example/submit", "submission_url"),
            ("https://a.example/how-to", "instructions_page"),
        ])

    def test_duplicate_url_is_kept_per_evidence_kind(self):
        # dedup is keyed on (url, target_kind): the same endpoint may be
        # advertised as both the submission URL and the instructions page,
        # and each evidence slot is checked (and audited) separately.
        targets = extract_check_targets({
            "submission_url": {"value": "https://a.example/submit"},
            "instructions": {"source_url": "https://a.example/submit"},
        })
        self.assertEqual(targets, [
            ("https://a.example/submit", "submission_url"),
            ("https://a.example/submit", "instructions_page"),
        ])

    def test_non_http_schemes_and_hostless_urls_skipped(self):
        targets = extract_check_targets({
            "submission_url": {"value": "ftp://a.example/drop"},
            "instructions": {"source_url": "http:///no-host"},
        })
        self.assertEqual(targets, [])

    def test_whitespace_is_stripped(self):
        targets = extract_check_targets({
            "submission_url": {"value": "  https://a.example/x  "},
        })
        self.assertEqual(targets, [("https://a.example/x",
                                    "submission_url")])

    def test_missing_or_malformed_payloads_yield_no_targets(self):
        for payload in (None, {}, "https://a.example",
                        {"submission_url": None},
                        {"submission_url": {"value": ""}}):
            with self.subTest(payload=payload):
                self.assertEqual(extract_check_targets(payload), [])


class TestSsrfGuard(unittest.TestCase):
    BLOCKED = (
        "http://127.0.0.1:8080/", "http://10.1.2.3/",
        "http://172.16.0.9/", "http://192.168.1.4/",
        "http://169.254.169.254/latest", "http://100.64.0.1/",
        "http://0.0.0.0/", "http://224.0.0.5/", "http://240.0.0.1/",
        "http://[::1]/", "http://[fe80::1]/",
    )

    def test_private_address_literals_refused_without_resolution(self):
        for url in self.BLOCKED:
            with self.subTest(url=url):
                with self.assertRaises(SsrfBlocked):
                    assert_public_url(url, resolve=self._fail)

    def test_public_literal_passes_without_dns(self):
        # resolve would raise if consulted — literals must short-circuit
        assert_public_url("http://93.184.216.34/music.html",
                          resolve=self._fail)

    @staticmethod
    def _fail(host, port):
        raise AssertionError(f"resolver must not be called for {host!r}")

    def test_hostname_resolving_to_any_private_address_blocked(self):
        resolve = lambda host, port: [(None, None, None, "",
                                       ("192.168.0.10", 0))]  # noqa: E731
        with self.assertRaises(SsrfBlocked) as ctx:
            assert_public_url("https://innocent.example/", resolve=resolve)
        self.assertIn("192.168.0.10", str(ctx.exception))

    def test_failed_resolution_blocks_by_default(self):
        def broken(host, port):
            raise OSError("no dns in sandbox")

        with self.assertRaises(SsrfBlocked) as ctx:
            assert_public_url("https://unresolvable.example/",
                              resolve=broken)
        self.assertIn("DNS resolution failed", str(ctx.exception))


# ---------------------------------------------------------------------------
# SQLite repository contract for tracks + link checks
# ---------------------------------------------------------------------------

class TestRepositoryTracksAndChecks(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = os.path.join(tmp.name, "db.sqlite")
        self.svc = PersistenceService(self.db_path)
        self.addCleanup(self.svc.close)
        self.assertIsInstance(self.svc, IntelligenceRepository)

    @staticmethod
    def _track(n, *, status="ready", created_at=None):
        return {
            "track_id": track_id_for(f"{n:064x}"),
            "sha256": f"{n:064x}",
            "original_filename": f"track{n}.mp3",
            "size_bytes": 100 + n,
            "content_type": "audio/mpeg",
            "status": status,
            "created_at": created_at,
        }

    def test_save_track_inserts_ready_row_with_matching_timestamps(self):
        row = self.svc.save_track(self._track(1))
        self.assertEqual(row["status"], "ready")
        self.assertIsNone(row["reject_reason"])
        self.assertEqual(row["created_at"], row["updated_at"])
        self.assertEqual(row["size_bytes"], 101)
        self.assertTrue(row["created_at"])

    def test_update_preserves_created_at_and_bumps_updated_at(self):
        first = self.svc.save_track(self._track(2))
        time.sleep(0.01)
        second = self.svc.save_track(
            dict(self._track(2), status="archived", notes="retired"))
        self.assertEqual(second["created_at"], first["created_at"])
        self.assertGreaterEqual(second["updated_at"], first["updated_at"])
        self.assertEqual(second["status"], "archived")
        self.assertEqual(second["notes"], "retired")

    def test_get_unknown_track_returns_none(self):
        self.assertIsNone(self.svc.get_track(track_id_for("b" * 64)))

    def test_list_tracks_filters_paginates_and_orders_newest_first(self):
        stamps = ("2026-08-01T00:00:03+00:00",
                  "2026-08-01T00:00:01+00:00",
                  "2026-08-01T00:00:02+00:00")
        for n, stamp in enumerate(stamps, start=1):
            self.svc.save_track(self._track(n, created_at=stamp))
        self.svc.save_track(self._track(9, status="quarantined"))

        rows, total = self.svc.list_tracks()
        self.assertEqual(total, 4)
        # created_at DESC: the real 'now' stamp beats the seeded August
        # timestamps, then 03 -> 02 -> 01.
        self.assertEqual([r["sha256"][-1] for r in rows],
                         ["9", "1", "3", "2"])
        rows, total = self.svc.list_tracks(status="ready")
        self.assertEqual(total, 3)
        rows, total = self.svc.list_tracks(limit=2, offset=1,
                                           status="ready")
        self.assertEqual(total, 3)
        self.assertEqual([r["sha256"][-1] for r in rows], ["3", "2"])
        rows, total = self.svc.list_tracks(offset=99)
        self.assertEqual((rows, total), ([], 4))

    def test_link_checks_append_newest_first_with_limit(self):
        record = kzow()
        self.svc.ingest_intelligence([record], source="tests")
        key = _identity_key(record)
        for n in range(3):
            self.svc.record_link_check(key, {
                "url": f"https://a.example/check{n}",
                "target_kind": "submission_url",
                "ok": bool(n % 2),
                "status": 200 if n % 2 else None,
                "error_kind": None if n % 2 else "dns_error",
                "latency_ms": n * 10,
                "checked_at": f"2026-08-0{n + 1}T00:00:00+00:00",
            })
        rows = self.svc.get_link_checks(key)
        self.assertEqual([r["url"][-1] for r in rows], ["2", "1", "0"])
        self.assertIs(rows[0]["ok"], False)         # n=2 -> dns_error
        self.assertEqual(rows[0]["error_kind"], "dns_error")
        self.assertIs(rows[1]["ok"], True)          # int 1 -> bool True
        self.assertEqual(rows[1]["status"], 200)
        self.assertEqual(len(self.svc.get_link_checks(key, limit=2)), 2)

    def test_deleting_station_cascades_its_check_history(self):
        record = kzow()
        self.svc.ingest_intelligence([record], source="tests")
        key = _identity_key(record)
        self.svc.record_link_check(key, {
            "url": "https://www.kzow.example/submit",
            "target_kind": "submission_url", "ok": True, "status": 200,
            "error_kind": None, "latency_ms": 12,
            "checked_at": "2026-08-02T00:00:00+00:00",
        })
        raw = sqlite3.connect(self.db_path)
        try:
            raw.execute("PRAGMA foreign_keys=ON")
            # SQLite schema names the org table `stations`
            # (the PG migration calls it `organizations`).
            raw.execute("DELETE FROM stations WHERE identity_key=?",
                        (key,))
            raw.commit()
        finally:
            raw.close()
        self.assertIsNone(self.svc.get_station(key))
        self.assertEqual(self.svc.get_link_checks(key), [])


# ---------------------------------------------------------------------------
# dispatcher contract (backend.routes) with injected doubles
# ---------------------------------------------------------------------------

class TestDispatcherContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.tmp_root = tmp.name
        # LIFO: DB closes before the temp dir disappears.
        cls.addClassCleanup(tmp.cleanup)
        cls.svc = PersistenceService(os.path.join(tmp.name, "db.sqlite"))
        cls.addClassCleanup(cls.svc.close)
        cls.store_root = os.path.join(tmp.name, "assets")
        cls.store = LocalTrackStore(cls.store_root)
        record = kzow()
        cls.svc.ingest_intelligence([record], source="tests")
        cls.kzow_key = _identity_key(record)

    def dispatch(self, method, path, params=None, body=None,
                 fetcher=None, allow_private=True):
        return dispatch(
            self.svc, method, path, params or {}, body,
            track_store=self.store,
            link_fetcher=fetcher or FakeFetcher(),
            allow_private=allow_private)

    def upload(self, payload, filename=None):
        params = {"filename": [filename]} if filename else {}
        status, body = self.dispatch("POST", "/api/v1/tracks", params,
                                     payload)
        return status, body

    # -- uploads -------------------------------------------------------------

    def test_upload_returns_opaque_ready_projection(self):
        payload = mp3_bytes(pad=2048)
        status, body = self.upload(payload, filename="Phase 8 Demo.mp3")
        self.assertEqual(status, 201)
        self.assertTrue(body["ok"])
        data = body["data"]
        self.assertEqual(data["track_id"],
                         track_id_for(sha256_of(payload)))
        self.assertEqual(data["sha256"], sha256_of(payload))
        self.assertEqual(data["status"], "ready")
        self.assertEqual(data["original_filename"], "Phase 8 Demo.mp3")
        self.assertEqual(data["size_bytes"], len(payload))
        self.assertEqual(data["links"]["self"],
                         f"/api/v1/tracks/{data['track_id']}")
        self.assertTrue(self.store.contains(data["track_id"]))
        self.assertNoLeak(body)

    def assertNoLeak(self, body):
        text = repr(body)
        for fragment in (self.tmp_root, self.store_root, "storage_path",
                         ".part"):
            self.assertNotIn(fragment, text)

    def test_duplicate_upload_is_idempotent(self):
        payload = mp3_bytes(pad=4096)
        first = self.upload(payload)[1]["data"]
        second = self.upload(payload, filename="again.mp3")[
            1]["data"]
        self.assertEqual(first["track_id"], second["track_id"])
        rows, total = self.svc.list_tracks()
        digests = [r["track_id"] for r in rows]
        self.assertEqual(digests.count(first["track_id"]), 1)
        self.assertNoLeak(second)

    def test_wrong_content_is_quarantined_with_audit_row(self):
        status, body = self.upload(b"<html>not audio</html>",
                                   filename="page.html")
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "track_rejected")
        rows, total = self.svc.list_tracks(status="quarantined")
        self.assertGreaterEqual(total, 1)
        quarantined = rows[0]
        self.assertIn("MP3", quarantined["reject_reason"])
        # refused payloads never reach the blob store
        self.assertFalse(
            self.store.contains(quarantined["track_id"]))
        ready, _total = self.svc.list_tracks(status="ready")
        self.assertNotIn(quarantined["track_id"],
                         [r["track_id"] for r in ready])

    def test_oversize_payload_refused_without_row(self):
        _rows, before = self.svc.list_tracks()
        send_data = bytes(DEFAULT_MAX_BYTES + 1)
        status, body = self.dispatch("POST", "/api/v1/tracks", {},
                                     send_data)
        self.assertEqual(status, 413)
        self.assertEqual(body["error"]["code"], "payload_too_large")
        _rows, after = self.svc.list_tracks()
        self.assertEqual(after, before)
        self.assertFalse(self.store.contains(track_id_for(
            sha256_of(send_data))))

    def test_empty_body_rejected_as_track_rejected(self):
        status, body = self.dispatch("POST", "/api/v1/tracks", {}, None)
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "track_rejected")

    def test_listing_envelope_pagination_and_bad_filter(self):
        status, body = self.dispatch("GET", "/api/v1/tracks",
                                     {"limit": ["1"], "offset": ["0"]})
        self.assertEqual(status, 200)
        data = body["data"]
        self.assertEqual(data["limit"], 1)
        self.assertEqual(len(data["tracks"]), 1)
        self.assertLessEqual(len(data["tracks"]), data["total"])
        for track in data["tracks"]:
            self.assertEqual(set(track) - {"links"}, set(TRACK_FIELDS))
            self.assertNoLeak({k: v for k, v in track.items()})
        status, body = self.dispatch("GET", "/api/v1/tracks",
                                     {"status": ["bogus"]})
        self.assertEqual((status, body["error"]["code"]),
                         (400, "bad_request"))

    def test_detail_unknown_and_malformed_ids(self):
        status, body = self.dispatch(
            "GET", f"/api/v1/tracks/{track_id_for('c' * 64)}")
        self.assertEqual((status, body["error"]["code"]),
                         (404, "track_not_found"))
        status, body = self.dispatch("GET", "/api/v1/tracks/nope")
        self.assertEqual((status, body["error"]["code"]),
                         (404, "route_not_found"))

    # -- submission path + accessibility -------------------------------------

    def test_submission_view_for_known_and_unknown_stations(self):
        status, body = self.dispatch(
            "GET", f"/api/v1/stations/{self.kzow_key}/submission")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["identity_key"], self.kzow_key)
        submission = body["data"]["submission"]
        self.assertTrue(submission["submission_url"]["value"])
        self.assertEqual(body["data"]["last_checks"], [])
        status, body = self.dispatch(
            "GET", "/api/v1/stations/domain:missing.example/submission")
        self.assertEqual((status, body["error"]["code"]),
                         (404, "station_not_found"))

    def test_checks_run_records_history_and_summary(self):
        record = _submission_with(
            "https://media.example/submit", "https://media.example/guide",
            name="Checks Happy Radio", website="https://checks-a.example/")
        self.svc.ingest_intelligence([record], source="tests")
        key = _identity_key(record)
        fetcher = FakeFetcher(results={
            "https://media.example/submit": FetchResult(
                url="https://media.example/submit", status=200,
                content_type="text/html", body="<html>ok</html>"),
            "https://media.example/guide": FetchResult(
                url="https://media.example/guide",
                error_kind="http_status", status=404),
        })
        status, body = self.dispatch(
            "POST", f"/api/v1/stations/{key}/submission/checks",
            fetcher=fetcher)
        self.assertEqual(status, 200)
        data = body["data"]
        self.assertEqual(data["targets"], 2)
        self.assertEqual(data["reachable"], 1)
        by_kind = {c["target_kind"]: c for c in data["checks"]}
        self.assertTrue(by_kind["submission_url"]["ok"])
        self.assertEqual(by_kind["submission_url"]["status"], 200)
        self.assertFalse(by_kind["instructions_page"]["ok"])
        self.assertEqual(by_kind["instructions_page"]["status"], 404)
        self.assertEqual(by_kind["instructions_page"]["error_kind"],
                         "http_status")
        for entry in data["checks"]:
            self.assertIsInstance(entry["latency_ms"], int)
            self.assertTrue(entry["checked_at"])

        status, history = self.dispatch(
            "GET", f"/api/v1/stations/{key}/submission/checks",
            {"limit": ["5"]})
        self.assertEqual(status, 200)
        checks = history["data"]["checks"]
        self.assertEqual([c["url"] for c in checks],
                         [c["url"] for c in reversed(data["checks"])])
        last = {c["url"]: c
                for c in history["data"]["last_by_target"]}
        self.assertEqual(set(last),
                         {"https://media.example/submit",
                          "https://media.example/guide"})
        self.assertTrue(last["https://media.example/submit"]["ok"])
        self.assertEqual(last["https://media.example/submit"]["status"],
                         200)
        self.assertFalse(last["https://media.example/guide"]["ok"])

    def test_ssrf_targets_refused_without_any_network_contact(self):
        record = _submission_with(
            "http://127.0.0.1:9/private", "http://10.9.9.9/also-private",
            name="Checks Ssrf Radio", website="https://checks-b.example/")
        self.svc.ingest_intelligence([record], source="tests")
        key = _identity_key(record)
        spy = FakeFetcher()
        status, body = self.dispatch(
            "POST", f"/api/v1/stations/{key}/submission/checks",
            fetcher=spy, allow_private=False)
        self.assertEqual(status, 200)
        for entry in body["data"]["checks"]:
            self.assertFalse(entry["ok"])
            self.assertEqual(entry["error_kind"], "ssrf_blocked")
            self.assertIsNone(entry["latency_ms"])
        self.assertEqual(spy.calls, [])      # guard fired BEFORE fetching
        status, history = self.dispatch(
            "GET", f"/api/v1/stations/{key}/submission/checks")
        kinds = {c["error_kind"] for c in history["data"]["checks"]}
        self.assertEqual(kinds, {"ssrf_blocked"})

    def test_checks_on_unknown_station_404s(self):
        status, body = self.dispatch(
            "POST",
            "/api/v1/stations/domain:missing.example/submission/checks")
        self.assertEqual((status, body["error"]["code"]),
                         (404, "station_not_found"))

# ---------------------------------------------------------------------------
# route table declarations (behavioral, not regex-text pinning)
# ---------------------------------------------------------------------------

class TestPhase8RouteDeclarations(unittest.TestCase):
    def test_route_table_accepts_phase8_methods_and_paths(self):
        samples = (
            ("POST", "/api/v1/tracks"),
            ("GET", "/api/v1/tracks"),
            ("GET", f"/api/v1/tracks/{track_id_for('d' * 64)}"),
            ("GET", "/api/v1/stations/domain:x.example/submission"),
            ("POST",
             "/api/v1/stations/domain:x.example/submission/checks"),
            ("GET",
             "/api/v1/stations/domain:x.example/submission/checks"),
        )
        for method, path in samples:
            with self.subTest(method=method, path=path):
                self.assertTrue(
                    any(row_method == method
                        and row_pattern.match(path)
                        for row_method, row_pattern, _t, _q in ROUTE_TABLE),
                    f"no {method} route matches {path}")

    def test_unsupported_verb_has_no_matching_row(self):
        hits = [row for row in ROUTE_TABLE
                if row[0] == "DELETE" and row[1].match("/api/v1/tracks")]
        self.assertEqual(hits, [])


# ---------------------------------------------------------------------------
# live stdlib webapp over real HTTP (upload + accessibility end-to-end)
# ---------------------------------------------------------------------------

class TestLiveWebappSubmissions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.tmp_root = tmp.name

        stub = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                               _StubHandler)
        stub_thread = threading.Thread(target=stub.serve_forever,
                                       daemon=True)
        stub_thread.start()
        cls.stub_base = f"http://127.0.0.1:{stub.server_port}"

        cls.db_path = os.path.join(tmp.name, "db.sqlite")
        cls.store_root = os.path.join(tmp.name, "assets")
        fetcher = StdlibHttpFetcher(timeout_seconds=5.0,
                                    rate_limit_seconds=0.0)
        cls.server = create_server(
            cls.db_path, "127.0.0.1", 0,
            static_root=DEFAULT_STATIC_ROOT,
            track_store=LocalTrackStore(cls.store_root),
            link_fetcher=fetcher, allow_private=True)
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"
        server_thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True)
        server_thread.start()

        # LIFO run order: webapp shutdown -> webapp socket close ->
        # DB close -> stub shutdown -> stub socket close -> temp removal.
        cls.addClassCleanup(tmp.cleanup)
        cls.addClassCleanup(stub.server_close)
        cls.addClassCleanup(stub.shutdown)
        cls.addClassCleanup(cls.server.service.close)
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)

        svc = cls.server.service
        station_a = _submission_with(f"{cls.stub_base}/ok",
                                     f"{cls.stub_base}/missing")
        station_a["name"] = "Stub A Radio"
        station_a["website"] = "https://stub-a.example/"
        station_b = _submission_with(f"{cls.stub_base}/binary",
                                     f"{cls.stub_base}/blocked")
        station_b["name"] = "Stub B Radio"
        station_b["website"] = "https://stub-b.example/"
        svc.ingest_intelligence([station_a, station_b], source="tests")
        cls.key_a = _identity_key(station_a)
        cls.key_b = _identity_key(station_b)

    # -- helpers -------------------------------------------------------------

    @classmethod
    def request(cls, method, path, data=None, headers=None):
        req = urllib.request.Request(cls.base_url + path, data=data,
                                     method=method,
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    @classmethod
    def get_json(cls, path):
        status, text = cls.request("GET", path)
        return status, json.loads(text)

    def assertNoLeak(self, text):
        for fragment in (self.tmp_root, self.store_root, "storage_path",
                         ".part"):
            self.assertNotIn(fragment, text)

    # -- contract --------------------------------------------------------------

    def test_health_reports_current_schema_version(self):
        status, body = self.get_json("/api/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["schema_version"], SCHEMA_VERSION)
        self.assertGreaterEqual(SCHEMA_VERSION, 4)
        self.assertNoLeak(text=json.dumps(body))

    def test_upload_roundtrip_over_real_http(self):
        payload = mp3_bytes(pad=4096)
        # Content-Type is advisory only: bytes decide acceptance.
        filename = urllib.request.quote("live demo.mp3")
        status, text = self.request(
            "POST", f"/api/v1/tracks?filename={filename}",
            data=payload, headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 201)
        body = json.loads(text)
        data = body["data"]
        self.assertTrue(body["ok"])
        self.assertEqual(data["track_id"],
                         track_id_for(sha256_of(payload)))
        self.assertEqual(data["status"], "ready")
        self.assertEqual(data["original_filename"], "live demo.mp3")
        self.assertTrue(LocalTrackStore(self.store_root).contains(
            data["track_id"]))
        self.assertNoLeak(text)

        dup_status, dup_text = self.request("POST", "/api/v1/tracks",
                                            data=payload)
        self.assertEqual(dup_status, 201)
        self.assertEqual(json.loads(dup_text)["data"]["track_id"],
                         data["track_id"])

    def test_wrong_content_quarantined_over_http(self):
        status, text = self.request("POST", "/api/v1/tracks",
                                    data=b"<html>still no audio</html>")
        self.assertEqual(status, 422)
        body = json.loads(text)
        self.assertEqual(body["error"]["code"], "track_rejected")
        list_status, list_body = self.get_json(
            "/api/v1/tracks?status=quarantined")
        self.assertEqual(list_status, 200)
        self.assertGreaterEqual(list_body["data"]["total"], 1)

    def test_static_handler_never_serves_stored_blobs(self):
        digest = sha256_of(mp3_bytes(pad=64))
        guesses = (
            f"/data/submissions/tracks/blobs/{digest[:2]}/{digest}.mp3",
            f"/..%2f..%2fdata%2fsubmissions%2ftracks%2fblobs%2f"
            f"{digest[:2]}%2f{digest}.mp3",
            f"/submissions/tracks/blobs/{digest[:2]}/{digest}.mp3",
        )
        for guess in guesses:
            with self.subTest(guess=guess):
                status, text = self.request("GET", guess)
                self.assertNotEqual(status, 200)
                self.assertNotIn("ID3", text)

    def test_checks_end_to_end_against_stub_site(self):
        status, text = self.request(
            "POST", f"/api/v1/stations/{self.key_a}/submission/checks")
        self.assertEqual(status, 200)
        body = json.loads(text)
        data = body["data"]
        self.assertEqual(data["targets"], 2)
        self.assertEqual(data["reachable"], 1)
        by_kind = {c["target_kind"]: c for c in data["checks"]}
        ok_entry = by_kind["submission_url"]
        missing_entry = by_kind["instructions_page"]
        self.assertTrue(ok_entry["ok"])
        self.assertEqual(ok_entry["status"], 200)
        self.assertIsNone(ok_entry["error_kind"])
        self.assertFalse(missing_entry["ok"])
        self.assertEqual(missing_entry["status"], 404)
        self.assertEqual(missing_entry["error_kind"], "http_status")
        for entry in (ok_entry, missing_entry):
            self.assertIsInstance(entry["latency_ms"], int)
            self.assertGreaterEqual(entry["latency_ms"], 0)

        history_status, history_body = self.get_json(
            f"/api/v1/stations/{self.key_a}/submission/checks?limit=10")
        self.assertEqual(history_status, 200)
        checks = history_body["data"]["checks"]
        self.assertEqual(len(checks), 2)
        self.assertEqual(checks[0]["url"],
                         f"{self.stub_base}/missing")   # newest first
        last_urls = {c["url"] for c in
                     history_body["data"]["last_by_target"]}
        self.assertEqual(last_urls,
                         {f"{self.stub_base}/ok",
                          f"{self.stub_base}/missing"})

    def test_content_type_and_robots_politeness_enforced(self):
        status, text = self.request(
            "POST", f"/api/v1/stations/{self.key_b}/submission/checks")
        self.assertEqual(status, 200)
        data = json.loads(text)["data"]
        self.assertEqual(data["reachable"], 0)
        kinds = {c["target_kind"]: c["error_kind"]
                 for c in data["checks"]}
        self.assertEqual(kinds.get("submission_url"), "content_type")
        # robots.txt from the stub disallows /blocked -> refused politely
        self.assertEqual(kinds.get("instructions_page"),
                         "robots_disallowed")

    def test_submission_and_history_envelopes_leak_nothing(self):
        collected = []
        _status, text = self.request("GET", "/api/v1/tracks?limit=50")
        collected.append(text)
        for path in (f"/api/v1/stations/{self.key_a}/submission",
                     f"/api/v1/stations/{self.key_a}/submission/checks",
                     "/api/v1/health"):
            _status, text = self.request("GET", path)
            collected.append(text)
        for text in collected:
            self.assertNoLeak(text)


class TestSubmissionServiceFunctions(unittest.TestCase):
    """Service-level seam checks that do not need an HTTP layer."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.svc = PersistenceService(
            os.path.join(tmp.name, "db.sqlite"))
        self.addCleanup(self.svc.close)
        self.store = LocalTrackStore(os.path.join(tmp.name, "assets"))

    def test_station_submission_raises_lookup_error_when_unknown(self):
        with self.assertRaises(LookupError):
            sub_service.station_submission(self.svc, "domain:nope.example")

    def test_last_checks_by_target_keeps_newest_per_pair(self):
        rows = [
            {"url": "https://a/x", "target_kind": "submission_url",
             "ok": True, "checked_at": "2026-08-02T00:00:00+00:00"},
            {"url": "https://a/x", "target_kind": "submission_url",
             "ok": False, "checked_at": "2026-08-01T00:00:00+00:00"},
            {"url": "https://a/guide", "target_kind":
             "instructions_page", "ok": False,
             "checked_at": "2026-08-01T12:00:00+00:00"},
        ]
        latest = sub_service.last_checks_by_target(rows)
        self.assertEqual(len(latest), 2)
        self.assertTrue(latest[0]["ok"])
        self.assertEqual(latest[0]["checked_at"],
                         "2026-08-02T00:00:00+00:00")

    def test_run_link_checks_refuses_private_targets_by_default(self):
        record = _submission_with("http://127.0.0.1:9/private")
        self.svc.ingest_intelligence([record], source="tests")
        key = _identity_key(record)
        fetcher = FakeFetcher()
        summary = sub_service.run_link_checks(
            self.svc, fetcher, key, allow_private=False)
        self.assertEqual(summary["targets"], 1)
        self.assertEqual(summary["reachable"], 0)
        self.assertEqual(fetcher.calls, [])
        self.assertEqual(summary["checks"][0]["error_kind"],
                         "ssrf_blocked")

    def test_upload_track_roundtrip_through_service_seam(self):
        payload = mp3_bytes(pad=256)
        row = sub_service.upload_track(self.svc, self.store, payload,
                                       filename="seam.mp3")
        self.assertEqual(row["track_id"],
                         track_id_for(sha256_of(payload)))
        self.assertEqual(row["original_filename"], "seam.mp3")


class TestPostgresMigration0002(unittest.TestCase):
    def test_tracks_migration_is_last_ordered_and_structural(self):
        migrations = load_pg_migrations()
        versions = [version for version, _name, _sql in migrations]
        self.assertEqual(versions, sorted(versions))
        name, sql = migrations[-1][1], migrations[-1][2]
        self.assertEqual(name, "0002_submissions.sql")
        self.assertIn("CREATE TABLE IF NOT EXISTS tracks", sql)
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS submission_link_checks", sql)
        self.assertIn("REFERENCES organizations", sql)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

