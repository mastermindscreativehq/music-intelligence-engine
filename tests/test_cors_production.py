"""Production CORS regression tests (offline).

The console (Vercel) and the API (Railway) are different origins, so every
browser call is cross-origin. These tests inspect the ACTUAL response headers
CORSMiddleware emits for the exact production Vercel origin — normal requests,
OPTIONS preflight (including the `Authorization` header the console sends),
and error responses — rather than trusting that a configuration variable
exists.

Also pins the middleware ordering requirement: the error envelopes the app
returns (404 route_not_found, 404 track_not_found, 400 bad_request, ...) still
carry `Access-Control-Allow-Origin`, so the browser surfaces the real API
error instead of an opaque CORS failure.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from backend.app import create_app
from auth.service import AuthService
from database.service import PersistenceService

PROD_ORIGIN = "https://music-intelligence-engine-five.vercel.app"
LOCAL_ORIGIN = "http://localhost:5173"
ACAO = "access-control-allow-origin"


class CorsCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.storage = PersistenceService(
            os.path.join(cls._tmp.name, "db.sqlite"))
        # The library routes are personal, so these clients are signed in
        # exactly like the console (the error envelope they assert on
        # therefore comes from the handler, not the 401 gate).
        auth = AuthService(cls.storage)
        operator = auth.create_user("cors-probe@example.com")
        cls.session_headers = {
            "Authorization": "Bearer " + auth.create_session(
                operator["user_id"])["session_token"],
        }
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.addClassCleanup(cls.storage.close)

    def client(self, *, extra_origins=None):
        """Build an app the way a fresh deploy would (env read at build)."""
        env = {}
        if extra_origins is not None:
            env["MIE_CORS_ORIGINS"] = extra_origins
        with mock.patch.dict(os.environ, env, clear=False):
            if extra_origins is None:
                os.environ.pop("MIE_CORS_ORIGINS", None)
            app = create_app(self.storage)
        return TestClient(app, raise_server_exceptions=False,
                          headers=dict(self.session_headers))


class TestDefaultProductionOrigin(CorsCase):
    def test_simple_get_is_allowed_for_the_production_origin(self):
        client = self.client()
        response = client.get("/api/v1/health", headers={"Origin": PROD_ORIGIN})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get(ACAO), PROD_ORIGIN)

    def test_preflight_allows_authorization_header(self):
        client = self.client()
        response = client.options(
            "/api/v1/tracks",
            headers={
                "Origin": PROD_ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        self.assertIn(response.status_code, (200, 204))
        self.assertEqual(response.headers.get(ACAO), PROD_ORIGIN)
        allowed_headers = response.headers.get(
            "access-control-allow-headers", "").lower()
        self.assertIn("authorization", allowed_headers)

    def test_preflight_allows_patch_used_by_release_editing(self):
        client = self.client()
        response = client.options(
            "/api/v1/tracks/sha256:" + "a" * 64,
            headers={
                "Origin": PROD_ORIGIN,
                "Access-Control-Request-Method": "PATCH",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        self.assertIn(response.status_code, (200, 204))
        methods = response.headers.get(
            "access-control-allow-methods", "")
        self.assertIn("PATCH", methods)

    def test_google_start_route_preflight_is_allowed(self):
        # The SPA fetches /auth/google/start and /auth/google/connect.
        client = self.client()
        response = client.options(
            "/api/v1/auth/google/start",
            headers={
                "Origin": PROD_ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        self.assertIn(response.status_code, (200, 204))
        self.assertEqual(response.headers.get(ACAO), PROD_ORIGIN)

    def test_error_response_still_carries_cors_headers(self):
        client = self.client()
        # 404 route_not_found envelope
        response = client.get("/api/v1/does-not-exist",
                              headers={"Origin": PROD_ORIGIN})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.headers.get(ACAO), PROD_ORIGIN)
        self.assertFalse(response.json()["ok"])

    def test_track_not_found_envelope_carries_cors_headers(self):
        client = self.client()
        response = client.get(
            "/api/v1/tracks/sha256:" + "0" * 64,
            headers={"Origin": PROD_ORIGIN})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.headers.get(ACAO), PROD_ORIGIN)
        self.assertEqual(response.json()["error"]["code"], "track_not_found")

    def test_unauthorized_envelope_carries_cors_headers(self):
        # The personal-data 401 must be readable by the console too, so the
        # browser shows "please sign in" instead of an opaque CORS failure.
        app = self.client().app
        anon = TestClient(app, raise_server_exceptions=False)
        response = anon.get("/api/v1/tracks",
                            headers={"Origin": PROD_ORIGIN})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers.get(ACAO), PROD_ORIGIN)
        self.assertEqual(response.json()["error"]["code"], "unauthorized")

    def test_unknown_origin_is_not_allowed(self):
        client = self.client()
        response = client.get("/api/v1/health",
                              headers={"Origin": "https://evil.example.com"})
        self.assertIsNone(response.headers.get(ACAO))


class TestExtraOrigins(CorsCase):
    def test_env_origin_is_added_alongside_the_default(self):
        client = self.client(extra_origins=LOCAL_ORIGIN)
        for origin in (PROD_ORIGIN, LOCAL_ORIGIN):
            response = client.get("/api/v1/health", headers={"Origin": origin})
            self.assertEqual(response.headers.get(ACAO), origin)

    def test_trailing_slash_in_env_is_normalized(self):
        client = self.client(extra_origins=LOCAL_ORIGIN + "/")
        response = client.get("/api/v1/health",
                              headers={"Origin": LOCAL_ORIGIN})
        self.assertEqual(response.headers.get(ACAO), LOCAL_ORIGIN)


class TestUnhandledErrorCors(CorsCase):
    """A crash must not look like a CORS failure to the browser."""

    def test_unhandled_exception_keeps_cors_headers(self):
        client = self.client()
        with mock.patch.object(
                __import__("backend.app", fromlist=["submission_service"])
                .submission_service, "list_tracks",
                side_effect=RuntimeError("boom")):
            response = client.get("/api/v1/tracks",
                                  headers={"Origin": PROD_ORIGIN})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.headers.get(ACAO), PROD_ORIGIN)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
