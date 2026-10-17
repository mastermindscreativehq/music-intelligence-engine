"""Release creation + editing regression tests (offline, real API).

Pins requirement 2 of the production repair: an operator can create a release
from metadata + a listening link (NO MP3 upload), retrieve its details through
the ``links.self`` path the SPA actually calls, edit its title / artist /
genre / listening link / storyline, and see the changes persist.

Two guarantees are asserted explicitly:

* editing an unrelated field NEVER silently clears a saved storyline (the
  frontend sends only changed keys; the service only rewrites present keys);
* a bad listening link is refused with a typed 400 and leaves the row intact.

These run against the real FastAPI app over an in-process SQLite store — no
network, no mocks of the code under test.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from backend.app import create_app
from database.service import PersistenceService

MUSIC_URL = "https://artist.bandcamp.com/track/example"


class TestReleaseEditing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.storage = PersistenceService(
            os.path.join(cls._tmp.name, "db.sqlite"))
        cls.client = TestClient(create_app(cls.storage),
                                raise_server_exceptions=False)
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.addClassCleanup(cls.storage.close)

    def _create(self, **overrides):
        payload = {
            "title": "Neon Fields",
            "artist_name": "Ari Vance",
            "music_url": MUSIC_URL,
        }
        payload.update(overrides)
        response = self.client.post("/api/v1/tracks/releases", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["data"]

    def _get(self, self_path):
        response = self.client.get(self_path)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    def test_create_release_returns_a_served_self_link(self):
        data = self._create(genre="synthwave", storyline="A drive at dusk.")
        self.assertTrue(data["track_id"].startswith("sha256:"))
        self.assertEqual(data["links"]["self"],
                         f"/api/v1/tracks/{data['track_id']}")
        # The exact path the SPA fetches resolves to a real detail route.
        detail = self._get(data["links"]["self"])
        self.assertEqual(detail["title"], "Neon Fields")
        self.assertEqual(detail["artist_name"], "Ari Vance")
        self.assertEqual(detail["listen_url"], MUSIC_URL)
        self.assertEqual(detail["genre"], "synthwave")
        self.assertEqual(detail["song_story"], "A drive at dusk.")

    def test_metadata_only_create_needs_no_upload(self):
        # No file bytes anywhere; a listening link alone is a valid release.
        data = self._create(title="Link Only", artist_name="Solo")
        self.assertEqual(data["listen_url"], MUSIC_URL)
        self.assertEqual(data["status"], "ready")
        self.assertEqual(data["content_type"],
                         "application/vnd.mie.release+json")

    def test_editing_title_preserves_song_story_and_genre(self):
        data = self._create(genre="house", storyline="Keep me.")
        updated = self.client.patch(
            data["links"]["self"], json={"title": "Neon Fields (Edit)"})
        self.assertEqual(updated.status_code, 200, updated.text)
        detail = self._get(data["links"]["self"])
        self.assertEqual(detail["title"], "Neon Fields (Edit)")
        self.assertEqual(detail["song_story"], "Keep me.")
        self.assertEqual(detail["genre"], "house")

    def test_editing_listening_link_preserves_story(self):
        data = self._create(storyline="Still here.")
        new_url = "https://open.spotify.com/track/abc123"
        updated = self.client.patch(
            data["links"]["self"], json={"listen_url": new_url})
        self.assertEqual(updated.status_code, 200, updated.text)
        detail = self._get(data["links"]["self"])
        self.assertEqual(detail["listen_url"], new_url)
        self.assertEqual(detail["song_story"], "Still here.")

    def test_story_can_be_explicitly_edited_and_cleared(self):
        data = self._create(storyline="first")
        self_path = data["links"]["self"]
        self.client.patch(self_path, json={"song_story": "second"})
        self.assertEqual(self._get(self_path)["song_story"], "second")
        self.client.patch(self_path, json={"song_story": ""})
        self.assertEqual(self._get(self_path)["song_story"] or "",
                         "")

    def test_invalid_link_is_refused_and_row_untouched(self):
        data = self._create(storyline="intact")
        response = self.client.patch(
            data["links"]["self"], json={"listen_url": "not-a-url"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "bad_request")
        detail = self._get(data["links"]["self"])
        self.assertEqual(detail["listen_url"], MUSIC_URL)
        self.assertEqual(detail["song_story"], "intact")

    def test_patch_without_editable_fields_is_bad_request(self):
        data = self._create()
        response = self.client.patch(data["links"]["self"], json={})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "bad_request")

    def test_blank_title_is_refused(self):
        data = self._create()
        response = self.client.patch(
            data["links"]["self"], json={"title": "   "})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._get(data["links"]["self"])["title"],
                         "Neon Fields")

    def test_changes_survive_a_relist(self):
        data = self._create(title="Persisted", storyline="durable")
        self.client.patch(data["links"]["self"], json={"title": "Persisted v2"})
        listing = self.client.get("/api/v1/tracks").json()["data"]
        rows = {row["track_id"]: row for row in listing["tracks"]}
        self.assertEqual(rows[data["track_id"]]["title"], "Persisted v2")
        self.assertEqual(rows[data["track_id"]]["song_story"], "durable")


class TestReleaseOwnership(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = PersistenceService(
            os.path.join(self._tmp.name, "db.sqlite"))
        self.client = TestClient(create_app(self.storage),
                                 raise_server_exceptions=False)

    def tearDown(self):
        self.storage.close()
        self._tmp.cleanup()

    def _login(self, email):
        response = self.client.post("/api/v1/auth/login", json={"email": email})
        return response.json()["data"]["session"]["session_token"]

    def test_owner_release_cannot_be_edited_by_another_user(self):
        owner_token = self._login("owner@example.com")
        data = self.client.post(
            "/api/v1/tracks/releases",
            json={"title": "Owned", "artist_name": "O", "music_url": MUSIC_URL},
            headers={"Authorization": f"Bearer {owner_token}"},
        ).json()["data"]

        other_token = self._login("other@example.com")
        response = self.client.patch(
            data["links"]["self"], json={"title": "Hijacked"},
            headers={"Authorization": f"Bearer {other_token}"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "forbidden")

        # ...but the owner can edit it.
        ok = self.client.patch(
            data["links"]["self"], json={"title": "Owned v2"},
            headers={"Authorization": f"Bearer {owner_token}"})
        self.assertEqual(ok.status_code, 200)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
