"""Phase 7 tests: operator console (frontend) over the REAL API.

No JavaScript runtime exists in this environment, so behavior is pinned
from Python:

- static asset integrity + strict security scans of the shipped assets;
- a coupling test proving every API path referenced from the frontend JS
  exists in the actually-served route table (backend.routes) with only
  supported query parameters;
- live single-origin integration against backend.webapp (static serving,
  traversal rejection, envelope round-trips incl. verification history).

There are no mocks anywhere: integration classes run the real dispatcher
against a seeded real SQLite storage.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from backend.routes import LIST_PARAMS, ROUTE_TABLE, dispatch
from backend.webapp import DEFAULT_STATIC_ROOT, create_server
from database.schema import SCHEMA_VERSION
from database.service import (
    PersistenceService,
    normalize_intelligence_record,
)
from tests.test_phase6_database_api import conflict_verification_report, kzow

FRONTEND = DEFAULT_STATIC_ROOT
JS_FILES = sorted(FRONTEND.rglob("*.js"))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Shipped assets: integrity + security posture
# ---------------------------------------------------------------------------

class TestStaticAssets(unittest.TestCase):
    def test_required_files_exist(self):
        for relative in ("index.html", "css/app.css", "js/dom.js",
                         "js/api.js", "js/basket.js", "js/router.js",
                         "js/views/list.js", "js/views/station.js",
                         "js/app.js"):
            self.assertTrue((FRONTEND / relative).is_file(), relative)

    def test_index_references_only_local_assets(self):
        html = _read(FRONTEND / "index.html")
        self.assertIn('http-equiv="Content-Security-Policy"', html)
        self.assertIn("default-src 'self'", html)
        for match in re.findall(r'(?:src|href)="([^"]+)"', html):
            local = match.startswith("/") or match.startswith("#")
            self.assertTrue(local,
                            f"non-local asset reference: {match}")
            self.assertFalse(match.startswith("//"), match)

    def test_no_remote_urls_in_any_asset(self):
        for path in [*JS_FILES, FRONTEND / "css/app.css",
                     FRONTEND / "index.html"]:
            content = _read(path)
            for pattern in (r"https?://", r'="//'):
                self.assertIsNone(re.search(pattern, content),
                                  f"{path.name} references a remote URL")

    def test_module_script_is_loaded(self):
        html = _read(FRONTEND / "index.html")
        self.assertIn('<script type="module" src="/js/app.js">', html)


class TestJsSecuritySurface(unittest.TestCase):
    def test_no_html_injection_or_code_eval_vectors(self):
        forbidden = [
            r"\beval\s*\(",
            r"\bnew\s+Function\b",
            r"\bdocument\.write\b",
            r"\.innerHTML\b",
            r"insertAdjacentHTML",
            r"\bdocument\.createElement\(\s*[\"']script",
        ]
        for path in JS_FILES:
            content = _read(path)
            for pattern in forbidden:
                self.assertIsNone(
                    re.search(pattern, content),
                    f"{path.name} uses forbidden vector {pattern!r}")

    def test_html_has_no_inline_event_handlers(self):
        html = _read(FRONTEND / "index.html")
        self.assertIsNone(re.search(r"\son[a-z]+\s*=", html, re.IGNORECASE),
                          "inline event handler found in index.html")

    def test_dom_builder_is_the_only_creation_path(self):
        dom = _read(FRONTEND / "js" / "dom.js")
        self.assertIn("export function el(", dom)
        # every view imports the builder rather than touching document directly
        for path in [*JS_FILES]:
            if path.name == "dom.js":
                continue
            content = _read(path)
            if "document.createElement" in content:
                self.fail(f"{path.name} creates elements directly")


# ---------------------------------------------------------------------------
# Frontend <-> backend contract coupling (consumes the REAL route table)
# ---------------------------------------------------------------------------

def _route_regex(template: str) -> re.Pattern:
    pattern = re.escape(template)
    pattern = pattern.replace(re.escape("{key}"), "[^/]+")
    pattern = pattern.replace(re.escape("{run_id}"), "[^/]+")
    return re.compile("^" + pattern + "$")


class TestApiContractCoupling(unittest.TestCase):
    def _referenced_paths(self) -> set[str]:
        referenced = set()
        finder = re.compile(r"/api/v1/[A-Za-z0-9_/.\-`${}()]*")
        prefix = len("/api/v1/")
        for path in JS_FILES:
            for raw in finder.findall(_read(path)):
                normalized = re.sub(r"\$\{[^}]*\}", "{param}", raw)
                # skip bare-prefix artifacts from prose comments
                if len(normalized) <= prefix:
                    continue
                # drop template-literal terminators captured at the tail
                referenced.add(normalized.rstrip("`()").rstrip("."))
        return referenced

    def test_every_referenced_api_path_is_served(self):
        served = [_route_regex(template) for _, _, template, _ in ROUTE_TABLE]
        referenced = self._referenced_paths()
        # health + stations list + detail/intelligence/contacts/verification;
        # ingest & runs are API-only surface, intentionally not called by UI.
        self.assertGreaterEqual(len(referenced), 6)
        for path in referenced:
            concrete = path.replace("{param}", "domain:x.example")
            self.assertTrue(
                any(pattern.match(concrete) for pattern in served),
                f"frontend calls unserved route: {path}")

    def test_filter_parameters_are_backend_supported(self):
        list_js = _read(FRONTEND / "js" / "views" / "list.js")
        used = set(re.findall(
            r"[\"'](q|status|genre|format|country|min_confidence|"
            r"limit|offset)[\"']", list_js))
        self.assertLessEqual(used - set(LIST_PARAMS), set(),
                             "unsupported listing filters crept in")

    def test_envelope_client_unwraps_contract(self):
        api_js = _read(FRONTEND / "js" / "api.js")
        self.assertIn("envelope.ok", api_js)
        self.assertIn("error.code", api_js.replace("detail.code",
                                                   "error.code"))
        self.assertIn("ApiError", api_js)


# ---------------------------------------------------------------------------
# Shared dispatcher (backend.routes) unit checks
# ---------------------------------------------------------------------------

class TestRoutesDispatcher(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = PersistenceService(
            os.path.join(self._tmp.name, "db.sqlite"))
        record = kzow()
        self.storage.ingest_intelligence([record], source="test")
        self.storage.persist_verification([record],
                                          conflict_verification_report(),
                                          source="test")
        self.key = normalize_intelligence_record(record)[1]

    def tearDown(self):
        self.storage.close()
        self._tmp.cleanup()

    def test_health_reports_schema_version(self):
        status, body = dispatch(self.storage, "GET", "/api/v1/health", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["schema_version"], SCHEMA_VERSION)

    def test_listing_supports_phase6_filters(self):
        status, body = dispatch(self.storage, "GET", "/api/v1/stations",
                                {"genre": ["news"]})
        self.assertEqual(body["data"]["total"], 1)
        _, body = dispatch(self.storage, "GET", "/api/v1/stations",
                           {"genre": ["jazz"]})
        self.assertEqual(body["data"]["total"], 0)

    def test_min_confidence_validated(self):
        status, body = dispatch(self.storage, "GET", "/api/v1/stations",
                                {"min_confidence": ["1.5"]})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "bad_request")

    def test_unknown_station_and_wrong_verb_codes(self):
        status, body = dispatch(self.storage, "GET",
                                "/api/v1/stations/domain:nope.example", {})
        self.assertEqual((status, body["error"]["code"]),
                         (404, "station_not_found"))
        status, body = dispatch(self.storage, "POST", "/api/v1/stations", {})
        self.assertEqual((status, body["error"]["code"]),
                         (405, "method_not_allowed"))

    def test_verification_roundtrip_through_dispatcher(self):
        status, body = dispatch(self.storage, "GET",
                                f"/api/v1/stations/{self.key}/verification",
                                {})
        self.assertEqual(status, 200)
        statuses = {r["status"]
                    for r in body["data"]["verification"]["results"]}
        self.assertEqual(statuses, {"conflicting", "unsupported"})

    def test_ingest_happy_isolation_and_bad_bodies(self):
        payload = ('{"records": [{"name": "QWER", '
                   '"website": "https://qwer.example/", "junk": 42}, 17],'
                   ' "source": "phase7"}').encode()
        status, body = dispatch(self.storage, "POST", "/api/v1/ingest", {},
                                payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["records_accepted"], 1)
        self.assertEqual(len(body["data"]["failures"]), 1)

        status, body = dispatch(self.storage, "POST", "/api/v1/ingest", {},
                                b"{broken")
        self.assertEqual((status, body["error"]["code"]),
                         (400, "bad_request"))
        status, body = dispatch(self.storage, "POST", "/api/v1/ingest", {},
                                b'{"records": "not-a-list"}')
        self.assertEqual(status, 400)

    def test_run_not_found(self):
        status, body = dispatch(self.storage, "GET", "/api/v1/runs/nope", {})
        self.assertEqual((status, body["error"]["code"]),
                         (404, "run_not_found"))


# ---------------------------------------------------------------------------
# Live single-origin operator server (static + API together)
# ---------------------------------------------------------------------------

class TestWebappLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        storage = PersistenceService(
            os.path.join(cls._tmp.name, "db.sqlite"))
        record = kzow()
        storage.ingest_intelligence([record], source="live")
        storage.persist_verification([record],
                                     conflict_verification_report(),
                                     source="live")
        cls.key = normalize_intelligence_record(record)[1]
        cls.server = create_server(storage, "127.0.0.1", 0,
                                   static_root=FRONTEND)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        # addClassCleanup runs LIFO: shutdown -> server_close -> db close
        # -> temp removal, so the sqlite file is never deleted while open.
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.addClassCleanup(storage.close)
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)

    def request(self, path, method="GET", data=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url, data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return (response.status, dict(response.headers),
                        response.read())
        except urllib.error.HTTPError as error:
            return (error.code, dict(error.headers), error.read())

    def get_json(self, path):
        status, headers, body = self.request(path)
        import json
        return status, json.loads(body.decode("utf-8"))

    def test_index_served_with_security_headers(self):
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertIn(b'Content-Security-Policy', body)
        self.assertIn(b'id="view"', body)

    def test_static_assets_served_with_types(self):
        expectations = {
            "/css/app.css": "text/css",
            "/js/app.js": "application/javascript",
            "/js/views/station.js": "application/javascript",
            "/js/api.js": "application/javascript",
        }
        for path, expected_type in expectations.items():
            status, headers, _ = self.request(path)
            self.assertEqual(status, 200, msg=path)
            self.assertTrue(headers["Content-Type"].startswith(expected_type),
                            msg=path)

    def test_missing_static_returns_plain_404(self):
        status, headers, _ = self.request("/definitely-missing.png")
        self.assertEqual(status, 404)

    def test_path_traversal_rejected(self):
        for evil in ("/../backend/service.py", "/..%2fbackend%2fservice.py",
                     "/js/%2e%2e/%2e%2e/backend/schema.py", "/\\.\\secret"):
            status, _, body = self.request(evil)
            self.assertIn(status, (403, 404), msg=evil)
            self.assertNotIn(b"SCHEMA_VERSION", body, msg=evil)

    def test_same_origin_api_listing_with_filters(self):
        status, body = self.get_json("/api/v1/stations?genre=news")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertGreaterEqual(body["data"]["total"], 1)

    def test_same_origin_api_verification_roundtrip(self):
        status, body = self.get_json(
            f"/api/v1/stations/{self.key}/verification")
        self.assertEqual(status, 200)
        statuses = {r["status"]
                    for r in body["data"]["verification"]["results"]}
        self.assertEqual(statuses, {"conflicting", "unsupported"})

    def test_same_origin_api_ingest_post(self):
        payload = ('{"records": [{"name": "WEBAPP LIVE",'
                   '"website": "https://webapplive.example/"}],'
                   '"source": "phase7-live"}').encode()
        status, headers, raw = self.request(
            "/api/v1/ingest", method="POST", data=payload)
        self.assertEqual(status, 200)
        import json
        body = json.loads(raw.decode("utf-8"))
        self.assertEqual(body["data"]["records_accepted"], 1)
        run_id = body["data"]["run_id"]
        status, run_body = self.get_json(f"/api/v1/runs/{run_id}")
        self.assertEqual(run_body["data"]["run_id"], run_id)

    def test_api_cache_control(self):
        _, headers, _ = self.request("/api/v1/stations")
        self.assertEqual(headers.get("Cache-Control"), "no-store")


if __name__ == "__main__":
    unittest.main()
