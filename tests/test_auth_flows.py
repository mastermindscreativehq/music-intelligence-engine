"""Auth flow regression tests (offline, no network).

Pins the two-requirement repair:

1. **Separate Google scopes.** Signing in (``/auth/google/start``) requests
   identity scopes only — ``openid`` + userinfo — and never bundles Gmail
   access. Sending outreach needs the minimal ``gmail.send`` scope, granted
   through a distinct, authenticated ``/auth/google/connect`` hop.

2. **A working Gmail send path.** Stored Google credentials live behind the
   backend-agnostic ``auth_*_google_account`` methods, so the same code reads
   them on SQLite (dev) and PostgreSQL (production). A reconnect that omits a
   refresh token must never erase the one already on file.

The callback logic is exercised with the network calls mocked: Google's token
exchange is stubbed and ``googleapiclient.discovery.build`` returns a fake
userinfo service. Nothing here contacts Google.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import auth.google_oauth as google_oauth
from auth.service import AuthService
from backend import routes
from database.service import PersistenceService

REDIRECT_URI = "http://localhost:8788/api/v1/auth/google/callback"
GMAIL_SEND = "https://www.googleapis.com/auth/gmail.send"


class TestScopeSeparation(unittest.TestCase):
    def test_signin_scopes_carry_no_mailbox_access(self):
        self.assertNotIn(GMAIL_SEND, google_oauth.SIGNIN_SCOPES)
        self.assertFalse(
            any("gmail" in scope for scope in google_oauth.SIGNIN_SCOPES),
            google_oauth.SIGNIN_SCOPES)
        self.assertIn("openid", google_oauth.SIGNIN_SCOPES)

    def test_send_scope_is_the_minimal_honest_set(self):
        self.assertEqual(google_oauth.GMAIL_SEND_SCOPES, [GMAIL_SEND])
        self.assertNotIn(
            "https://www.googleapis.com/auth/gmail.readonly",
            google_oauth.GMAIL_SEND_SCOPES)


class _ConfiguredOAuthCase(unittest.TestCase):
    def setUp(self):
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


class TestAuthUrlScopes(_ConfiguredOAuthCase):
    def _scopes_in(self, scopes):
        from urllib.parse import parse_qs, urlparse
        url, _ = google_oauth.GoogleOAuth().get_auth_url("S", "verifier", scopes)
        return parse_qs(urlparse(url).query).get("scope", [""])[0].split()

    def test_signin_url_requests_identity_scopes_only(self):
        requested = self._scopes_in(google_oauth.SIGNIN_SCOPES)
        self.assertIn("openid", requested)
        self.assertNotIn(GMAIL_SEND, requested)

    def test_connect_url_requests_send_scope(self):
        requested = self._scopes_in(google_oauth.GMAIL_SEND_SCOPES)
        self.assertEqual(requested, [GMAIL_SEND])


class TestAuthStoreSQLite(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = PersistenceService(
            os.path.join(self._tmp.name, "db.sqlite"))
        self.svc = AuthService(self.storage)

    def tearDown(self):
        self.storage.close()
        self._tmp.cleanup()

    def test_create_user_is_idempotent_and_lowercases_email(self):
        first = self.svc.create_user("Operator@Example.COM", "Op")
        self.assertEqual(first["email"], "operator@example.com")
        again = self.svc.create_user("operator@example.com")
        self.assertEqual(again["user_id"], first["user_id"])
        self.assertEqual(self.svc.get_user_by_email(
            "OPERATOR@example.com")["user_id"], first["user_id"])
        self.assertEqual(self.svc.get_user_by_id(
            first["user_id"])["email"], "operator@example.com")

    def test_session_lifecycle(self):
        user = self.svc.create_user("op@example.com")
        session = self.svc.create_session(user["user_id"])
        self.assertEqual(
            self.svc.get_session(session["session_token"])["user_id"],
            user["user_id"])
        self.svc.revoke_session(session["session_token"])
        self.assertIsNone(self.svc.get_session(session["session_token"]))

    def test_oauth_state_is_single_use_and_keeps_verifier(self):
        user = self.svc.create_user("op@example.com")
        state = self.svc.create_oauth_state(user["user_id"], "the-verifier")
        record = self.svc.consume_oauth_state(state)
        self.assertEqual(record["user_id"], user["user_id"])
        self.assertEqual(record["code_verifier"], "the-verifier")
        self.assertIsNone(self.svc.consume_oauth_state(state))

    def test_google_account_upsert_preserves_refresh_token(self):
        user = self.svc.create_user("op@example.com")
        base = {
            "user_id": user["user_id"], "google_id": "g-1",
            "email": "op@example.com", "picture": "pic",
            "access_token": "at-1", "refresh_token": "rt-1",
            "token_expiry": "2026-01-01T00:00:00+00:00",
            "scopes": GMAIL_SEND,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
        self.storage.auth_save_google_account(base)
        # A second consent that returns no refresh token (Google only sends it
        # on first grant) must NOT wipe the stored one.
        reconnect = dict(base, access_token="at-2", refresh_token=None,
                         picture=None,
                         updated_at="2026-02-01T00:00:00+00:00")
        self.storage.auth_save_google_account(reconnect)
        account = self.storage.auth_get_google_account(user["user_id"])
        self.assertEqual(account["refresh_token"], "rt-1")
        self.assertEqual(account["access_token"], "at-2")
        self.assertEqual(account["picture"], "pic")
        self.assertEqual(
            self.storage.auth_delete_google_account(user["user_id"]), 1)
        self.assertIsNone(
            self.storage.auth_get_google_account(user["user_id"]))

    def test_find_google_account_by_google_id_and_email(self):
        user = self.svc.create_user("op@example.com")
        self.storage.auth_save_google_account({
            "user_id": user["user_id"], "google_id": "g-1",
            "email": "op@example.com", "picture": "pic",
            "access_token": "at", "refresh_token": "rt",
            "token_expiry": "2026-01-01T00:00:00+00:00", "scopes": GMAIL_SEND,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        })
        self.assertEqual(
            self.storage.auth_find_google_account(
                "g-1", user["user_id"], "op@example.com")["user_id"],
            user["user_id"])
        # no google id: falls back to the (user, email) pair
        self.assertEqual(
            self.storage.auth_find_google_account(
                None, user["user_id"], "op@example.com")["user_id"],
            user["user_id"])
        self.assertIsNone(
            self.storage.auth_find_google_account(
                None, "u_nobody", "nobody@example.com"))


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


class TestTwoFlowCallback(_ConfiguredOAuthCase):
    """``_finish_google_callback`` discriminates sign-in from Gmail-connect."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = PersistenceService(
            os.path.join(self._tmp.name, "db.sqlite"))
        self.svc = AuthService(self.storage)

    def tearDown(self):
        self.storage.close()
        self._tmp.cleanup()
        super().tearDown()

    def _session_token(self, redirect):
        from urllib.parse import parse_qs, urlparse
        return parse_qs(urlparse(redirect).query)["auth"][0]

    def _run(self, state, *, email="operator@example.com", google_id="g-1",
             scopes_seen=None, tokens=None):
        info = {"email": email, "id": google_id, "picture": "pic"}
        fake_tokens = tokens or {
            "access_token": "at", "refresh_token": "rt",
            "token_expiry": "2026-01-01T00:00:00+00:00",
            "scopes": [GMAIL_SEND],
        }

        def fake_exchange(self, code, verifier, scopes=None):
            if scopes_seen is not None:
                scopes_seen.append(scopes)
            return fake_tokens

        def fake_build(service, version, credentials=None,
                       cache_discovery=False, **kwargs):
            return _FakeOAuth2Service(info)

        with mock.patch.object(google_oauth.GoogleOAuth, "exchange_code",
                               fake_exchange), \
                mock.patch("googleapiclient.discovery.build", fake_build):
            return routes._finish_google_callback(
                self.svc, self.storage, "the-code", state)

    def test_signin_creates_session_and_stores_no_gmail(self):
        scopes_seen = []
        state = self.svc.create_oauth_state(None, "v")
        status, body = self._run(state, scopes_seen=scopes_seen)
        self.assertEqual(status, 200)
        self.assertEqual(scopes_seen[0], google_oauth.SIGNIN_SCOPES)
        redirect = body["data"]["redirect"]
        self.assertTrue(redirect.startswith("/?oauth=connected&auth="))
        token = self._session_token(redirect)
        session = self.svc.get_session(token)
        self.assertIsNotNone(session)
        # identity only — no mailbox credentials were captured
        self.assertIsNone(
            self.storage.auth_get_google_account(session["user_id"]))
        self.assertEqual(
            self.svc.get_user_by_id(session["user_id"])["email"],
            "operator@example.com")

    def test_signin_reuses_existing_user(self):
        user = self.svc.create_user("operator@example.com")
        state = self.svc.create_oauth_state(None, "v")
        status, body = self._run(state)
        self.assertEqual(status, 200)
        token = self._session_token(body["data"]["redirect"])
        self.assertEqual(self.svc.get_session(token)["user_id"],
                         user["user_id"])

    def test_connect_attaches_send_credentials_and_starts_no_session(self):
        user = self.svc.create_user("operator@example.com")
        scopes_seen = []
        state = self.svc.create_oauth_state(user["user_id"], "v")
        status, body = self._run(state, scopes_seen=scopes_seen)
        self.assertEqual(status, 200)
        self.assertEqual(scopes_seen[0], google_oauth.GMAIL_SEND_SCOPES)
        self.assertEqual(body["data"]["redirect"], "/?oauth=connected")
        account = self.storage.auth_get_google_account(user["user_id"])
        self.assertIsNotNone(account)
        self.assertEqual(account["refresh_token"], "rt")
        self.assertEqual(account["email"], "operator@example.com")

    def test_connect_binds_to_starter_not_answering_identity(self):
        starter = self.svc.create_user("starter@example.com")
        state = self.svc.create_oauth_state(starter["user_id"], "v")
        # A different Google account answers the consent screen.
        status, _ = self._run(state, email="someone-else@example.com")
        self.assertEqual(status, 200)
        account = self.storage.auth_get_google_account(starter["user_id"])
        self.assertEqual(account["email"], "someone-else@example.com")
        # ...but no operator account is created for the answering identity.
        self.assertIsNone(
            self.svc.get_user_by_email("someone-else@example.com"))

    def test_connect_for_missing_operator_is_rejected(self):
        # The schema's FK keeps a state from being bound to a nonexistent
        # user, but the callback still defends the case: if the operator row
        # vanished between connect-start and callback, fail closed.
        user = self.svc.create_user("op@example.com")
        state = self.svc.create_oauth_state(user["user_id"], "v")
        with mock.patch.object(self.svc, "get_user_by_id", return_value=None):
            status, body = self._run(state)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "oauth_state_invalid")

    def test_reused_state_is_rejected(self):
        state = self.svc.create_oauth_state(None, "v")
        status, _ = self._run(state)
        self.assertEqual(status, 200)
        status, body = self._run(state)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "oauth_state_invalid")

    def test_unknown_state_is_rejected(self):
        status, body = self._run("no-such-state")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "oauth_state_invalid")


class TestAuthEndpointsFastAPI(unittest.TestCase):
    """The FastAPI adapter gates Gmail-connect behind a real session."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from backend.app import create_app
        cls._tmp = tempfile.TemporaryDirectory()
        cls.storage = PersistenceService(
            os.path.join(cls._tmp.name, "db.sqlite"))
        cls.client = TestClient(create_app(cls.storage),
                                raise_server_exceptions=False)
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.addClassCleanup(cls.storage.close)

    def setUp(self):
        # Keep the OAuth branch deterministic regardless of the ambient .env.
        self._saved = (
            google_oauth.GOOGLE_CLIENT_ID,
            google_oauth.GOOGLE_CLIENT_SECRET,
            google_oauth.GOOGLE_REDIRECT_URI,
        )
        google_oauth.GOOGLE_CLIENT_ID = ""
        google_oauth.GOOGLE_CLIENT_SECRET = ""
        google_oauth.GOOGLE_REDIRECT_URI = ""

    def tearDown(self):
        (google_oauth.GOOGLE_CLIENT_ID,
         google_oauth.GOOGLE_CLIENT_SECRET,
         google_oauth.GOOGLE_REDIRECT_URI) = self._saved

    def _login(self, email):
        response = self.client.post("/api/v1/auth/login", json={"email": email})
        self.assertEqual(response.status_code, 200)
        return response.json()["data"]["session"]["session_token"]

    def _auth(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_connect_requires_a_session(self):
        response = self.client.get("/api/v1/auth/google/connect")
        self.assertEqual(response.status_code, 401)

    def test_connect_with_session_reaches_oauth_layer(self):
        token = self._login("connecter@example.com")
        response = self.client.get("/api/v1/auth/google/connect",
                                   headers=self._auth(token))
        # Past the auth gate; OAuth is unconfigured in the test env.
        self.assertEqual(response.status_code, 503)

    def test_start_is_anonymous(self):
        response = self.client.get("/api/v1/auth/google/start")
        self.assertEqual(response.status_code, 503)

    def test_me_reflects_signin_and_gmail_flag(self):
        self.assertEqual(self.client.get("/api/v1/auth/me").status_code, 401)
        token = self._login("viewer@example.com")
        response = self.client.get("/api/v1/auth/me", headers=self._auth(token))
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["user"]["email"], "viewer@example.com")
        self.assertFalse(data["gmail_connected"])

    def test_disconnect_requires_a_session_and_never_logs_out(self):
        self.assertEqual(
            self.client.post("/api/v1/auth/google/disconnect").status_code,
            401)
        token = self._login("disconnector@example.com")
        response = self.client.post("/api/v1/auth/google/disconnect",
                                    headers=self._auth(token))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["data"]["gmail_connected"])
        # session survives a Gmail disconnect
        self.assertEqual(
            self.client.get("/api/v1/auth/me",
                            headers=self._auth(token)).status_code, 200)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
