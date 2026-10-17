"""Static frontend/backend API-path agreement (offline, no network).

Guards against the frontend calling a path the backend does not serve (or a
route that drifted during a rename). It is deliberately static: it reads the
source of the three API surfaces and asserts the contract for the two
net-new endpoints added for frontend completeness, plus a spot-check that the
frontend never points at a bare ``/api/v1/profile`` (the real path is under
``/api/v1/auth/``).
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API_JS = ROOT / "frontend" / "js" / "api.js"
ROUTES_PY = ROOT / "backend" / "routes.py"
APP_PY = ROOT / "backend" / "app.py"


class TestApiPathAgreement(unittest.TestCase):
    def setUp(self):
        self.api = API_JS.read_text(encoding="utf-8")
        self.routes = ROUTES_PY.read_text(encoding="utf-8")
        self.app = APP_PY.read_text(encoding="utf-8")

    def test_frontend_calls_profile_and_plan_paths(self):
        self.assertIn('"/api/v1/auth/profile"', self.api)
        self.assertIn('"/api/v1/plan"', self.api)

    def test_frontend_never_uses_a_bare_profile_path(self):
        # The profile route lives under /auth/; a bare /api/v1/profile would
        # 404 in production.
        self.assertNotIn('"/api/v1/profile"', self.api)

    def test_stdlib_route_table_serves_profile_and_plan(self):
        self.assertRegex(
            self.routes,
            r'\("PATCH",\s*re\.compile\(r"\^/api/v1/auth/profile\$"\)')
        self.assertRegex(
            self.routes,
            r'\("GET",\s*re\.compile\(r"\^/api/v1/plan\$"\)')

    def test_fastapi_app_serves_profile_and_plan(self):
        self.assertIn('@app.patch("/api/v1/auth/profile")', self.app)
        self.assertIn('@app.get("/api/v1/plan")', self.app)

    def test_methods_match_between_surfaces(self):
        # PATCH for profile (a write), GET for plan (a read) - on both
        # surfaces. A method drift here would 405 in production.
        self.assertIn("updateProfile: (name)", self.api)
        self.assertRegex(self.api, r"updateProfile[\s\S]{0,160}PATCH")
        self.assertRegex(self.api, r"plan:\s*\(\)\s*=>\s*request")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
