"""Google OAuth PKCE regression tests (offline, no network).

Pins the fix for the production failure

    oauth_exchange_failed: (invalid_grant) Missing code verifier

Root cause: ``google_auth_oauthlib.Flow.authorization_url()`` generates a PKCE
``code_verifier`` and embeds the matching S256 ``code_challenge`` in the
authorization URL. The old flow built a NEW ``Flow`` at exchange time, so
``flow.code_verifier`` was ``None`` and the token request carried no verifier
while the authorization request had carried a challenge — Google rejects that
with ``invalid_grant: Missing code verifier``.

The fix persists the verifier with the single-use ``state`` row and replays it
at exchange. These tests assert that contract without contacting Google.
"""

from __future__ import annotations

import base64
import hashlib
import os
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse

import auth.google_oauth as google_oauth
from auth.service import AuthService
from database.schema import SCHEMA_VERSION
from database.service import PersistenceService

REDIRECT_URI = "http://localhost:8788/api/v1/auth/google/callback"


def _expected_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().split("=")[0]


class _ConfiguredOAuthCase(unittest.TestCase):
    def setUp(self):
        # Make the module "configured" without touching the real environment.
        self._saved = (
            google_oauth.GOOGLE_CLIENT_ID,
            google_oauth.GOOGLE_CLIENT_SECRET,
            google_oauth.GOOGLE_REDIRECT_URI,
        )
        google_oauth.GOOGLE_CLIENT_ID = "test-client.apps.googleusercontent.com"
        google_oauth.GOOGLE_CLIENT_SECRET = "test-secret"
        google_oauth.GOOGLE_REDIRECT_URI = REDIRECT_URI

    def tearDown(self):
        (google_oauth.GOOGLE_CLIENT_ID,
         google_oauth.GOOGLE_CLIENT_SECRET,
         google_oauth.GOOGLE_REDIRECT_URI) = self._saved

    def oauth(self):
        return google_oauth.GoogleOAuth()


class TestCodeVerifier(unittest.TestCase):
    def test_verifier_is_rfc7636_shaped(self):
        oauth = google_oauth.GoogleOAuth()
        verifier = oauth.new_code_verifier()
        self.assertGreaterEqual(len(verifier), 43)
        self.assertLessEqual(len(verifier), 128)
        allowed = set(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
            "0123456789-._~")
        self.assertTrue(set(verifier) <= allowed, verifier)
        self.assertNotEqual(oauth.new_code_verifier(), verifier)


class TestAuthUrlChallenge(_ConfiguredOAuthCase):
    def test_url_challenge_matches_supplied_verifier(self):
        oauth = self.oauth()
        verifier = oauth.new_code_verifier()
        url, returned = oauth.get_auth_url("STATE-123", verifier)
        self.assertEqual(returned, verifier)

        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["state"][0], "STATE-123")
        self.assertEqual(query["redirect_uri"][0], REDIRECT_URI)
        self.assertEqual(query["code_challenge_method"][0], "S256")
        self.assertEqual(
            query["code_challenge"][0], _expected_challenge(verifier))

    def test_authorization_url_always_carries_a_challenge(self):
        # PKCE is never silently disabled: even without a caller-supplied
        # verifier the URL carries an S256 challenge and one is returned.
        oauth = self.oauth()
        url, verifier = oauth.get_auth_url("STATE-456")
        self.assertTrue(verifier)
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["code_challenge_method"][0], "S256")
        self.assertEqual(
            query["code_challenge"][0], _expected_challenge(verifier))


class TestStateCarriesVerifier(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = PersistenceService(
            os.path.join(self._tmp.name, "db.sqlite"))
        self.svc = AuthService(self.storage)

    def tearDown(self):
        self.storage.close()
        self._tmp.cleanup()

    def test_schema_has_code_verifier_column(self):
        columns = [row[1] for row in self.storage._conn.execute(
            "PRAGMA table_info(oauth_state)")]
        self.assertIn("code_verifier", columns)
        self.assertGreaterEqual(SCHEMA_VERSION, 11)

    def test_verifier_survives_create_and_consume(self):
        user = self.svc.create_user("operator@example.com")
        verifier = "v" * 64
        state = self.svc.create_oauth_state(user["user_id"], verifier)
        record = self.svc.consume_oauth_state(state)
        self.assertIsNotNone(record)
        self.assertEqual(record["code_verifier"], verifier)
        self.assertEqual(record["user_id"], user["user_id"])
        # single-use: a replayed state is rejected
        self.assertIsNone(self.svc.consume_oauth_state(state))

    def test_anonymous_state_allows_null_user(self):
        state = self.svc.create_oauth_state(None, "verifier-value")
        record = self.svc.consume_oauth_state(state)
        self.assertIsNone(record["user_id"])
        self.assertEqual(record["code_verifier"], "verifier-value")


class TestExchangeReplaysVerifier(_ConfiguredOAuthCase):
    def test_fetch_token_receives_the_persisted_verifier(self):
        # intercept the lowest-level network call to prove the exact value
        # google-auth-oauthlib would transmit.
        from requests_oauthlib import OAuth2Session

        captured = {}
        original = OAuth2Session.fetch_token

        def fake_fetch(self, *args, **kwargs):
            captured["code_verifier"] = kwargs.get("code_verifier")
            raise RuntimeError("stop-before-network")

        OAuth2Session.fetch_token = fake_fetch
        try:
            with self.assertRaises(RuntimeError):
                self.oauth().exchange_code("THE-CODE", "persisted-verifier")
        finally:
            OAuth2Session.fetch_token = original

        self.assertEqual(captured.get("code_verifier"),
                         "persisted-verifier")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
