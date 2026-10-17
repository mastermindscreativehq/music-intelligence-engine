"""Regression: an adopted Google sign-in session must authenticate GET reads.

Pins the production "signed in but the header still shows Sign in with Google"
bug. The frontend's ``mergeHeaders`` early-returned (``if (!extra) return
base;``) BEFORE attaching the bearer token, so every authenticated GET —
including ``/api/v1/auth/me`` on the Profile page — was sent ANONYMOUSLY. The
callback had already stored a valid session and the token lived in
localStorage, so this was neither a missing cookie nor a missing server-side
session: it was a frontend request that never carried the credential.

The test runs the REAL ``frontend/js/api.js`` under Node (browser shims only,
fetch stubbed) to reproduce the exact browser path: adopt the ``auth=<token>``
handed back by the OAuth redirect, then call ``api.me()``. It asserts the
client emitted ``Authorization: Bearer <token>`` and then replays that header
against the real backend ``/api/v1/auth/me`` and requires 200. Skipped cleanly
when Node is unavailable.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from auth.service import AuthService
from backend.app import create_app
from database.service import PersistenceService

ROOT = Path(__file__).resolve().parents[1]
API_JS = ROOT / "frontend" / "js" / "api.js"
HARNESS = ROOT / "tests" / "frontend_me_harness.mjs"


class TestAdoptedSessionAuthenticatesReads(unittest.TestCase):
    def setUp(self):
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

    @property
    def client(self):
        if self._client is None:
            from fastapi.testclient import TestClient
            self._client = TestClient(create_app(self.storage),
                                      raise_server_exceptions=False)
        return self._client

    def _run_browser_harness(self, token):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node not available to execute frontend api.js")
        env = dict(os.environ,
                   MIE_TEST_API_JS=str(API_JS),
                   MIE_TEST_TOKEN=token)
        proc = subprocess.run(
            [node, str(HARNESS)],
            capture_output=True, text=True, encoding="utf-8",
            timeout=60, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_me_request_carries_the_adopted_bearer_token(self):
        user = self.svc.create_user("signed-in@example.com")
        session = self.svc.create_session(user["user_id"])
        token = session["session_token"]

        result = self._run_browser_harness(token)

        self.assertTrue(result["adopted"],
                        "the OAuth-return token must be adopted")
        self.assertEqual(result["storedToken"], token)
        self.assertEqual(result["authorization"], f"Bearer {token}",
                         "the frontend must attach the bearer on GET reads")

        me = self.client.get("/api/v1/auth/me",
                             headers={"Authorization": result["authorization"]})
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["data"]["user"]["email"],
                         "signed-in@example.com")

    def test_anonymous_me_is_unauthorized(self):
        # The counterpart guard: without the header the backend is 401, so a
        # future regression that drops the bearer fails the test above rather
        # than silently degrading to an anonymous read.
        me = self.client.get("/api/v1/auth/me")
        self.assertEqual(me.status_code, 401)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
