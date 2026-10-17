"""Split-origin (Vercel console <-> Railway API) regression tests.

The console and the API are different origins in production, which produced
two real bugs:

1. The Google OAuth callback finished with a SAME-ORIGIN relative redirect
   ("/?oauth=..."), so the browser landed on the API host and the SPA never
   received the freshly minted session token. ``absolute_redirect`` rewrites
   that target onto the configured console origin (MIE_FRONTEND_ORIGIN).
2. The zero-dependency stdlib server (``backend.webapp``) emitted no CORS
   headers and did not answer OPTIONS, so a separately hosted console could
   not call it at all.

These tests pin both the helper contract and the real over-the-wire behavior
(FastAPI Location header; stdlib response headers + preflight).
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from backend import routes
from backend.contracts import success_body
from backend.webapp import DEFAULT_STATIC_ROOT, create_server
from database.service import PersistenceService

PROD_ORIGIN = "https://music-intelligence-engine-five.vercel.app"
LOCAL_ORIGIN = "http://localhost:5173"
ACAO = "access-control-allow-origin"


# ---------------------------------------------------------------------------
# Helper contract (backend.routes)
# ---------------------------------------------------------------------------

class TestFrontendOriginHelpers(unittest.TestCase):
    def test_frontend_origin_unset_is_empty(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MIE_FRONTEND_ORIGIN", None)
            self.assertEqual(routes.frontend_origin(), "")

    def test_frontend_origin_strips_slash_query_and_fragment(self):
        with mock.patch.dict(
                os.environ,
                {"MIE_FRONTEND_ORIGIN": PROD_ORIGIN + "/?a=b#c"},
                clear=False):
            self.assertEqual(routes.frontend_origin(), PROD_ORIGIN)

    def test_absolute_redirect_unset_keeps_relative(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MIE_FRONTEND_ORIGIN", None)
            self.assertEqual(
                routes.absolute_redirect("/?oauth=connected&auth=t"),
                "/?oauth=connected&auth=t")

    def test_absolute_redirect_prefixes_console_origin(self):
        with mock.patch.dict(
                os.environ, {"MIE_FRONTEND_ORIGIN": PROD_ORIGIN},
                clear=False):
            self.assertEqual(
                routes.absolute_redirect("/?oauth=connected&auth=t"),
                PROD_ORIGIN + "/?oauth=connected&auth=t")

    def test_absolute_redirect_passes_non_relative_targets(self):
        with mock.patch.dict(
                os.environ, {"MIE_FRONTEND_ORIGIN": PROD_ORIGIN},
                clear=False):
            self.assertIsNone(routes.absolute_redirect(None))
            # protocol-relative and absolute URLs are never rewritten
            self.assertEqual(routes.absolute_redirect("//evil.example/x"),
                             "//evil.example/x")
            self.assertEqual(routes.absolute_redirect("https://x.example/y"),
                             "https://x.example/y")

    def test_console_origins_default_and_extra_deduped(self):
        with mock.patch.dict(
                os.environ,
                {"MIE_CORS_ORIGINS": f"{LOCAL_ORIGIN}/, {PROD_ORIGIN}"},
                clear=False):
            origins = routes.console_origins()
        self.assertEqual(origins, [PROD_ORIGIN, LOCAL_ORIGIN])


# ---------------------------------------------------------------------------
# FastAPI callback wiring (Location header)
# ---------------------------------------------------------------------------

class TestFastApiCallbackRedirect(unittest.TestCase):
    def _client(self):
        from fastapi.testclient import TestClient
        from backend.app import create_app
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        storage = PersistenceService(os.path.join(self._tmp.name, "db.sqlite"))
        self.addCleanup(storage.close)
        return TestClient(create_app(storage), raise_server_exceptions=False)

    def test_callback_location_uses_configured_console_origin(self):
        client = self._client()
        body = success_body({"redirect": "/?oauth=connected&auth=tok"})
        with mock.patch.dict(
                os.environ, {"MIE_FRONTEND_ORIGIN": PROD_ORIGIN}, clear=False), \
                mock.patch.object(routes, "_finish_google_callback",
                                  return_value=(200, body)):
            resp = client.get(
                "/api/v1/auth/google/callback?code=c&state=s",
                follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"],
                         PROD_ORIGIN + "/?oauth=connected&auth=tok")

    def test_callback_stays_relative_without_configured_origin(self):
        client = self._client()
        body = success_body({"redirect": "/?oauth=connected&auth=tok"})
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(routes, "_finish_google_callback",
                                  return_value=(200, body)):
            os.environ.pop("MIE_FRONTEND_ORIGIN", None)
            resp = client.get(
                "/api/v1/auth/google/callback?code=c&state=s",
                follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/?oauth=connected&auth=tok")


# ---------------------------------------------------------------------------
# Stdlib server CORS + preflight (live)
# ---------------------------------------------------------------------------

class TestWebappCrossOriginLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        storage = PersistenceService(
            os.path.join(cls._tmp.name, "db.sqlite"))
        cls.server = create_server(storage, "127.0.0.1", 0,
                                   static_root=DEFAULT_STATIC_ROOT)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.addClassCleanup(storage.close)
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)

    def request(self, path, method="GET", headers=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, {
                    k.lower(): v for k, v in response.headers.items()}
        except urllib.error.HTTPError as error:
            return error.code, {
                k.lower(): v for k, v in error.headers.items()}

    def test_health_allows_production_console_origin(self):
        status, headers = self.request(
            "/api/v1/health", headers={"Origin": PROD_ORIGIN})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get(ACAO), PROD_ORIGIN)

    def test_preflight_answers_204_with_methods_and_headers(self):
        status, headers = self.request(
            "/api/v1/auth/google/start", method="OPTIONS",
            headers={
                "Origin": PROD_ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            })
        self.assertEqual(status, 204)
        self.assertEqual(headers.get(ACAO), PROD_ORIGIN)
        self.assertIn("PATCH", headers.get("access-control-allow-methods", ""))
        self.assertIn("authorization",
                      headers.get("access-control-allow-headers", "").lower())

    def test_unknown_origin_gets_no_cors_header(self):
        status, headers = self.request(
            "/api/v1/health", headers={"Origin": "https://evil.example.com"})
        self.assertEqual(status, 200)
        self.assertIsNone(headers.get(ACAO))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
