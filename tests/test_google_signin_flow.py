"""End-to-end Google sign-in/registration coverage (offline, no network).

Pins the first-time-registration repair for the operator console:

- A brand-new Google user clicking "Continue with Google" is created and
  signed in without any separate registration step.
- A returning Google user is recognized and signed in to the SAME account;
  repeated sign-ins never create duplicate user rows.
- The session minted by the callback persists and authorizes requests.
- A cancelled/failed OAuth hop redirects the browser back to the console with
  a machine-readable reason (``/?oauth=error&reason=<code>``) instead of a raw
  JSON screen or an endless loading state.

Google's token exchange and userinfo call are mocked; nothing here contacts
Google.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

import auth.google_oauth as google_oauth
from auth.service import AuthService
from backend import routes
from backend.app import create_app
from database.service import PersistenceService

PROD_CONSOLE = "https://music-intelligence-engine-five.vercel.app"


class _FakeExec:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _FakeUserinfo:
    def __init__(self, payload):
        self._payload = payload

    def get(self):
        return _FakeExec(self._payload)


class _FakeOAuth2Service:
    def __init__(self, payload):
        self._payload = payload

    def userinfo(self):
        return _FakeUserinfo(self._payload)


class GoogleSignInFlowTests(unittest.TestCase):
    def setUp(self):
        self._saved = (
            google_oauth.GOOGLE_CLIENT_ID,
            google_oauth.GOOGLE_CLIENT_SECRET,
            google_oauth.GOOGLE_REDIRECT_URI,
        )
        self._saved_frontend = os.environ.pop("MIE_FRONTEND_ORIGIN", None)
        google_oauth.GOOGLE_CLIENT_ID = "test-client.apps.googleusercontent.com"
        google_oauth.GOOGLE_CLIENT_SECRET = "test-secret"
        google_oauth.GOOGLE_REDIRECT_URI = (
            "http://localhost:8788/api/v1/auth/google/callback")

        self._tmp = tempfile.TemporaryDirectory()
        self.storage = PersistenceService(
            os.path.join(self._tmp.name, "db.sqlite"))
        self.svc = AuthService(self.storage)
        self._client = None

    def tearDown(self):
        if self._client is not None:
            self._client.close()
        self.storage.close()
        self._tmp.cleanup()
        (google_oauth.GOOGLE_CLIENT_ID,
         google_oauth.GOOGLE_CLIENT_SECRET,
         google_oauth.GOOGLE_REDIRECT_URI) = self._saved
        if self._saved_frontend is not None:
            os.environ["MIE_FRONTEND_ORIGIN"] = self._saved_frontend

    @property
    def client(self):
        if self._client is None:
            from fastapi.testclient import TestClient
            self._client = TestClient(create_app(self.storage),
                                      raise_server_exceptions=False)
        return self._client

    def _callback(self, *, email="fresh@example.com", google_id="g-fresh"):
        state = self.svc.create_oauth_state(None, "verifier")
        info = {"email": email, "id": google_id, "picture": "pic"}
        tokens = {"access_token": "at", "refresh_token": "rt",
                  "token_expiry": "2026-01-01T00:00:00+00:00",
                  "scopes": ["openid"]}

        def fake_exchange(self, code, verifier, scopes=None):
            return tokens

        def fake_build(service, version, credentials=None,
                       cache_discovery=False, **kwargs):
            return _FakeOAuth2Service(info)

        with mock.patch.object(google_oauth.GoogleOAuth, "exchange_code",
                               fake_exchange), \
                mock.patch("googleapiclient.discovery.build", fake_build):
            return self.client.get(
                f"/api/v1/auth/google/callback?code=the-code&state={state}",
                follow_redirects=False)

    @staticmethod
    def _auth_token(location):
        return parse_qs(urlparse(location).query)["auth"][0]

    def _me(self, token):
        return self.client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"})

    # -- first-time registration -------------------------------------------

    def test_first_time_user_is_created_and_signed_in(self):
        self.assertIsNone(self.svc.get_user_by_email("fresh@example.com"))
        resp = self._callback()
        self.assertEqual(resp.status_code, 303)
        location = resp.headers["location"]
        self.assertTrue(location.startswith(PROD_CONSOLE))
        self.assertEqual(urlparse(location).path, "/")
        self.assertIn("oauth=connected", location)

        token = self._auth_token(location)
        me = self._me(token)
        self.assertEqual(me.status_code, 200)
        user = me.json()["data"]["user"]
        self.assertEqual(user["email"], "fresh@example.com")
        self.assertEqual(user["user_id"],
                         self.svc.get_user_by_email("fresh@example.com")["user_id"])

    def test_returning_user_is_recognized_without_duplicates(self):
        first = self._auth_token(self._callback().headers["location"])
        first_id = self._me(first).json()["data"]["user"]["user_id"]
        second = self._auth_token(self._callback().headers["location"])
        second_id = self._me(second).json()["data"]["user"]["user_id"]
        self.assertEqual(first_id, second_id)
        row = self.storage._conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE email=?",
            ("fresh@example.com",)).fetchone()
        self.assertEqual(int(row["n"]), 1)

    def test_session_persists_and_authorizes_requests(self):
        token = self._auth_token(self._callback().headers["location"])
        self.assertEqual(self._me("not-a-token").status_code, 401)
        self.assertEqual(self._me(token).status_code, 200)

    # -- cancellation / failure are clear, never a stuck JSON screen --------

    def test_cancelled_consent_redirects_to_console_with_reason(self):
        resp = self.client.get(
            "/api/v1/auth/google/callback?error=access_denied&state=s",
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(
            resp.headers["location"],
            f"{PROD_CONSOLE}/?oauth=error&reason=oauth_denied")

    def test_invalid_state_redirects_to_console_with_reason(self):
        resp = self.client.get(
            "/api/v1/auth/google/callback?code=c&state=unknown",
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertTrue(resp.headers["location"].endswith(
            "/?oauth=error&reason=oauth_state_invalid"))

    def test_missing_state_or_code_redirects_to_console(self):
        resp = self.client.get(
            "/api/v1/auth/google/callback?code=c",
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertTrue(resp.headers["location"].endswith(
            "/?oauth=error&reason=bad_request"))

    # -- helper + service-layer contract -----------------------------------

    def test_oauth_error_target_maps_error_envelope(self):
        err = {"ok": False, "data": None,
               "error": {"code": "oauth_denied", "message": "x"}}
        self.assertEqual(routes.oauth_error_target(err),
                         "/?oauth=error&reason=oauth_denied")
        ok = {"ok": True, "data": {"redirect": "/?oauth=connected"},
              "error": None}
        self.assertIsNone(routes.oauth_error_target(ok))

    def test_dispatch_callback_error_envelope_is_unchanged(self):
        # The framework-free layer keeps its typed error envelope; only the
        # HTTP adapters turn a callback failure into a console redirect.
        status, body = routes.dispatch(
            self.storage, "GET", "/api/v1/auth/google/callback",
            {"error": ["access_denied"], "state": ["s"]})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "oauth_denied")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
