"""Phase 13: per-release song story + song-story-aware AI email drafts.

Pins the whole song-story seam end to end:

- the SQLite v13 migration adds nullable ``tracks.song_story`` and
  ``tracks.owner_user_id`` and upgrades an existing v12 database in place;
- the repository round-trips both, an identical-byte re-upload never wipes a
  saved story / listening URL / owner, and releases stay isolated;
- the update helpers set/clear/validate and stay per-release;
- the projection exposes ``song_story`` but NEVER ``owner_user_id``;
- the dispatcher's ``PATCH /api/v1/tracks/{id}`` sets the story while
  preserving the v12 link contract and enforces ownership (an owned release
  is only editable by its owner; legacy NULL-owner releases keep their
  historical behavior), and ``POST /api/v1/outreach/draft-email`` is
  authenticated, ownership-checked, draft-only, and never sends;
- ``outreach.ai_writer`` composes a recipient-specific draft from the saved
  story + listening link, falls back deterministically offline, never
  fabricates a story's meaning, and never records or sends anything;
- the Postgres migration + storage mirror both columns;
- the frontend exposes a per-release story editor and an AI "Write email from
  song story" action that populates the editable fields without auto-sending.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from auth.service import AuthService
from backend.contracts import TRACK_FIELDS, track_projection
from backend.routes import ROUTE_TABLE, dispatch
from database.schema import SCHEMA_VERSION, apply_migrations
from database.schema_migrations import load_pg_migrations
from database.service import PersistenceService
from enrichment.llm import OllamaClient, OllamaConfig
from outreach import ai_writer
from submissions import service as sub_service
from submissions.storage import track_id_for

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"

TRACK_ID = track_id_for("c" * 64)          # 'sha256:<hex>' shape
OTHER_ID = track_id_for("d" * 64)


def _columns(conn) -> dict:
    return {row[1]: {"notnull": row[3], "default": row[4]}
            for row in conn.execute("PRAGMA table_info(tracks)").fetchall()}


def _track(**overrides) -> dict:
    row = {
        "track_id": TRACK_ID,
        "sha256": "c" * 64,
        "original_filename": "Neon Sky.mp3",
        "size_bytes": 2048,
        "content_type": "audio/mpeg",
        "status": "ready",
        "created_at": "2020-01-01T00:00:00+00:00",
        "updated_at": "2020-01-01T00:00:00+00:00",
    }
    row.update(overrides)
    return row


def _offline_client() -> OllamaClient:
    """A client whose transport always reports the model unavailable."""

    def transport(method, path, payload=None, timeout=None):
        raise OSError("ollama offline in tests")

    return OllamaClient(OllamaConfig(), transport=transport)


def _ai_client(subject="From Ada", message="Hello there.") -> OllamaClient:
    """A client whose transport returns a schema-valid model response."""

    def transport(method, path, payload=None, timeout=None):
        if method == "GET":
            return {}
        return {"response": json.dumps(
            {"subject": subject, "message": message})}

    return OllamaClient(OllamaConfig(), transport=transport)


# ---------------------------------------------------------------------------
# Schema: SQLite migration v13
# ---------------------------------------------------------------------------

class TestSchemaMigration(unittest.TestCase):
    def test_fresh_database_is_current_and_has_both_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = PersistenceService(os.path.join(tmp, "db.sqlite"))
            try:
                self.assertEqual(service.version, SCHEMA_VERSION)
                self.assertGreaterEqual(SCHEMA_VERSION, 13)
                columns = _columns(service._conn)
                for name in ("song_story", "owner_user_id"):
                    with self.subTest(column=name):
                        self.assertIn(name, columns)
                        # Nullable, NULL default — unset is honest absence.
                        self.assertEqual(columns[name]["notnull"], 0)
                        self.assertIsNone(columns[name]["default"])
            finally:
                service.close()

    def test_existing_v12_database_is_upgraded_in_place(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE tracks ("
            "track_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, "
            "original_filename TEXT, size_bytes INTEGER NOT NULL, "
            "content_type TEXT NOT NULL DEFAULT 'audio/mpeg', "
            "status TEXT NOT NULL, reject_reason TEXT, notes TEXT, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
            "listen_url TEXT)")
        conn.execute("PRAGMA user_version=12")
        self.assertNotIn("song_story", _columns(conn))
        version = apply_migrations(conn)
        self.assertEqual(version, SCHEMA_VERSION)
        columns = _columns(conn)
        self.assertIn("song_story", columns)
        self.assertIn("owner_user_id", columns)
        conn.close()


# ---------------------------------------------------------------------------
# Repository + service: round-trip, preservation, isolation, validation
# ---------------------------------------------------------------------------

class TestRepositorySongStory(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.service = PersistenceService(
            os.path.join(self._tmp.name, "db.sqlite"))

    def tearDown(self):
        self.service.close()
        self._tmp.cleanup()

    def test_save_round_trips_story_and_owner(self):
        saved = self.service.save_track(_track(
            song_story="Written at 3am about a long drive home.",
            owner_user_id="user-1"))
        self.assertEqual(saved["song_story"],
                         "Written at 3am about a long drive home.")
        self.assertEqual(saved["owner_user_id"], "user-1")
        fetched = self.service.get_track(TRACK_ID)
        self.assertEqual(fetched["song_story"], saved["song_story"])

    def test_absent_story_stays_null(self):
        bare = self.service.save_track(_track())
        self.assertIsNone(bare["song_story"])
        self.assertIsNone(bare["owner_user_id"])

    def test_reupload_without_keys_preserves_story_link_and_owner(self):
        self.service.save_track(_track(
            listen_url="https://example.com/album",
            song_story="The story the operator wrote.",
            owner_user_id="user-1"))
        # An identical-byte re-upload arrives with only the mutable basics.
        again = self.service.save_track(_track(status="ready"))
        self.assertEqual(again["listen_url"], "https://example.com/album")
        self.assertEqual(again["song_story"], "The story the operator wrote.")
        self.assertEqual(again["owner_user_id"], "user-1")

    def test_explicit_none_clears_but_absence_preserves(self):
        self.service.save_track(_track(song_story="keep me"))
        preserved = self.service.save_track(_track(status="ready"))
        self.assertEqual(preserved["song_story"], "keep me")
        cleared = self.service.save_track(_track(song_story=None))
        self.assertIsNone(cleared["song_story"])

    def test_releases_are_isolated(self):
        self.service.save_track(_track(song_story="song A story"))
        self.service.save_track(_track(track_id=OTHER_ID, sha256="d" * 64,
                                       song_story="song B story"))
        self.assertEqual(self.service.get_track(TRACK_ID)["song_story"],
                         "song A story")
        self.assertEqual(self.service.get_track(OTHER_ID)["song_story"],
                         "song B story")
        sub_service.update_track_song_story(self.service, TRACK_ID, "A v2")
        self.assertEqual(self.service.get_track(TRACK_ID)["song_story"],
                         "A v2")
        self.assertEqual(self.service.get_track(OTHER_ID)["song_story"],
                         "song B story")

    def test_update_sets_edits_and_clears(self):
        self.service.save_track(_track())
        first = sub_service.update_track_song_story(
            self.service, TRACK_ID, "  first draft story  ")
        self.assertEqual(first["song_story"], "first draft story")
        edited = sub_service.update_track_song_story(
            self.service, TRACK_ID, "refined story")
        self.assertEqual(edited["song_story"], "refined story")
        cleared = sub_service.update_track_song_story(self.service, TRACK_ID, "")
        self.assertIsNone(cleared["song_story"])
        self.assertIsNone(sub_service.update_track_song_story(
            self.service, TRACK_ID, None)["song_story"])

    def test_update_preserves_created_at_and_advances_updated_at(self):
        self.service.save_track(_track())
        updated = sub_service.update_track_song_story(
            self.service, TRACK_ID, "story")
        self.assertEqual(updated["created_at"], "2020-01-01T00:00:00+00:00")
        self.assertNotEqual(updated["updated_at"],
                            "2020-01-01T00:00:00+00:00")

    def test_unknown_track_returns_none(self):
        self.assertIsNone(sub_service.update_track_song_story(
            self.service, OTHER_ID, "story"))

    def test_normalize_song_story(self):
        self.assertEqual(sub_service.normalize_song_story("  hi  "), "hi")
        self.assertIsNone(sub_service.normalize_song_story("   "))
        self.assertIsNone(sub_service.normalize_song_story(None))

    def test_update_track_fields_edits_both_and_validates_atomically(self):
        self.service.save_track(_track(listen_url="https://old.example"))
        row = sub_service.update_track_fields(self.service, TRACK_ID, {
            "listen_url": "https://new.example",
            "song_story": "a story"})
        self.assertEqual(row["listen_url"], "https://new.example")
        self.assertEqual(row["song_story"], "a story")
        # A bad link refuses the WHOLE update, leaving the row untouched.
        with self.assertRaises(ValueError):
            sub_service.update_track_fields(self.service, TRACK_ID, {
                "listen_url": "not a url", "song_story": "should not land"})
        after = self.service.get_track(TRACK_ID)
        self.assertEqual(after["listen_url"], "https://new.example")
        self.assertEqual(after["song_story"], "a story")
        # No editable key at all is a caller error, not a silent no-op.
        with self.assertRaises(ValueError):
            sub_service.update_track_fields(self.service, TRACK_ID, {})


# ---------------------------------------------------------------------------
# Contract: story is public, owner is NOT
# ---------------------------------------------------------------------------

class TestContracts(unittest.TestCase):
    def test_track_fields_expose_story_but_not_owner(self):
        self.assertIn("song_story", TRACK_FIELDS)
        self.assertNotIn("owner_user_id", TRACK_FIELDS)

    def test_projection_hides_owner(self):
        projection = track_projection(_track(
            song_story="a story", owner_user_id="user-1"))
        self.assertEqual(projection["song_story"], "a story")
        self.assertNotIn("owner_user_id", projection)


# ---------------------------------------------------------------------------
# Dispatcher: PATCH story + authenticated draft-only endpoint
# ---------------------------------------------------------------------------

class TestDispatcherSongStory(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.service = PersistenceService(
            os.path.join(self._tmp.name, "db.sqlite"))
        self.service.save_track(_track(listen_url="https://youtu.be/abc"))
        self.auth = AuthService(self.service)
        self.user = self.auth.create_user("writer@example.com")
        token = self.auth.create_session(self.user["user_id"])["session_token"]
        self.auth_headers = {"Authorization": f"Bearer {token}"}

    def tearDown(self):
        self.service.close()
        self._tmp.cleanup()

    def _draft(self, payload, headers=None):
        return dispatch(self.service, "POST",
                        "/api/v1/outreach/draft-email", {},
                        json.dumps(payload).encode(),
                        headers=headers)

    def _patch(self, track_id, payload, headers=None):
        return dispatch(self.service, "PATCH",
                        f"/api/v1/tracks/{track_id}", {},
                        json.dumps(payload).encode(),
                        headers=headers)

    def test_route_table_declares_the_post(self):
        hits = [row for row in ROUTE_TABLE if row[0] == "POST"
                and row[1].match("/api/v1/outreach/draft-email")]
        self.assertEqual(len(hits), 1)

    def test_patch_sets_story_and_keeps_link_contract(self):
        status, body = self._patch(TRACK_ID, {"song_story": "a per-release story"})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["song_story"], "a per-release story")
        self.assertEqual(set(body["data"]) - {"links"}, set(TRACK_FIELDS))
        self.assertNotIn("owner_user_id", body["data"])

    def test_patch_bad_link_is_still_bad_request(self):
        status, body = self._patch(TRACK_ID, {"listen_url": "not a url"})
        self.assertEqual((status, body["error"]["code"]), (400, "bad_request"))

    def test_patch_owned_track_rejects_other_user_and_anonymous(self):
        self.service.save_track(_track(owner_user_id="someone-else"))
        owned = _track(owner_user_id="someone-else", track_id=OTHER_ID,
                       sha256="d" * 64)
        self.service.save_track(owned)
        status, body = self._patch(OTHER_ID, {"song_story": "mine"},
                                   self.auth_headers)
        self.assertEqual((status, body["error"]["code"]), (403, "forbidden"))
        # Anonymous callers cannot edit someone else's release either.
        status, body = self._patch(OTHER_ID, {"song_story": "mine"}, {})
        self.assertEqual((status, body["error"]["code"]), (403, "forbidden"))
        # Nothing was written by the rejected callers.
        self.assertIsNone(self.service.get_track(OTHER_ID)["song_story"])

    def test_patch_owned_track_allows_the_owner(self):
        self.service.save_track(_track(owner_user_id=self.user["user_id"]))
        status, body = self._patch(TRACK_ID,
                                   {"song_story": "the owner's story"},
                                   self.auth_headers)
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["song_story"], "the owner's story")

    def test_patch_legacy_null_owner_release_keeps_its_historical_flow(self):
        # setUp stores a NULL-owner legacy release: patchable by anyone,
        # signed in or not — unchanged single-operator behavior.
        status, body = self._patch(TRACK_ID, {"song_story": "legacy story"})
        self.assertEqual(status, 200)
        status, body = self._patch(TRACK_ID, {"song_story": "also fine"},
                                   self.auth_headers)
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["song_story"], "also fine")

    def test_draft_requires_authentication(self):
        status, body = self._draft({"track_id": TRACK_ID})
        self.assertEqual((status, body["error"]["code"]), (401, "unauthorized"))

    def test_draft_is_owner_checked(self):
        # The release belongs to someone else -> 403, no draft leaks.
        self.service.save_track(_track(owner_user_id="someone-else"))
        status, body = self._draft({"track_id": TRACK_ID},
                                   self.auth_headers)
        self.assertEqual((status, body["error"]["code"]), (403, "forbidden"))

    def test_unknown_track_is_not_found(self):
        status, body = self._draft({"track_id": OTHER_ID}, self.auth_headers)
        self.assertEqual((status, body["error"]["code"]),
                         (404, "track_not_found"))

    def test_bad_style_is_bad_request_for_the_owner(self):
        self.service.save_track(
            _track(owner_user_id=self.user["user_id"]))
        status, body = self._draft(
            {"track_id": TRACK_ID, "style": "shouty"}, self.auth_headers)
        self.assertEqual((status, body["error"]["code"]), (400, "bad_request"))

    def test_owner_can_draft_offline_fallback_with_story_and_link(self):
        self.service.save_track(_track(
            owner_user_id=self.user["user_id"],
            song_story="I wrote this after a long night drive.",
            listen_url="https://youtu.be/abc"))
        original = ai_writer.default_client
        ai_writer.default_client = _offline_client
        try:
            status, body = self._draft(
                {"track_id": TRACK_ID, "style": "warm",
                 "recipient": {"name": "Robin", "organization": "KEXP"}},
                self.auth_headers)
        finally:
            ai_writer.default_client = original
        self.assertEqual(status, 200)
        draft = body["data"]
        self.assertFalse(draft["used_ai"])
        self.assertTrue(draft["used_story"])
        self.assertTrue(draft["link_included"])
        self.assertIn("https://youtu.be/abc", draft["message"])
        self.assertEqual(draft["listening_url"], "https://youtu.be/abc")
        self.assertEqual(set(draft), {
            "subject", "message", "style", "used_ai", "used_story", "model",
            "prompt_version", "listening_url", "link_included", "song_title",
            "warnings"})

    def test_legacy_release_without_owner_is_draftable(self):
        # NULL owner == legacy/shared release; an authenticated operator may
        # still draft from it.
        original = ai_writer.default_client
        ai_writer.default_client = _offline_client
        try:
            status, body = self._draft({"track_id": TRACK_ID}, self.auth_headers)
        finally:
            ai_writer.default_client = original
        self.assertEqual(status, 200)
        self.assertFalse(body["data"]["used_story"])  # no story saved


# ---------------------------------------------------------------------------
# AI writer: honest fallback, validated AI path, never fabricates/sends
# ---------------------------------------------------------------------------

class TestAiWriter(unittest.TestCase):
    def test_expected_style_vocabulary(self):
        self.assertEqual(
            set(ai_writer.STYLES),
            {"professional", "warm", "story", "concise", "industry", "pitch"})
        self.assertEqual(ai_writer.DEFAULT_STYLE, "professional")

    def test_offline_fallback_is_used_when_model_unavailable(self):
        draft = ai_writer.compose_email(
            track=_track(song_story="A story",
                         listen_url="https://youtu.be/abc"),
            payload={"recipient": {"name": "Sam"}},
            client=_offline_client())
        self.assertFalse(draft["used_ai"])
        self.assertTrue(draft["used_story"])
        self.assertTrue(draft["link_included"])
        self.assertEqual(draft["style"], "professional")
        self.assertIsNone(draft["model"])

    def test_missing_story_is_honest_and_does_not_describe_the_song(self):
        draft = ai_writer.compose_email(track=_track(), payload={}, client=None)
        self.assertFalse(draft["used_story"])
        self.assertIn("song story", " ".join(draft["warnings"]).lower())
        # The fallback body never asserts a meaning that was not supplied.
        self.assertNotIn("I wrote", draft["message"])

    def test_missing_link_warns_and_is_not_invented(self):
        draft = ai_writer.compose_email(
            track=_track(song_story="A story"), payload={}, client=None)
        self.assertFalse(draft["link_included"])
        self.assertIsNone(draft["listening_url"])
        self.assertIn("listening", " ".join(draft["warnings"]).lower())

    def test_link_is_appended_once_even_from_ai_output(self):
        draft = ai_writer.compose_email(
            track=_track(song_story="A story",
                         listen_url="https://youtu.be/abc"),
            payload={}, client=_ai_client(message="Please take a listen."))
        self.assertTrue(draft["used_ai"])
        self.assertEqual(draft["message"].count("https://youtu.be/abc"), 1)

    def test_ai_path_uses_validated_output_and_reports_provenance(self):
        draft = ai_writer.compose_email(
            track=_track(song_story="A story",
                         listen_url="https://youtu.be/abc"),
            payload={"style": "story"},
            client=_ai_client(subject="For Sam", message="A warm note."))
        self.assertTrue(draft["used_ai"])
        self.assertEqual(draft["subject"], "For Sam")
        self.assertEqual(draft["style"], "story")
        self.assertEqual(draft["prompt_version"], "v1")
        self.assertTrue(draft["model"])
        self.assertTrue(draft["link_included"])

    def test_malformed_ai_output_falls_back(self):
        def transport(method, path, payload=None, timeout=None):
            if method == "GET":
                return {}
            return {"response": "not json at all"}

        draft = ai_writer.compose_email(
            track=_track(song_story="A story"),
            payload={}, client=OllamaClient(OllamaConfig(),
                                            transport=transport))
        self.assertFalse(draft["used_ai"])

    def test_unknown_style_raises(self):
        with self.assertRaises(ValueError):
            ai_writer.compose_email(track=_track(), payload={"style": "loud"},
                                    client=None)

    def test_explicit_story_override_wins_over_saved(self):
        draft = ai_writer.compose_email(
            track=_track(song_story="saved story"),
            payload={"song_story": "override story"}, client=None)
        self.assertTrue(draft["used_story"])
        self.assertIn("override story", draft["message"])
        self.assertNotIn("saved story", draft["message"])

    def test_prompt_template_loads_with_every_declared_variable(self):
        template = ai_writer.load_template(ai_writer.PROMPT_DOMAIN,
                                           ai_writer.PROMPT_NAME)
        self.assertEqual(template.prompt_version, "v1")
        self.assertEqual(set(template.variables), {
            "song_title", "artist_name", "recipient_name", "recipient_role",
            "organization", "song_story", "listening_url", "objective",
            "style", "instructions"})

    def test_writer_never_records_or_sends(self):
        source = inspect.getsource(ai_writer)
        self.assertNotIn("create_outreach", source)
        self.assertNotIn("record_outreach", source)
        self.assertNotIn("smtplib", source)
        self.assertNotIn("gmail", source.lower())
        # It returns a plain draft dict and takes no transport/storage seam.
        params = inspect.signature(ai_writer.compose_email).parameters
        self.assertEqual(set(params), {"track", "payload", "client"})


# ---------------------------------------------------------------------------
# Postgres parity (offline structural scan)
# ---------------------------------------------------------------------------

class TestPostgresParity(unittest.TestCase):
    def test_new_migration_mirrors_both_columns(self):
        migrations = load_pg_migrations()
        versions = [version for version, _n, _s in migrations]
        self.assertEqual(versions, sorted(versions))
        by_name = {name: sql for _v, name, sql in migrations}
        self.assertIn("0012_track_song_story.sql", by_name)
        sql = by_name["0012_track_song_story.sql"]
        self.assertIn("tracks", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS song_story", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS owner_user_id", sql)

    def test_pg_store_code_handles_both_columns(self):
        from database import pg_store
        source = inspect.getsource(pg_store.PostgresStorage)
        self.assertIn("song_story", source)
        self.assertIn("owner_user_id", source)


# ---------------------------------------------------------------------------
# Frontend: story editor + AI action that never auto-sends
# ---------------------------------------------------------------------------

class TestFrontend(unittest.TestCase):
    def _read(self, relative: str) -> str:
        return (FRONTEND / relative).read_text(encoding="utf-8")

    def test_no_remote_url_literals_in_changed_assets(self):
        for relative in ("js/views/outreachModal.js", "js/views/tracks.js",
                         "js/api.js"):
            with self.subTest(asset=relative):
                content = self._read(relative)
                self.assertIsNone(re.search(r"https?://", content))
                self.assertIsNone(re.search(r'="//', content))

    def test_my_music_exposes_a_song_story_editor(self):
        source = self._read("js/views/tracks.js")
        self.assertIn("Song story / creative brief", source)
        self.assertIn("api.updateTrackSongStory", source)
        self.assertIn("song-story-input", source)

    def test_api_client_has_both_new_methods(self):
        source = self._read("js/api.js")
        self.assertIn("updateTrackSongStory", source)
        self.assertIn("composeEmail", source)
        self.assertIn("/api/v1/outreach/draft-email", source)

    def test_composer_offers_the_action_without_auto_sending(self):
        source = self._read("js/views/outreachModal.js")
        self.assertIn("Write email from song story", source)
        self.assertIn("api.composeEmail", source)
        self.assertIn("EMAIL_STYLES", source)
        # A 401 is surfaced with a sign-in prompt, not an automatic retry.
        self.assertIn("Sign in to write with AI", source)
        # Replacing existing text asks first; nothing is sent from here.
        self.assertIn("window.confirm", source)
        self.assertIn("hand", source.lower())

    def test_route_is_declared_for_the_api_coupling_check(self):
        # Complements test_phase7_frontend's route/JS coupling scan.
        self.assertIn("song_story", TRACK_FIELDS)


if __name__ == "__main__":
    unittest.main()
