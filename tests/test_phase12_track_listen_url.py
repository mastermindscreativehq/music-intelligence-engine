"""Phase 12: persistent per-release official listening URL.

The outreach handoff must use a release's SAVED listening URL (My music),
never a manually-typed or invented one. This suite pins the whole seam:

- the SQLite v12 migration adds a nullable ``tracks.listen_url`` column and
  upgrades an existing v11 database in place;
- the repository round-trips it and the update helper sets/clears/validates;
- the dispatcher's ``PATCH /api/v1/tracks/{id}`` contract (200/400/404) and
  the projection exposing ``listen_url`` (``TRACK_FIELDS``);
- the Postgres migration + storage mirror the column;
- the frontend auto-fills from the saved URL, states honestly when none is
  saved, previews the exact line the email will carry, and never embeds a
  remote URL literal.
"""

from __future__ import annotations

import inspect
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend.contracts import TRACK_FIELDS, track_projection
from backend.routes import ROUTE_TABLE, dispatch
from database.schema import SCHEMA_VERSION, apply_migrations
from database.schema_migrations import load_pg_migrations
from database.service import PersistenceService
from submissions import service as sub_service
from submissions.storage import track_id_for

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"

TRACK_ID = track_id_for("a" * 64)          # 'sha256:<hex>' shape
OTHER_ID = track_id_for("b" * 64)


def _columns(conn) -> dict:
    return {row[1]: {"notnull": row[3], "default": row[4]}
            for row in conn.execute("PRAGMA table_info(tracks)").fetchall()}


def _track(**overrides) -> dict:
    row = {
        "track_id": TRACK_ID,
        "sha256": "a" * 64,
        "original_filename": "Neon Sky.mp3",
        "size_bytes": 2048,
        "content_type": "audio/mpeg",
        "status": "ready",
        "created_at": "2020-01-01T00:00:00+00:00",
        "updated_at": "2020-01-01T00:00:00+00:00",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# Schema: SQLite migration v12
# ---------------------------------------------------------------------------

class TestSchemaMigration(unittest.TestCase):
    def test_fresh_database_is_current_and_has_the_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = PersistenceService(os.path.join(tmp, "db.sqlite"))
            try:
                self.assertEqual(service.version, SCHEMA_VERSION)
                self.assertGreaterEqual(SCHEMA_VERSION, 12)
                columns = _columns(service._conn)
                self.assertIn("listen_url", columns)
                # Nullable and defaulted to NULL — an unset link is honest.
                self.assertEqual(columns["listen_url"]["notnull"], 0)
                self.assertIsNone(columns["listen_url"]["default"])
            finally:
                service.close()

    def test_existing_v11_database_is_upgraded_in_place(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE tracks ("
            "track_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, "
            "original_filename TEXT, size_bytes INTEGER NOT NULL, "
            "content_type TEXT NOT NULL DEFAULT 'audio/mpeg', "
            "status TEXT NOT NULL, reject_reason TEXT, notes TEXT, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        conn.execute("PRAGMA user_version=11")
        self.assertNotIn("listen_url", _columns(conn))
        version = apply_migrations(conn)
        self.assertEqual(version, SCHEMA_VERSION)
        self.assertIn("listen_url", _columns(conn))
        conn.close()


# ---------------------------------------------------------------------------
# Repository + service: round-trip, set/clear, validation
# ---------------------------------------------------------------------------

class TestRepositoryListenUrl(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.service = PersistenceService(
            os.path.join(self._tmp.name, "db.sqlite"))

    def tearDown(self):
        self.service.close()
        self._tmp.cleanup()

    def test_save_round_trips_listen_url_and_absent_stays_null(self):
        saved = self.service.save_track(
            _track(listen_url="https://example.com/album"))
        self.assertEqual(saved["listen_url"], "https://example.com/album")
        self.assertEqual(
            self.service.get_track(TRACK_ID)["listen_url"],
            "https://example.com/album")
        bare = self.service.save_track(
            _track(track_id=OTHER_ID, sha256="b" * 64))
        self.assertIsNone(bare["listen_url"])

    def test_update_sets_edits_and_clears(self):
        self.service.save_track(_track())
        first = sub_service.update_track_listen_url(
            self.service, TRACK_ID, "https://band.bandcamp.com/album/x")
        self.assertEqual(first["listen_url"],
                         "https://band.bandcamp.com/album/x")
        edited = sub_service.update_track_listen_url(
            self.service, TRACK_ID, "  https://youtu.be/abc  ")
        self.assertEqual(edited["listen_url"], "https://youtu.be/abc")
        cleared = sub_service.update_track_listen_url(
            self.service, TRACK_ID, "")
        self.assertIsNone(cleared["listen_url"])
        self.assertIsNone(
            sub_service.update_track_listen_url(
                self.service, TRACK_ID, None)["listen_url"])

    def test_update_preserves_created_at_and_advances_updated_at(self):
        self.service.save_track(_track())
        updated = sub_service.update_track_listen_url(
            self.service, TRACK_ID, "https://example.com/x")
        self.assertEqual(updated["created_at"], "2020-01-01T00:00:00+00:00")
        self.assertNotEqual(updated["updated_at"],
                            "2020-01-01T00:00:00+00:00")

    def test_invalid_urls_are_refused(self):
        self.service.save_track(_track())
        for bad in ("example.com", "ftp://host/x", "//host/x", "http://",
                    "mailto:a@b.com", "javascript:alert(1)"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    sub_service.update_track_listen_url(
                        self.service, TRACK_ID, bad)
        # A refused value never mutates the stored row.
        self.assertIsNone(self.service.get_track(TRACK_ID)["listen_url"])

    def test_unknown_track_returns_none(self):
        self.assertIsNone(sub_service.update_track_listen_url(
            self.service, OTHER_ID, "https://example.com/x"))

    def test_normalize_schemes(self):
        self.assertEqual(sub_service.normalize_listen_url("https://a.b/c"),
                         "https://a.b/c")
        self.assertEqual(sub_service.normalize_listen_url("http://a.b/c"),
                         "http://a.b/c")
        self.assertIsNone(sub_service.normalize_listen_url("   "))
        with self.assertRaises(ValueError):
            sub_service.normalize_listen_url("a.b")


# ---------------------------------------------------------------------------
# Dispatcher contract: PATCH /api/v1/tracks/{track_id}
# ---------------------------------------------------------------------------

class TestDispatcherListenUrl(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.service = PersistenceService(
            os.path.join(self._tmp.name, "db.sqlite"))
        self.service.save_track(_track())

    def tearDown(self):
        self.service.close()
        self._tmp.cleanup()

    def _patch(self, track_id, payload):
        import json
        return dispatch(self.service, "PATCH",
                        f"/api/v1/tracks/{track_id}", {},
                        json.dumps(payload).encode())

    def test_route_table_declares_patch(self):
        hits = [row for row in ROUTE_TABLE if row[0] == "PATCH"
                and row[1].match(f"/api/v1/tracks/{TRACK_ID}")]
        self.assertEqual(len(hits), 1)

    def test_patch_sets_and_projection_exposes_it(self):
        status, body = self._patch(TRACK_ID,
                                   {"listen_url": "https://youtu.be/abc"})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["listen_url"], "https://youtu.be/abc")
        self.assertEqual(set(body["data"]) - {"links"}, set(TRACK_FIELDS))
        status, body = dispatch(
            self.service, "GET", f"/api/v1/tracks/{TRACK_ID}", {})
        self.assertEqual(body["data"]["listen_url"], "https://youtu.be/abc")

    def test_patch_empty_clears_the_link(self):
        self._patch(TRACK_ID, {"listen_url": "https://youtu.be/abc"})
        status, body = self._patch(TRACK_ID, {"listen_url": ""})
        self.assertEqual(status, 200)
        self.assertIsNone(body["data"]["listen_url"])

    def test_patch_invalid_is_bad_request(self):
        status, body = self._patch(TRACK_ID, {"listen_url": "not a url"})
        self.assertEqual((status, body["error"]["code"]),
                         (400, "bad_request"))

    def test_patch_unknown_is_track_not_found(self):
        status, body = self._patch(OTHER_ID,
                                   {"listen_url": "https://x.example"})
        self.assertEqual((status, body["error"]["code"]),
                         (404, "track_not_found"))

    def test_listing_projection_includes_listen_url(self):
        self._patch(TRACK_ID, {"listen_url": "https://youtu.be/abc"})
        status, body = dispatch(self.service, "GET", "/api/v1/tracks", {})
        self.assertEqual(status, 200)
        track = body["data"]["tracks"][0]
        self.assertEqual(set(track) - {"links"}, set(TRACK_FIELDS))
        self.assertEqual(track["listen_url"], "https://youtu.be/abc")

    def test_projection_shape_matches_contract(self):
        projection = track_projection(_track(listen_url="https://x.example"))
        self.assertIn("listen_url", projection)


# ---------------------------------------------------------------------------
# Postgres parity (offline structural scan)
# ---------------------------------------------------------------------------

class TestPostgresParity(unittest.TestCase):
    def test_new_migration_file_mirrors_the_column(self):
        migrations = load_pg_migrations()
        versions = [version for version, _n, _s in migrations]
        self.assertEqual(versions, sorted(versions))
        by_name = {name: sql for _v, name, sql in migrations}
        self.assertIn("0011_track_listen_url.sql", by_name)
        sql = by_name["0011_track_listen_url.sql"]
        self.assertIn("tracks", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS listen_url", sql)

    def test_pg_store_code_handles_listen_url(self):
        from database import pg_store
        source = inspect.getsource(pg_store.PostgresStorage)
        self.assertIn("listen_url", source)


# ---------------------------------------------------------------------------
# Frontend: uses the SAVED url, honest when absent, never embeds a link
# ---------------------------------------------------------------------------

class TestFrontendUsesSavedUrl(unittest.TestCase):
    def _read(self, relative: str) -> str:
        return (FRONTEND / relative).read_text(encoding="utf-8")

    def test_no_remote_url_literals_in_changed_assets(self):
        for relative in ("js/views/outreachModal.js", "js/views/tracks.js",
                         "js/api.js", "js/views/djProfile.js"):
            with self.subTest(asset=relative):
                content = self._read(relative)
                self.assertIsNone(re.search(r"https?://", content))
                self.assertIsNone(re.search(r'="//', content))

    def test_my_music_exposes_an_editable_listening_url(self):
        source = self._read("js/views/tracks.js")
        self.assertIn("Official listening URL", source)
        self.assertIn("api.updateTrackListenUrl", source)
        self.assertIn("No listening URL saved yet.", source)

    def test_api_client_has_the_patch_method(self):
        source = self._read("js/api.js")
        self.assertIn("updateTrackListenUrl", source)
        self.assertIn("PATCH", source)

    def test_composer_autofills_from_saved_url_with_honest_empty_state(self):
        source = self._read("js/views/outreachModal.js")
        # reads the persisted field, not a typed-in value
        self.assertIn("track.listen_url", source)
        # honest when the selected release has nothing saved
        self.assertIn("No listening URL is saved for this release", source)
        # a live preview of the exact appended line
        self.assertIn("You can listen here:", source)
        self.assertIn("updateLinkPreview", source)
        # and it is preserved onto the outreach record
        self.assertIn("music_link", source)


if __name__ == "__main__":
    unittest.main()
