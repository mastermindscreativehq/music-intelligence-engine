"""Phase 7 focused console-completion tests (offline, no network).

Covers the personal-workspace work end to end at the seams that matter:

- AUTHENTICATION GATES: every personal-data route (releases, outreach,
  opportunities, email providers) fails closed with the ``401 unauthorized``
  envelope when no valid bearer session is presented, on BOTH surfaces (the
  stdlib dispatcher backend.routes and the FastAPI backend.app). Public
  routes (health, stations, DJs) stay open.
- HOMEPAGE / landing: the console ships a real signed-out landing view and
  the auth-aware shell (index.html data-auth + nav-hidden, gate.js,
  app.js syncChrome).
- PROFILE / MY MUSIC / COMPOSER wiring: the profile page reads its services
  from the provider endpoint, My Music keeps its release-creation and
  listening-URL editor paths, and the outreach composer auto-fills the
  SELECTED release's saved link (never a fabricated one).
- PROVIDER UI: GET /email-providers only ever lists providers this backend
  can really send through, with honest capability flags and connection
  state, so nothing unsupported can render as connected or available.
- GMAIL SEND PATH: the send route and the Gmail provider contract stay
  wired exactly as the sending tests expect.

No real email is transmitted; no Google call is made.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from auth.service import AuthService
from backend.app import create_app
from backend.routes import ROUTE_TABLE, dispatch
from database.service import PersistenceService
from gmail_provider import GMAIL_SEND_SCOPE, GmailProvider, GmailSendError
from outreach.providers import (
    CAN_ATTACH,
    CAN_ERRORS,
    CAN_SEND,
    CAN_STATUS,
    CAPABILITY_ORDER,
    provider_descriptors,
)

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"

TRACK_ID = "sha256:" + "0" * 64
OUTREACH_ID = "om_deadbeef"


def _session_headers(auth: AuthService, email: str):
    user = auth.create_user(email)
    token = auth.create_session(user["user_id"])["session_token"]
    return user, {"authorization": f"Bearer {token}"}


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.svc = PersistenceService(
            os.path.join(self._tmp.name, "db.sqlite"))
        self.addCleanup(self.svc.close)
        self.auth = AuthService(self.svc)
        self.user, self.headers = _session_headers(self.auth, "op@example.com")


class TestPersonalRouteEnforcement(_Base):
    """No session token -> 401 on every personal-data route (fail-closed)."""

    PERSONAL = (
        ("GET", "/api/v1/tracks", {}, None),
        ("POST", "/api/v1/tracks", {}, b"\xff\xfb"),
        ("POST", "/api/v1/tracks/releases", {},
         json.dumps({"title": "T", "artist_name": "A",
                     "music_url": "https://x.example/m"}).encode()),
        ("GET", f"/api/v1/tracks/{TRACK_ID}", {}, None),
        ("PATCH", f"/api/v1/tracks/{TRACK_ID}", {},
         json.dumps({"title": "New"}).encode()),
        ("DELETE", f"/api/v1/tracks/{TRACK_ID}", {}, None),
        ("GET", "/api/v1/outreach", {}, None),
        ("POST", "/api/v1/outreach", {},
         json.dumps({"recipient": {"email": "a@b.c"}}).encode()),
        ("GET", f"/api/v1/outreach/{OUTREACH_ID}", {}, None),
        ("DELETE", f"/api/v1/outreach/{OUTREACH_ID}", {}, None),
        ("POST", f"/api/v1/outreach/{OUTREACH_ID}/event", {},
         json.dumps({"event": "opened_in_email"}).encode()),
        ("GET", "/api/v1/opportunities", {}, None),
        ("GET", "/api/v1/email-providers", {}, None),
    )

    def test_every_personal_route_answers_401_without_a_session(self):
        for method, path, params, body in self.PERSONAL:
            for headers in ({}, {"authorization": "Bearer forged-token"}):
                with self.subTest(method=method, path=path, headers=headers):
                    status, envelope = dispatch(
                        self.svc, method, path, params, body,
                        headers=headers)
                    self.assertEqual(status, 401)
                    self.assertEqual(envelope["error"]["code"], "unauthorized")

    def test_public_routes_stay_open(self):
        for method, path in (("GET", "/api/v1/health"),
                             ("GET", "/api/v1/stations"),
                             ("GET", "/api/v1/djs"),
                             ("GET", "/api/v1/plan")):
            with self.subTest(path=path):
                status, _envelope = dispatch(self.svc, method, path, {})
                self.assertNotEqual(status, 401)

    def test_signed_in_session_reaches_the_handler(self):
        status, envelope = dispatch(
            self.svc, "GET", "/api/v1/email-providers", {},
            headers=self.headers)
        self.assertEqual(status, 200)
        self.assertTrue(envelope["ok"])
        # A missing required parameter now fails as validation (400), which
        # proves the gate passed — not that the route is unreachable.
        status, envelope = dispatch(
            self.svc, "GET", "/api/v1/opportunities", {}, headers=self.headers)
        self.assertEqual((status, envelope["error"]["code"]),
                         (400, "bad_request"))


class TestFastAPIGateParity(_Base):
    """The production FastAPI surface enforces the same personal gates."""

    def setUp(self):
        super().setUp()
        self.client = TestClient(
            create_app(self.svc), raise_server_exceptions=False)
        self.anon = TestClient(
            self.client.app, raise_server_exceptions=False)
        token = self.headers["authorization"].split(" ", 1)[1]
        self.bearer = {"Authorization": f"Bearer {token}"}

    def test_anonymous_is_401_on_personal_routes(self):
        for path in ("/api/v1/tracks",
                     f"/api/v1/tracks/{TRACK_ID}",
                     "/api/v1/outreach",
                     f"/api/v1/outreach/{OUTREACH_ID}",
                     "/api/v1/opportunities",
                     "/api/v1/email-providers"):
            with self.subTest(path=path):
                response = self.anon.get(path)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["error"]["code"],
                                 "unauthorized")

    def test_session_passes_the_gate_on_both_surfaces(self):
        # Same token, same answers from dispatch and FastAPI for the
        # provider endpoint (the console's Connected-services source).
        status, envelope = dispatch(
            self.svc, "GET", "/api/v1/email-providers", {},
            headers=self.headers)
        response = self.client.get("/api/v1/email-providers",
                                   headers=self.bearer)
        self.assertEqual(response.status_code, status)
        self.assertEqual(response.json()["data"],
                         envelope["data"])


class TestProviderContract(_Base):
    """GET /email-providers lists only what this backend can really do."""

    def _providers(self, headers=None):
        status, envelope = dispatch(
            self.svc, "GET", "/api/v1/email-providers", {},
            headers=headers if headers is not None else self.headers)
        self.assertEqual(status, 200)
        return envelope["data"]["providers"]

    def test_only_real_providers_are_offered(self):
        ids = [p["id"] for p in self._providers()]
        self.assertEqual(ids, ["gmail", "mailto"])
        # The non-sending local stub must never be offered to the console.
        self.assertNotIn("local", ids)

    def test_gmail_entry_reports_honest_capabilities_and_paths(self):
        gmail = self._providers()[0]
        self.assertEqual(gmail["label"], "Gmail")
        self.assertTrue(gmail["auth_required"])
        self.assertFalse(gmail["connected"])
        self.assertIsNone(gmail["email"])
        # The Gmail send path neither attaches files nor tracks delivery,
        # so those flags are False — a provider is never oversold.
        self.assertEqual(gmail["capabilities"], {
            CAN_SEND: True, CAN_ATTACH: False, CAN_STATUS: False,
            CAN_ERRORS: True})
        self.assertEqual(gmail["connect_path"],
                         "/api/v1/auth/google/connect")
        self.assertEqual(gmail["disconnect_path"],
                         "/api/v1/auth/google/disconnect")

    def test_mailto_entry_is_not_a_connection(self):
        mailto = self._providers()[1]
        self.assertEqual(mailto["id"], "mailto")
        self.assertFalse(mailto["auth_required"])
        # Not a connectable mailbox: nothing to connect, nothing to show
        # as connected.
        self.assertIsNone(mailto["connected"])
        self.assertIsNone(mailto["connect_path"])
        self.assertIsNone(mailto["disconnect_path"])
        self.assertFalse(mailto["capabilities"][CAN_ATTACH])

    def test_connected_state_follows_the_stored_mailbox(self):
        self.svc.auth_save_google_account({
            "user_id": self.user["user_id"],
            "google_id": "gid-1",
            "email": "op@gmail.example",
            "picture": None,
            "access_token": "at",
            "refresh_token": "rt",
            "token_expiry": "2030-01-01T00:00:00+00:00",
            "scopes": f"openid,{GMAIL_SEND_SCOPE}",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        })
        gmail = self._providers()[0]
        self.assertTrue(gmail["connected"])
        self.assertEqual(gmail["email"], "op@gmail.example")
        self.assertIn(GMAIL_SEND_SCOPE, gmail["scopes"])

    def test_state_is_per_operator(self):
        self.svc.auth_save_google_account({
            "user_id": self.user["user_id"],
            "google_id": "gid-1",
            "email": "op@gmail.example",
            "picture": None,
            "access_token": "at",
            "refresh_token": "rt",
            "token_expiry": "2030-01-01T00:00:00+00:00",
            "scopes": GMAIL_SEND_SCOPE,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        })
        _other, other_headers = _session_headers(self.auth, "b@example.com")
        gmail = self._providers(other_headers)[0]
        self.assertFalse(gmail["connected"])
        self.assertIsNone(gmail["email"])

    def test_descriptors_always_cover_every_capability_key(self):
        for provider in provider_descriptors(None):
            self.assertEqual(set(provider["capabilities"]),
                             set(CAPABILITY_ORDER))

    def test_fastapi_serves_the_same_list(self):
        client = TestClient(create_app(self.svc),
                            raise_server_exceptions=False)
        response = client.get("/api/v1/email-providers", headers={
            "Authorization": self.headers["authorization"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([p["id"] for p in response.json()["data"]["providers"]],
                         ["gmail", "mailto"])


class TestGmailSendPathContract(unittest.TestCase):
    """The Gmail send route and provider stay wired for the sending tests."""

    def test_send_route_is_registered_on_both_surfaces(self):
        verbs = {(method, template)
                 for method, _p, template, _q in ROUTE_TABLE}
        self.assertIn(("POST", "/api/v1/outreach/{outreach_id}/send"), verbs)
        app_py = (ROOT / "backend" / "app.py").read_text(encoding="utf-8")
        self.assertIn('@app.post("/api/v1/outreach/{outreach_id}/send")',
                      app_py)

    def test_provider_sends_from_the_operator_account_only(self):
        source = inspect_source(GmailProvider.send_email)
        # The mailbox is the signed-in operator's; nothing is hardwired.
        self.assertIn("user_id", source)
        self.assertNotIn("smtplib", inspect_source(GmailProvider))

    def test_typed_send_errors_are_unchanged(self):
        self.assertEqual(
            set(GmailSendError("x", code=c).code
                for c in ("no_connected_gmail", "gmail_reauth_required",
                          "gmail_scope_missing", "gmail_send_failed")),
            {"no_connected_gmail", "gmail_reauth_required",
             "gmail_scope_missing", "gmail_send_failed"})

    def test_capability_constant_matches_the_real_send_path(self):
        # The send call takes to/subject/body only, so Gmail cannot claim
        # attachments or delivery status anywhere in the console.
        import inspect
        params = set(inspect.signature(GmailProvider.send_email).parameters)
        self.assertEqual(params, {"self", "user_id", "to", "subject", "body"})
        gmail = provider_descriptors(None)[0]
        self.assertFalse(gmail["capabilities"][CAN_ATTACH])
        self.assertFalse(gmail["capabilities"][CAN_STATUS])


def inspect_source(func) -> str:
    import inspect
    return inspect.getsource(func)


class TestFrontendWorkspaceWiring(unittest.TestCase):
    """Homepage, gates, profile, My Music and the composer link picker."""

    def _read(self, relative: str) -> str:
        return (FRONTEND / relative).read_text(encoding="utf-8")

    # -- homepage + auth gate ------------------------------------------------

    def test_index_marks_the_shell_auth_aware(self):
        html = self._read("index.html")
        self.assertIn("data-auth", html)
        self.assertIn("nav-hidden", html)
        self.assertIn('<script type="module" src="/js/app.js">', html)

    def test_signed_out_landing_exists(self):
        dashboard = self._read("js/views/dashboard.js")
        self.assertIn("landingPage", dashboard)

    def test_gate_module_and_route_wiring_exist(self):
        gate = self._read("js/views/gate.js")
        self.assertIn("requiresAuth", gate)
        app_js = self._read("js/app.js")
        self.assertIn("requiresAuth", app_js)
        self.assertIn("syncChrome", app_js)
        self.assertIn("mie:auth-changed", app_js)
        for route in ("tracks", "opportunities", "outreach",
                      "outreachHistory"):
            self.assertIn(route, app_js)

    def test_sign_out_announces_the_auth_change(self):
        account = self._read("js/views/account.js")
        self.assertIn("mie:auth-changed", account)

    # -- profile + provider UI ----------------------------------------------

    def test_profile_renders_services_from_the_provider_endpoint(self):
        profile = self._read("js/views/profile.js")
        self.assertIn("api.emailProviders()", profile)
        self.assertIn("providerConnect", profile)
        self.assertIn("providerDisconnect", profile)
        # Provider-specific CTA text is gone: the section is data-driven.
        self.assertNotIn("Connect Gmail", profile)
        self.assertNotIn("Disconnect Gmail", profile)

    def test_api_client_exposes_the_provider_endpoints(self):
        api_js = self._read("js/api.js")
        self.assertIn('"/api/v1/email-providers"', api_js)
        self.assertIn("providerConnect:", api_js)
        self.assertIn("providerDisconnect:", api_js)
        # The console never points at a path the backend does not serve.
        self.assertIn("/api/v1/email-providers",
                      {template for _m, _p, template, _q in ROUTE_TABLE})

    def test_provider_section_is_honest_about_unsupported_work(self):
        profile = self._read("js/views/profile.js")
        self.assertIn("capabilities", profile)
        self.assertIn("No email providers are configured", profile)

    # -- My Music + release selection ----------------------------------------

    def test_my_music_keeps_release_creation_and_link_editing(self):
        api_js = self._read("js/api.js")
        for method in ("createRelease", "updateTrackListenUrl",
                       "updateTrackSongStory", "trackDetail"):
            self.assertIn(f"{method}:", api_js)
        tracks = self._read("js/views/tracks.js")
        self.assertIn("listen", tracks.lower())
        self.assertIn("Song story / creative brief", tracks)

    def test_composer_autofills_the_selected_releases_saved_link(self):
        composer = self._read("js/views/outreachModal.js")
        self.assertIn("applyReleaseLink", composer)
        self.assertIn("listen_url", composer)
        # Replacing an operator-typed link asks first; nothing is invented.
        self.assertIn("window.confirm", composer)

    def test_no_remote_urls_in_the_changed_console_assets(self):
        for relative in ("js/views/profile.js", "js/views/gate.js",
                         "js/views/dashboard.js", "js/api.js"):
            with self.subTest(asset=relative):
                content = self._read(relative)
                self.assertIsNone(re.search(r"https?://", content))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
