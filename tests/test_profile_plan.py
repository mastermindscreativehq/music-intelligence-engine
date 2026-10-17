"""Profile edit + truthful plan endpoint (offline, no network).

Covers the two net-new API surfaces added for frontend completeness:

* ``PATCH /api/v1/auth/profile`` — an authenticated operator can change only
  their display name (never the email), and the change is persisted. Both
  the stdlib dispatcher and the FastAPI adapter are exercised so SQLite
  (dev) and PostgreSQL (prod) behaviour stay in lockstep.
* ``GET /api/v1/plan`` — a truthful entitlement report. This deployment has
  no billing, so the endpoint must say so and must never imply an upgrade is
  purchasable.

Nothing here contacts Google or sends mail.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from auth.service import AuthService
from backend import routes
from database.service import PersistenceService


def _body(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


class TestAuthServiceUpdateName(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = PersistenceService(
            os.path.join(self._tmp.name, "db.sqlite"))
        self.svc = AuthService(self.storage)

    def tearDown(self):
        self.storage.close()
        self._tmp.cleanup()

    def test_update_name_persists_and_preserves_email(self):
        user = self.svc.create_user("artist@example.com", "Old Name")
        updated = self.svc.update_user_name(user["user_id"], "New Name")
        self.assertEqual(updated["name"], "New Name")
        self.assertEqual(updated["email"], "artist@example.com")
        # A fresh read reflects the write.
        self.assertEqual(
            self.svc.get_user_by_id(user["user_id"])["name"], "New Name")

    def test_update_name_can_clear_to_null(self):
        user = self.svc.create_user("clear@example.com", "Temp")
        updated = self.svc.update_user_name(user["user_id"], None)
        self.assertIsNone(updated["name"])


class TestProfileAndPlanDispatch(unittest.TestCase):
    """The stdlib dispatcher (used by webapp.py and tests)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = PersistenceService(
            os.path.join(self._tmp.name, "db.sqlite"))
        self.svc = AuthService(self.storage)

    def tearDown(self):
        self.storage.close()
        self._tmp.cleanup()

    def _token(self, email="operator@example.com"):
        user = self.svc.create_user(email)
        return self.svc.create_session(user["user_id"])["session_token"]

    def _dispatch(self, method, path, *, body=None, token=None, params=None):
        headers = {}
        if token:
            headers["authorization"] = f"Bearer {token}"
        return routes.dispatch(
            self.storage, method, path, params or {}, body, headers=headers)

    def test_profile_patch_requires_a_session(self):
        status, envelope = self._dispatch(
            "PATCH", "/api/v1/auth/profile", body=_body({"name": "X"}))
        self.assertEqual(status, 401)
        self.assertEqual(envelope["error"]["code"], "unauthorized")

    def test_profile_patch_updates_name(self):
        token = self._token()
        status, envelope = self._dispatch(
            "PATCH", "/api/v1/auth/profile",
            body=_body({"name": "  Stage Name  "}), token=token)
        self.assertEqual(status, 200)
        self.assertEqual(envelope["ok"], True)
        self.assertEqual(envelope["data"]["user"]["name"], "Stage Name")
        # And it is visible through /auth/me.
        status, me = self._dispatch("GET", "/api/v1/auth/me", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(me["data"]["user"]["name"], "Stage Name")

    def test_profile_patch_never_changes_email(self):
        token = self._token("locked@example.com")
        status, envelope = self._dispatch(
            "PATCH", "/api/v1/auth/profile",
            body=_body({"name": "N", "email": "attacker@example.com"}),
            token=token)
        self.assertEqual(status, 200)
        self.assertEqual(
            envelope["data"]["user"]["email"], "locked@example.com")

    def test_profile_patch_rejects_non_string_name(self):
        token = self._token()
        status, envelope = self._dispatch(
            "PATCH", "/api/v1/auth/profile",
            body=_body({"name": 123}), token=token)
        self.assertEqual(status, 400)
        self.assertEqual(envelope["error"]["code"], "bad_request")

    def test_profile_patch_clears_name_with_empty_string(self):
        token = self._token()
        self._dispatch("PATCH", "/api/v1/auth/profile",
                       body=_body({"name": "Given"}), token=token)
        status, envelope = self._dispatch(
            "PATCH", "/api/v1/auth/profile",
            body=_body({"name": ""}), token=token)
        self.assertEqual(status, 200)
        self.assertIsNone(envelope["data"]["user"]["name"])

    def test_plan_is_truthful_about_no_billing(self):
        status, envelope = self._dispatch("GET", "/api/v1/plan")
        self.assertEqual(status, 200)
        data = envelope["data"]
        self.assertFalse(data["plan"]["billing_configured"])
        self.assertFalse(data["plan"]["upgrade_available"])
        self.assertFalse(data["signed_in"])
        self.assertIn("no billing", data["plan"]["note"].lower())

    def test_plan_reports_signed_in(self):
        token = self._token()
        status, envelope = self._dispatch(
            "GET", "/api/v1/plan", token=token)
        self.assertEqual(status, 200)
        self.assertTrue(envelope["data"]["signed_in"])


class TestProfileAndPlanFastAPI(unittest.TestCase):
    """The FastAPI adapter (used by the Railway production app)."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from backend.app import create_app
        cls._tmp = tempfile.TemporaryDirectory()
        cls.storage = PersistenceService(
            os.path.join(cls._tmp.name, "db.sqlite"))
        cls.client = TestClient(create_app(cls.storage),
                                raise_server_exceptions=False)
        # LIFO: close the DB before removing its temp dir (Windows holds a
        # lock on the file until the connection is closed).
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.addClassCleanup(cls.storage.close)

    def _login(self, email):
        response = self.client.post("/api/v1/auth/login", json={"email": email})
        self.assertEqual(response.status_code, 200)
        return response.json()["data"]["session"]["session_token"]

    def _auth(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_profile_patch_requires_a_session(self):
        response = self.client.patch("/api/v1/auth/profile",
                                     json={"name": "X"})
        self.assertEqual(response.status_code, 401)

    def test_profile_patch_updates_name(self):
        token = self._login("fastapi-artist@example.com")
        response = self.client.patch("/api/v1/auth/profile",
                                     json={"name": "Fast Name"},
                                     headers=self._auth(token))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["user"]["name"], "Fast Name")

    def test_plan_truthful(self):
        response = self.client.get("/api/v1/plan")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertFalse(data["plan"]["billing_configured"])
        self.assertFalse(data["plan"]["upgrade_available"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
