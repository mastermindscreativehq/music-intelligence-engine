"""Gmail send regression tests (offline; Google is never contacted).

Pins requirement 4 of the production repair: outreach actually sends from the
operator's OWN connected Gmail account, with a least-privilege ``gmail.send``
scope, and every failure mode answers with a *clear, actionable* error instead
of a blanket 500:

- never connected          -> ``no_connected_gmail`` (400)
- grant expired/revoked    -> ``gmail_reauth_required`` (400)
- consent without scope    -> ``gmail_scope_missing`` (403)
- anything else            -> ``gmail_send_failed`` (500)

The Google client is faked at ``gmail_provider.build`` so the real provider
code (message construction, From = connected account, error classification) is
exercised end to end without a network call.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

import gmail_provider
import outreach.service as outreach_service
from backend import routes
from gmail_provider import GmailProvider, GmailSendError
from database.service import PersistenceService
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


def _connect(storage, user_id, *, email="operator@example.com",
             scopes=SEND_SCOPE, refresh_token="rt-1", google_id=None):
    storage.auth_save_google_account({
        "user_id": user_id, "google_id": google_id or f"g-{email}",
        "email": email,
        "picture": None, "access_token": "at-1",
        "refresh_token": refresh_token,
        "token_expiry": "2030-01-01T00:00:00+00:00", "scopes": scopes,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    })


class _HttpResp:
    def __init__(self, status, reason="Error"):
        self.status = status
        self.reason = reason


def _http_error(status, message="boom"):
    content = json.dumps(
        {"error": {"code": status, "message": message}}).encode()
    return HttpError(_HttpResp(status), content)


def _fake_build(payload=None, error=None, captured=None):
    """Return a ``build`` stand-in mirroring the chained Gmail client API."""
    payload = payload if payload is not None else {"id": "m1", "threadId": "t1"}

    class _Exec:
        def execute(self):
            if error is not None:
                raise error
            return payload

    class _Messages:
        def send(self, userId=None, body=None):
            if captured is not None:
                captured["userId"] = userId
                captured["body"] = body
            return _Exec()

    class _Users:
        def messages(self):
            return _Messages()

    class _Service:
        def users(self):
            return _Users()

    def build(service, version, credentials=None, cache_discovery=False,
              **kwargs):
        if captured is not None:
            captured["service"] = service
            captured["version"] = version
            captured["credentials"] = credentials
        return _Service()

    return build


class GmailProviderUnitCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = PersistenceService(
            os.path.join(self._tmp.name, "db.sqlite"))
        self.provider = GmailProvider(self.storage)

    def tearDown(self):
        self.storage.close()
        self._tmp.cleanup()


class TestSendClassification(GmailProviderUnitCase):
    def test_never_connected_is_no_connected_gmail(self):
        with self.assertRaises(GmailSendError) as ctx:
            self.provider.send_email("u_1", "dj@example.com", "s", "b")
        self.assertEqual(ctx.exception.code, "no_connected_gmail")

    def test_success_sends_from_connected_account(self):
        from auth.service import AuthService
        user = AuthService(self.storage).create_user("operator@example.com")
        _connect(self.storage, user["user_id"], email="me@example.com")
        captured = {}
        with mock.patch.object(gmail_provider, "build",
                               _fake_build(captured=captured)):
            result = self.provider.send_email(
                user["user_id"], "dj@example.com", "Hello", "Body")
        self.assertEqual(result["sender_email"], "me@example.com")
        self.assertEqual(result["message_id"], "m1")
        self.assertEqual(captured["service"], "gmail")
        # From is the connected account; encoded raw carries the subject.
        import base64
        raw = base64.urlsafe_b64decode(captured["body"]["raw"]).decode()
        lowered = raw.lower()
        self.assertIn("from: me@example.com", lowered)
        self.assertIn("to: dj@example.com", lowered)
        self.assertIn("subject: hello", lowered)

    def test_missing_send_scope_detected_without_calling_google(self):
        from auth.service import AuthService
        user = AuthService(self.storage).create_user("operator@example.com")
        _connect(self.storage, user["user_id"],
                 scopes="openid email")
        with mock.patch.object(gmail_provider, "build") as build:
            with self.assertRaises(GmailSendError) as ctx:
                self.provider.send_email(
                    user["user_id"], "dj@example.com", "s", "b")
        self.assertEqual(ctx.exception.code, "gmail_scope_missing")
        build.assert_not_called()

    def test_unknown_scope_column_does_not_block_send(self):
        from auth.service import AuthService
        user = AuthService(self.storage).create_user("operator@example.com")
        _connect(self.storage, user["user_id"], scopes=None)
        with mock.patch.object(gmail_provider, "build", _fake_build()):
            result = self.provider.send_email(
                user["user_id"], "dj@example.com", "s", "b")
        self.assertEqual(result["message_id"], "m1")

    def test_revoked_refresh_token_is_reauth_required(self):
        from auth.service import AuthService
        user = AuthService(self.storage).create_user("operator@example.com")
        _connect(self.storage, user["user_id"])
        error = RefreshError("invalid_grant: Token has been expired or revoked")
        with mock.patch.object(gmail_provider, "build",
                               _fake_build(error=error)):
            with self.assertRaises(GmailSendError) as ctx:
                self.provider.send_email(
                    user["user_id"], "dj@example.com", "s", "b")
        self.assertEqual(ctx.exception.code, "gmail_reauth_required")

    def test_http_401_is_reauth_required(self):
        from auth.service import AuthService
        user = AuthService(self.storage).create_user("operator@example.com")
        _connect(self.storage, user["user_id"])
        with mock.patch.object(gmail_provider, "build",
                               _fake_build(error=_http_error(401))):
            with self.assertRaises(GmailSendError) as ctx:
                self.provider.send_email(
                    user["user_id"], "dj@example.com", "s", "b")
        self.assertEqual(ctx.exception.code, "gmail_reauth_required")

    def test_http_403_insufficient_scope_is_scope_missing(self):
        from auth.service import AuthService
        user = AuthService(self.storage).create_user("operator@example.com")
        _connect(self.storage, user["user_id"])
        error = _http_error(
            403, "Request had insufficient authentication scopes.")
        with mock.patch.object(gmail_provider, "build",
                               _fake_build(error=error)):
            with self.assertRaises(GmailSendError) as ctx:
                self.provider.send_email(
                    user["user_id"], "dj@example.com", "s", "b")
        self.assertEqual(ctx.exception.code, "gmail_scope_missing")

    def test_other_http_error_is_send_failed(self):
        from auth.service import AuthService
        user = AuthService(self.storage).create_user("operator@example.com")
        _connect(self.storage, user["user_id"])
        with mock.patch.object(gmail_provider, "build",
                               _fake_build(error=_http_error(500, "server"))):
            with self.assertRaises(GmailSendError) as ctx:
                self.provider.send_email(
                    user["user_id"], "dj@example.com", "s", "b")
        self.assertEqual(ctx.exception.code, "gmail_send_failed")


class TestSendRouteFastAPI(unittest.TestCase):
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

    def _login(self, email):
        resp = self.client.post("/api/v1/auth/login", json={"email": email})
        self.assertEqual(resp.status_code, 200)
        return resp.json()["data"]["session"]["session_token"]

    def _auth(self, token):
        return {"Authorization": f"Bearer {token}"}

    def _ready_outreach(self):
        record = outreach_service.create_outreach(
            self.storage,
            payload={"recipient": {"email": "dj@example.com"},
                     "subject": "Hello", "message": "Body"})
        return record["outreach_id"]

    def test_send_requires_login(self):
        oid = self._ready_outreach()
        resp = self.client.post(f"/api/v1/outreach/{oid}/send")
        self.assertEqual(resp.status_code, 401)

    def test_send_without_connected_gmail_is_clear_400(self):
        token = self._login("sender1@example.com")
        oid = self._ready_outreach()
        resp = self.client.post(f"/api/v1/outreach/{oid}/send",
                                headers=self._auth(token))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "no_connected_gmail")

    def test_send_success_records_sent_event(self):
        from auth.service import AuthService
        user = AuthService(self.storage).create_user("sender2@example.com")
        _connect(self.storage, user["user_id"], email="sender2@example.com")
        token = self._login("sender2@example.com")
        oid = self._ready_outreach()
        with mock.patch.object(gmail_provider, "build", _fake_build()):
            resp = self.client.post(f"/api/v1/outreach/{oid}/send",
                                    headers=self._auth(token))
        self.assertEqual(resp.status_code, 200)
        events = [a["event"] for a in resp.json()["data"]["attempts"]]
        self.assertIn("sent", events)

    def test_send_reauth_required_is_400(self):
        from auth.service import AuthService
        user = AuthService(self.storage).create_user("sender3@example.com")
        _connect(self.storage, user["user_id"], email="sender3@example.com")
        token = self._login("sender3@example.com")
        oid = self._ready_outreach()
        with mock.patch.object(gmail_provider, "build",
                               _fake_build(error=_http_error(401))):
            resp = self.client.post(f"/api/v1/outreach/{oid}/send",
                                    headers=self._auth(token))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "gmail_reauth_required")

    def test_send_scope_missing_is_403_and_records_failure(self):
        from auth.service import AuthService
        user = AuthService(self.storage).create_user("sender4@example.com")
        _connect(self.storage, user["user_id"], email="sender4@example.com",
                 scopes="openid email")
        token = self._login("sender4@example.com")
        oid = self._ready_outreach()
        resp = self.client.post(f"/api/v1/outreach/{oid}/send",
                                headers=self._auth(token))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "gmail_scope_missing")
        events = [a["event"] for a in
                  outreach_service.get_outreach(
                      self.storage, oid)["attempts"]]
        self.assertIn("failed", events)


class TestSendRouteDispatch(unittest.TestCase):
    """The other adapter (``routes.dispatch``) maps the same codes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = PersistenceService(
            os.path.join(self._tmp.name, "db.sqlite"))

    def tearDown(self):
        self.storage.close()
        self._tmp.cleanup()

    def _outreach(self):
        return outreach_service.create_outreach(
            self.storage,
            payload={"recipient": {"email": "dj@example.com"},
                     "subject": "s", "message": "b"})["outreach_id"]

    def test_dispatch_no_connection_is_400(self):
        from auth.service import AuthService
        svc = AuthService(self.storage)
        user = svc.create_user("d1@example.com")
        session = svc.create_session(user["user_id"])
        oid = self._outreach()
        status, body = routes.dispatch(
            self.storage, "POST", f"/api/v1/outreach/{oid}/send", {},
            headers={"authorization": f"Bearer {session['session_token']}"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "no_connected_gmail")

    def test_dispatch_success_is_200(self):
        from auth.service import AuthService
        svc = AuthService(self.storage)
        user = svc.create_user("d2@example.com")
        _connect(self.storage, user["user_id"], email="d2@example.com")
        session = svc.create_session(user["user_id"])
        oid = self._outreach()
        with mock.patch.object(gmail_provider, "build", _fake_build()):
            status, body = routes.dispatch(
                self.storage, "POST", f"/api/v1/outreach/{oid}/send", {},
                headers={"authorization":
                         f"Bearer {session['session_token']}"})
        self.assertEqual(status, 200)
        events = [a["event"] for a in body["data"]["attempts"]]
        self.assertIn("sent", events)


class TestOAuthDeniedCallback(unittest.TestCase):
    """Google's ?error= callback param yields a clear envelope, not 500."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = PersistenceService(
            os.path.join(self._tmp.name, "db.sqlite"))

    def tearDown(self):
        self.storage.close()
        self._tmp.cleanup()

    def test_dispatch_oauth_denied(self):
        status, body = routes.dispatch(
            self.storage, "GET", "/api/v1/auth/google/callback",
            {"error": ["access_denied"], "state": ["s"]})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "oauth_denied")
        self.assertIn("access_denied", body["error"]["message"])

    def test_fastapi_oauth_denied(self):
        from fastapi.testclient import TestClient
        from backend.app import create_app
        client = TestClient(create_app(self.storage),
                            raise_server_exceptions=False)
        resp = client.get(
            "/api/v1/auth/google/callback?error=access_denied&state=s",
            follow_redirects=False)
        # The callback is a top-level browser hop: a cancelled consent must
        # return the operator to the console with a clear reason, never strand
        # them on a JSON error screen.
        self.assertEqual(resp.status_code, 303)
        self.assertTrue(resp.headers["location"].endswith(
            "/?oauth=error&reason=oauth_denied"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
