"""FastAPI application over stored radio intelligence (Phases 6-8).

Primary API server per docs/architecture.md ("backend owns persistence
and business queries"). Serves the SAME envelope contract, routes, and
payload shapes as the Phase 4 stdlib server (``backend.api``), plus the
Phase 6-8 additions:

    GET  /api/v1/health
    GET  /api/v1/stations?limit&offset&q&status[&genre&format&country&min_confidence]
    GET  /api/v1/stations/{identity_key}
    GET  /api/v1/stations/{identity_key}/intelligence
    GET  /api/v1/stations/{identity_key}/contacts
    GET  /api/v1/stations/{identity_key}/verification      (Phase 6)
    POST /api/v1/ingest                                    (Phase 6)
    GET  /api/v1/runs/{run_id}                             (Phase 6)
    POST /api/v1/tracks                                    (Phase 8)
    POST /api/v1/tracks/releases                           (Phase 14)
    GET  /api/v1/tracks[?limit&offset&status]              (Phase 8)
    GET  /api/v1/tracks/{track_id}                         (Phase 8)
    PATCH /api/v1/tracks/{track_id}                        (Phase 12)
    GET  /api/v1/stations/{identity_key}/submission        (Phase 8)
    POST /api/v1/stations/{identity_key}/submission/checks (Phase 8)
    GET  /api/v1/stations/{identity_key}/submission/checks (Phase 8)

Envelope contract (unchanged):
    success -> {"ok": true,  "data": <payload>, "error": null}
    failure -> {"ok": false, "data": null,
                "error": {"code": <str>, "message": <str>}}

The storage backend is injected (``create_app(storage)``): SQLite
``PersistenceService`` offline/tests, ``PostgresStorage`` against a live
PostgreSQL/Supabase instance. Phase 8 submission dependencies
(``track_store``, ``link_fetcher``) are injectable the same way.
FastAPI/Starlette are imported at module level — Phases 1-5 suites simply
never import this module, so they never require the dependency.
Annotations must stay resolvable at module scope (PEP 563 + FastAPI
dependency resolution). Configuration uses env var NAMES only
(MIE_DATABASE_PATH / MIE_PG_DSN / MIE_API_HOST / MIE_API_PORT /
SUBMISSION_STORAGE_ROOT / MIE_CORS_ORIGINS / MIE_FRONTEND_ORIGIN);
values are never logged or echoed into
responses, and asset responses never contain storage locations. No
crawling logic, no secrets.

The ASGI entrypoint for hosting platforms (e.g. Railway) is
``backend.app:app`` (``uvicorn backend.app:app``). ``app`` is served
lazily via PEP 562 so importing only ``create_app``/``build_storage``
(tests, ``main``) never forces a database connection at import time;
construction happens exactly when uvicorn resolves the ``app`` attribute.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

_log = logging.getLogger("mie.backend")

# Load the PROJECT-ROOT .env before any code reads os.environ. python-dotenv
# only sets variables that are not already present (override=False), so a real
# platform var such as Railway's MIE_PG_DSN / DATABASE_URL always wins. The
# path is derived from this file (parents[1] == repo root), so it works even
# when the backend is started from another working directory.
from dotenv import load_dotenv

load_dotenv(
    Path(__file__).resolve().parents[1] / ".env",
    override=False,
    verbose=False,
)

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.contracts import (
    contacts_payload,
    dj_detail,
    dj_summary,
    error_body,
    intelligence_payload,
    station_detail,
    station_summary,
    submission_view,
    success_body,
    track_projection,
    tracks_payload,
    validate_override_payload,
)

from submissions import service as submission_service
from submissions.service import TrackRejected, TrackTooLarge

from outreach import service as outreach_service

from djs import service as dj_service

MAX_LIMIT = 200
DEFAULT_LIMIT = 50

# The console is hosted on Vercel, the API on Railway, so browser calls are
# cross-origin. The allowed origins (production default + MIE_CORS_ORIGINS)
# are shared with every server layer via ``backend.routes.console_origins`` so
# the FastAPI app, the reference server and the operator webapp agree.
from backend.routes import console_origins  # noqa: E402


class StationNotFound(Exception):
    """Unknown identity key; converted to a 404 envelope."""


class TrackNotFound(Exception):
    """Unknown opaque asset id; converted to a 404 envelope."""


class OutreachNotFound(Exception):
    """Unknown outreach id; converted to a 404 envelope."""


class DjNotFound(Exception):
    """Unknown DJ id; converted to a 404 envelope (independent DJs)."""


class CorsVisibleErrors:
    """Return the app's JSON 500 envelope for otherwise-unhandled crashes.

    Starlette installs ``ServerErrorMiddleware`` OUTSIDE all user middleware,
    so a truly unhandled exception would be turned into a bare 500 with no
    ``Access-Control-Allow-Origin`` header — the browser would then report an
    opaque CORS/network failure instead of the real server error. This
    middleware is added BEFORE ``CORSMiddleware`` (so CORS wraps it) and
    converts any exception escaping the router into the standard envelope, so
    even a crash keeps the cross-origin headers the console needs to read it.
    The traceback is logged server-side and never sent to the client.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def _send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except Exception:  # noqa: BLE001 — last-resort envelope, then re-log
            if started:
                raise
            _log.exception("unhandled error serving %s", scope.get("path"))
            response = JSONResponse(
                status_code=500,
                content=error_body("internal_error", "internal server error"))
            await response(scope, receive, send)


def create_app(storage, *, track_store=None, link_fetcher=None,
               allow_private=False, discover_fetcher=None):
    """Build the FastAPI application bound to *storage*.

    Imported lazily as a whole so importing ``backend.app`` stays optional
    for environments without FastAPI installed.
    """
    if track_store is None:
        track_store = submission_service.default_track_store()
    if link_fetcher is None:
        link_fetcher = submission_service.default_link_fetcher()
    from discovery.async_discovery import DiscoveryJobScheduler
    scheduler = DiscoveryJobScheduler(storage, fetcher=discover_fetcher)
    app = FastAPI(title="Music Intelligence Engine API",
                  version="0.8",
                  docs_url="/api/v1/docs", openapi_url="/api/v1/openapi.json")

    # Added FIRST so CORSMiddleware (added below) wraps it: an unhandled crash
    # becomes a JSON 500 envelope that still passes back through CORS.
    app.add_middleware(CorsVisibleErrors)
    # Cross-origin support for separately-hosted frontends (the static Vercel
    # console against this Railway backend). console_origins() yields the exact
    # production origin by default plus any MIE_CORS_ORIGINS. Credentials stay
    # off: the console authenticates with a bearer token in the Authorization
    # header (allowed below), not cross-site cookies. A cookie exchange would
    # require allow_credentials and could never use a wildcard.
    cors_origins = console_origins()
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "Accept", "Authorization"],
        )

    def _json(status: int, body: dict) -> JSONResponse:
        return JSONResponse(status_code=status, content=body)

    async def _json_request(request: Request) -> dict:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
        return payload

    @app.exception_handler(StationNotFound)
    async def _station_not_found(request: Request, exc: StationNotFound):
        return _json(404, error_body("station_not_found", str(exc)))

    @app.exception_handler(LookupError)
    async def _lookup_error(request: Request, exc: LookupError):
        # submissions.service signals unknown stations this way
        return _json(404, error_body("station_not_found", str(exc)))

    @app.exception_handler(TrackTooLarge)
    async def _too_large(request: Request, exc: TrackTooLarge):
        return _json(413, error_body("payload_too_large", str(exc)))

    @app.exception_handler(TrackRejected)
    async def _rejected(request: Request, exc: TrackRejected):
        return _json(422, error_body("track_rejected", str(exc)))

    @app.exception_handler(TrackNotFound)
    async def _track_not_found(request: Request, exc: TrackNotFound):
        return _json(404, error_body("track_not_found", str(exc)))

    @app.exception_handler(OutreachNotFound)
    async def _outreach_not_found(request: Request, exc: OutreachNotFound):
        return _json(404, error_body("outreach_not_found", str(exc)))

    @app.exception_handler(DjNotFound)
    async def _dj_not_found(request: Request, exc: DjNotFound):
        return _json(404, error_body("dj_not_found", str(exc)))

    async def _bad_value(request: Request, exc: Exception):
        # mirrors the stdlib dispatcher's contract-failure mapping
        # (e.g. submissions.service rejects unknown track statuses here);
        # registered per-class because Starlette handlers take single
        # exception classes only
        return _json(400, error_body("bad_request", str(exc)))

    # MRO resolution keeps the specific TrackTooLarge/TrackRejected
    # handlers winning over this generic ValueError mapping.
    app.add_exception_handler(ValueError, _bad_value)
    app.add_exception_handler(TypeError, _bad_value)

    @app.exception_handler(RequestValidationError)
    async def _bad_request(request: Request,
                           exc: RequestValidationError):
        return _json(400, error_body("bad_request",
                                     "invalid request parameters"))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request,
                          exc: StarletteHTTPException):
        if exc.status_code == 404:
            return _json(404, error_body("route_not_found",
                                         "no route for this path"))
        if exc.status_code == 405:
            return _json(405, error_body("method_not_allowed",
                                         "method not allowed on this route"))
        return _json(exc.status_code, error_body(
            "internal_error" if exc.status_code >= 500 else "bad_request",
            str(exc.detail)))

    def _require_station(key: str) -> dict:
        row = storage.get_station(key)
        if row is None:
            raise StationNotFound(f"unknown station {key!r}")
        return row

    def _get_current_user(request: Request):
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return None
        token = auth[7:].strip()
        from auth.service import AuthService
        svc = AuthService(storage)
        sess = svc.get_session(token)
        if not sess:
            return None
        return svc.get_user_by_id(sess["user_id"])

    @app.post("/api/v1/auth/login")
    async def auth_login(request: Request):
        payload = await _json_request(request)
        email = str(payload.get("email") or "").strip().lower()
        if not email:
            raise ValueError("email required")
        from auth.service import AuthService
        svc = AuthService(storage)
        user = svc.get_user_by_email(email) or svc.create_user(email)
        sess = svc.create_session(user["user_id"])
        return success_body({"user": {"user_id": user["user_id"], "email": user["email"], "name": user.get("name")}, "session": sess})

    @app.post("/api/v1/auth/logout")
    async def auth_logout(request: Request):
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            from auth.service import AuthService
            svc = AuthService(storage)
            svc.revoke_session(auth[7:].strip())
        return success_body({"ok": True})

    @app.get("/api/v1/auth/me")
    async def auth_me(request: Request):
        user = _get_current_user(request)
        if not user:
            return _json(401, error_body("unauthorized", "not logged in"))
        from gmail_provider import GmailProvider
        gp = GmailProvider(storage)
        account = gp.connected_account(user["user_id"])
        return success_body({
            "user": {"user_id": user["user_id"], "email": user["email"],
                     "name": user.get("name")},
            "gmail_connected": account is not None,
            "gmail_email": (account.get("email") or user.get("email"))
            if account else None,
        })

    @app.get("/api/v1/auth/google/start")
    async def google_start(request: Request):
        # Sign-in flow: identity only, no Gmail scopes. The callback mints the
        # operator's session after Google confirms the account email.
        from auth.google_oauth import GoogleOAuth
        oauth = GoogleOAuth()
        if not oauth.configured:
            return _json(503, error_body("oauth_not_configured",
                                         "Google OAuth not configured"))
        from auth.service import AuthService
        svc = AuthService(storage)
        verifier = oauth.new_code_verifier()
        state = svc.create_oauth_state(None, verifier)
        auth_url, _ = oauth.get_auth_url(
            state, verifier, GoogleOAuth.SIGNIN_SCOPES)
        return success_body({"auth_url": auth_url})

    @app.get("/api/v1/auth/google/connect")
    async def google_connect(request: Request):
        # Gmail-connect flow: an already signed-in operator grants the Gmail
        # SEND scope. Separate from sign-in so identity and mailbox access are
        # never bundled into one consent.
        user = _get_current_user(request)
        if not user:
            return _json(401, error_body(
                "unauthorized", "sign in before connecting Gmail"))
        from auth.google_oauth import GoogleOAuth
        oauth = GoogleOAuth()
        if not oauth.configured:
            return _json(503, error_body("oauth_not_configured",
                                         "Google OAuth not configured"))
        from auth.service import AuthService
        svc = AuthService(storage)
        verifier = oauth.new_code_verifier()
        state = svc.create_oauth_state(user["user_id"], verifier)
        auth_url, _ = oauth.get_auth_url(
            state, verifier, GoogleOAuth.GMAIL_SEND_SCOPES)
        return success_body({"auth_url": auth_url})

    @app.get("/api/v1/auth/google/callback")
    async def google_callback(code: str | None = None,
                              state: str | None = None,
                              error: str | None = None):
        if error:
            # Google redirects here with ?error=... when consent is denied
            # or the request is rejected; surface it instead of a cryptic
            # "code required".
            return _json(400, error_body(
                "oauth_denied",
                f"Google authorization was not completed: {error}"))
        if not state:
            return _json(400, error_body("bad_request", "state required"))
        if not code:
            return _json(400, error_body("bad_request", "code required"))
        from backend.routes import (
            _finish_google_callback, absolute_redirect, redirect_target)
        from auth.service import AuthService
        svc = AuthService(storage)
        status, body = _finish_google_callback(svc, storage, code, state)
        location = absolute_redirect(redirect_target(body))
        if location is not None:
            return RedirectResponse(url=location, status_code=303,
                                    headers={"Cache-Control": "no-store"})
        return _json(status, body)

    @app.post("/api/v1/auth/google/disconnect")
    async def google_disconnect(request: Request):
        # Disconnect Gmail without ending the session: the operator stays
        # signed in, but their stored Google credentials are removed so
        # outreach sends fail closed until they reconnect.
        user = _get_current_user(request)
        if not user:
            return _json(401, error_body("unauthorized", "not logged in"))
        from gmail_provider import GmailProvider
        gp = GmailProvider(storage)
        disconnected = gp.disconnect_account(user["user_id"])
        return success_body({
            "gmail_connected": False,
            "gmail_email": None,
            "disconnected": disconnected,
        })


    @app.get("/api/v1/health")
    def health():
        version = getattr(storage, "version", None)
        return success_body({"status": "ok", "schema_version": version})

    @app.get("/api/v1/stations")
    def list_stations(limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
                      offset: int = Query(0, ge=0),
                      q: str | None = None,
                      status: str | None = None,
                      genre: str | None = None,
                      format: str | None = None,
                      country: str | None = None,
                      min_confidence: float | None = Query(None, ge=0.0,
                                                           le=1.0)):
        if status is None:
            rows, total, dev_excluded, quarantined_excluded = storage.list_stations(
                limit=limit, offset=offset, q=q, status=None, genre=genre,
                format_filter=format, country=country,
                min_confidence=min_confidence, exclude_dev=True,
                exclude_quarantined=True)
        else:
            # A status filter refines the SAME normal listing and must never
            # re-admit dev fixtures or quarantined rows.
            rows, total, dev_excluded, quarantined_excluded = storage.list_stations(
                limit=limit, offset=offset, q=q, status=status, genre=genre,
                format_filter=format, country=country,
                min_confidence=min_confidence, exclude_dev=True,
                exclude_quarantined=True)
        return success_body({
            "stations": [station_summary(r) for r in rows],
            "total": total, "limit": limit, "offset": offset,
            "dev_fixtures_excluded": dev_excluded,
            "quarantined_excluded": quarantined_excluded,
        })

    @app.get("/api/v1/stations/{key}")
    def get_station(key: str):
        row = _require_station(key)
        return success_body(station_detail(row))

    @app.get("/api/v1/stations/{key}/intelligence")
    def get_intelligence(key: str):
        row = _require_station(key)
        return success_body(intelligence_payload(
            station=row,
            emails=storage.get_station_emails(key),
            phones=storage.get_station_phones(key),
            contacts=storage.get_station_contacts(key),
            submission=storage.get_submission(key),
            fetches=storage.get_fetches(key),
        ))

    @app.get("/api/v1/stations/{key}/contacts")
    def get_contacts(key: str):
        row = _require_station(key)
        return success_body(contacts_payload(
            station=row,
            contacts=storage.get_station_contacts(key),
            submission=storage.get_submission(key),
        ))

    # -- authenticated operator station edits --------------------------------
    #
    # The ONLY write endpoint over station field values. It never writes a
    # column directly: every change goes through
    # storage.set_station_overrides(), which records the operator correction
    # (with the displaced automated value snapshotted) and keeps re-ingestion
    # from undoing it. Validation is EXPLICIT here because the storage layer
    # silently drops non-overridable fields - a client typo must not come back
    # as a 200 that quietly did nothing.

    def _operator_auth_ok(request: Request) -> bool:
        """Fail-closed bearer gate for operator endpoints.

        Deliberately stricter than ``_check_automation_auth``: an automation
        token that is unset turns its gate OFF for zero-config local dev, which
        is acceptable for a scheduler trigger but NOT for a data-mutation
        endpoint. Here a missing or empty MIE_OPERATOR_TOKEN denies everything;
        there is no path that falls back to unauthenticated access. Comparison
        is constant-time and the token is never logged or echoed.
        """
        expected = os.environ.get("MIE_OPERATOR_TOKEN", "").strip()
        if not expected:
            return False
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return False
        import hmac
        return hmac.compare_digest(auth[7:].strip(), expected)

    @app.patch("/api/v1/stations/{key}/overrides")
    async def patch_station_overrides(key: str, request: Request):
        if not _operator_auth_ok(request):
            return _json(401, error_body(
                "unauthorized",
                "an Authorization: Bearer token matching MIE_OPERATOR_TOKEN "
                "is required for operator endpoints"))
        try:
            payload = await _json_request(request)
        except ValueError as exc:
            return _json(400, error_body("bad_request", str(exc)))
        overrides, error = validate_override_payload(payload)
        if error is not None:
            return _json(400, error_body("bad_request", error))
        # set_station_overrides returns False for an unknown identity_key; the
        # storage layer never creates a row from this path.
        if not storage.set_station_overrides(key, overrides):
            raise StationNotFound(f"unknown station {key!r}")
        return success_body(station_detail(_require_station(key)))

    @app.get("/api/v1/stations/{key}/verification")
    def get_verification(key: str):
        _require_station(key)
        report = storage.get_verification(key)
        return success_body({"verification": report})

    @app.post("/api/v1/ingest")
    async def ingest(request: Request):
        try:
            raw = await request.body()
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except (ValueError, UnicodeDecodeError):
            return _json(400, error_body("bad_request",
                                         "body must be valid JSON"))
        if not isinstance(payload, dict) \
                or not isinstance(payload.get("records"), list):
            return _json(400, error_body(
                "bad_request", "body must be {\"records\": [...], \"source\": str?}"))
        source = payload.get("source")
        if source is not None and not isinstance(source, str):
            return _json(400, error_body("bad_request",
                                         "'source' must be a string"))
        try:
            report = storage.ingest_intelligence(payload["records"],
                                                 source=source or "api")
        except Exception:
            return _json(500, error_body(
                "internal_error",
                "unexpected server error; see server logs"))
        return success_body(report.to_dict())

    @app.get("/api/v1/runs/{run_id}")
    def get_run(run_id: str):
        run = storage.get_ingestion_run(run_id)
        if run is None:
            return _json(404, error_body("run_not_found",
                                         f"unknown run {run_id!r}"))
        return success_body(run)

    # -- Phase 8: submission assets + link accessibility -----------------------
    #
    # Pure delegation: validation/storage/orchestration live in the
    # submissions domain and the injected track_store; handlers here only
    # translate HTTP into service calls and contract payloads.

    @app.post("/api/v1/tracks", status_code=201)
    async def upload_track(request: Request, filename: str | None = None):
        data = await request.body()
        user = _get_current_user(request)
        row = submission_service.upload_track(
            storage, track_store, data, filename=filename,
            owner_user_id=user["user_id"] if user else None)
        return success_body(track_projection(row))

    @app.post("/api/v1/tracks/releases", status_code=201)
    async def create_release(request: Request):
        # Metadata-only release creation (My Music's primary workflow): no
        # binary upload. Required title/artist_name/music_url; optional
        # storyline/genre. Reuses the tracks table and projection.
        payload = await _json_request(request)
        user = _get_current_user(request)
        row = submission_service.create_release(
            storage, payload,
            owner_user_id=user["user_id"] if user else None)
        return success_body(track_projection(row))

    @app.get("/api/v1/tracks")
    def list_tracks(limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
                    offset: int = Query(0, ge=0),
                    status: str | None = None):
        rows, total = submission_service.list_tracks(
            storage, limit=limit, offset=offset, status=status)
        return success_body(tracks_payload(rows, total, limit, offset))

    @app.get("/api/v1/tracks/{track_id}")
    def get_track(track_id: str):
        row = submission_service.get_track(storage, track_id)
        if row is None:
            raise TrackNotFound(f"unknown track {track_id!r}")
        return success_body(track_projection(row))

    @app.delete("/api/v1/tracks/{track_id}")
    def delete_track(track_id: str):
        if submission_service.get_track(storage, track_id) is None:
            raise TrackNotFound(f"unknown track {track_id!r}")
        storage.delete_track(track_id)
        return success_body({"track_id": track_id})

    @app.patch("/api/v1/tracks/{track_id}")
    async def update_track(track_id: str, request: Request):
        # Operator-owned presentation fields: the release's title,
        # artist_name, genre, official listening URL and song story /
        # creative brief. JSON body may carry any subset; an empty value
        # clears an optional field (title/artist_name must stay non-empty); an
        # invalid link is a 400 (ValueError handler) — never invented.
        # Ownership guard mirrors dispatch: a
        # release with an owner may only be edited by that user; legacy NULL-
        # owner releases keep their historical editable-by-anyone behavior.
        track = submission_service.get_track(storage, track_id)
        if track is None:
            raise TrackNotFound(f"unknown track {track_id!r}")
        user = _get_current_user(request)
        owner = track.get("owner_user_id")
        if owner and (not user or user["user_id"] != owner):
            return _json(403, error_body(
                "forbidden", "this release belongs to another user"))
        payload = await _json_request(request)
        row = submission_service.update_track_fields(
            storage, track_id, payload)
        return success_body(track_projection(row))

    @app.get("/api/v1/stations/{key}/submission")
    def get_submission(key: str):
        view = submission_service.station_submission(storage, key)
        last_checks = submission_service.last_checks_by_target(
            storage.get_link_checks(key, limit=50))
        return success_body(submission_view(
            view["identity_key"], view["submission"], last_checks))

    @app.post("/api/v1/stations/{key}/submission/checks")
    def run_submission_checks(key: str):
        _require_station(key)
        summary = submission_service.run_link_checks(
            storage, link_fetcher, key, allow_private=allow_private)
        return success_body(summary)

    @app.get("/api/v1/stations/{key}/submission/checks")
    def submission_check_history(
            key: str,
            limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)):
        history = submission_service.link_history(storage, key,
                                                  limit=limit)
        return success_body(history)

    # -- Phase 9: outreach ----------------------------------------------------

    @app.post("/api/v1/outreach", status_code=201)
    async def create_outreach(request: Request):
        payload = await _json_request(request)
        record = outreach_service.create_outreach(storage, payload=payload)
        return success_body(record)

    @app.get("/api/v1/outreach")
    def list_outreach(limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
                      offset: int = Query(0, ge=0),
                      status: str | None = None):
        rows, total = outreach_service.list_outreach(
            storage, limit=limit, offset=offset, status=status)
        return success_body({"outreach": rows, "total": total,
                             "limit": limit, "offset": offset})

    @app.get("/api/v1/outreach/{outreach_id}")
    def get_outreach(outreach_id: str):
        record = outreach_service.get_outreach(storage, outreach_id)
        if record is None:
            raise OutreachNotFound(f"unknown outreach {outreach_id!r}")
        return success_body(record)

    @app.delete("/api/v1/outreach/{outreach_id}")
    def delete_outreach(outreach_id: str):
        if outreach_service.get_outreach(storage, outreach_id) is None:
            raise OutreachNotFound(f"unknown outreach {outreach_id!r}")
        storage.delete_outreach(outreach_id)
        return success_body({"outreach_id": outreach_id})

    @app.post("/api/v1/outreach/{outreach_id}/event")
    async def outreach_event(outreach_id: str, request: Request):
        if outreach_service.get_outreach(storage, outreach_id) is None:
            raise OutreachNotFound(f"unknown outreach {outreach_id!r}")
        payload = await _json_request(request)
        event = payload.get("event")
        if not isinstance(event, str) or event not in \
                outreach_service.OUTREACH_EVENTS:
            raise ValueError("invalid outreach event")
        record = outreach_service.record_outreach_event(
            storage, outreach_id, event=event, meta=payload.get("meta"))
        return success_body(record)

    @app.post("/api/v1/outreach/draft-email")
    async def draft_email(request: Request):
        # Song-story-aware AI DRAFT. An authenticated operator (email
        # session — never Google OAuth) composes from a release they are
        # authorized for; the response is a draft only and nothing is
        # persisted or sent.
        user = _get_current_user(request)
        if not user:
            return _json(401, error_body(
                "unauthorized", "sign in to write an email with AI"))
        payload = await _json_request(request)
        track_id = payload.get("track_id")
        if not isinstance(track_id, str) or not track_id.strip():
            raise ValueError("track_id required")
        track = submission_service.get_track(storage, track_id)
        if track is None:
            raise TrackNotFound(f"unknown track {track_id!r}")
        owner = track.get("owner_user_id")
        if owner and owner != user["user_id"]:
            return _json(403, error_body(
                "forbidden", "this release belongs to another user"))
        from outreach import ai_writer
        draft = ai_writer.compose_email(
            track=track, payload=payload, client=ai_writer.default_client())
        return success_body(draft)

    @app.post("/api/v1/outreach/{outreach_id}/send")
    async def outreach_send(outreach_id: str, request: Request):
        user = _get_current_user(request)
        if not user:
            return _json(401, error_body("unauthorized", "not logged in"))
        record = outreach_service.get_outreach(storage, outreach_id)
        if record is None:
            raise OutreachNotFound(f"unknown outreach {outreach_id!r}")
        recipient = record.get("recipient") or {}
        recipient_email = recipient.get("email")
        if not recipient_email:
            return _json(400, error_body(
                "bad_request",
                "recipient has no email address; open in your email app instead"))
        from gmail_provider import GmailProvider, GmailSendError
        gp = GmailProvider(storage)
        try:
            res = gp.send_email(
                user["user_id"],
                recipient_email,
                record.get("subject") or "",
                record.get("message") or "",
            )
        except GmailSendError as e:
            code = getattr(e, "code", "gmail_send_failed")
            if code == "no_connected_gmail":
                return _json(400, error_body("no_connected_gmail", "Connect Gmail first"))
            outreach_service.record_outreach_event(
                storage, outreach_id, event="failed", meta={"error": str(e), "provider": "gmail_user"}
            )
            if code == "gmail_reauth_required":
                return _json(400, error_body(
                    code, "Your Google authorization expired or was "
                    "revoked - reconnect Gmail to send."))
            if code == "gmail_scope_missing":
                return _json(403, error_body(
                    code, "Gmail send permission is missing - reconnect "
                    "Gmail and allow sending."))
            return _json(500, error_body("gmail_send_failed", str(e)))
        except Exception as e:
            outreach_service.record_outreach_event(
                storage, outreach_id, event="failed", meta={"error": str(e), "provider": "gmail_user"}
            )
            return _json(500, error_body("send_failed", str(e)))
        record = outreach_service.record_outreach_event(
            storage,
            outreach_id,
            event="sent",
            meta={"provider": "gmail_user",
                  "message_id": res.get("message_id"),
                  "thread_id": res.get("thread_id"),
                  "sender_email": res.get("sender_email")},
        )
        return success_body(record)

    # -- Phase 4b: independent DJs -------------------------------------------
    # Independent music professionals, kept fully separate from the radio
    # station pipeline. Station fields are optional metadata only.

    def _check_automation_auth(request: Request) -> bool:
        expected = os.environ.get("MIE_AUTOMATION_TOKEN", "").strip()
        if not expected:
            return True
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return False
        import hmac
        return hmac.compare_digest(auth[7:].strip(), expected)

    @app.get("/api/v1/djs")
    def list_djs(limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
                 offset: int = Query(0, ge=0),
                 q: str | None = None,
                 genre: str | None = None,
                 country: str | None = None,
                 location: str | None = None,
                 station: str | None = None,
                 dj_type: str | None = None,
                 platform: str | None = None,
                 has_contact: bool | None = None,
                 sort: str | None = Query(None, pattern="^(name|station|discovered)$"),
                 order: str | None = Query(None, pattern="^(asc|desc)$")):
        rows, total, dev_excluded, rejected_excluded = dj_service.list_djs(
            storage, limit=limit, offset=offset, q=q, genre=genre,
            country=country, location=location, station=station,
            dj_type=dj_type, platform=platform, has_contact=has_contact,
            sort=sort, order=order, exclude_dev=True,
            exclude_rejected=True)
        return success_body({"djs": [dj_summary(r) for r in rows],
                             "total": total, "limit": limit,
                             "offset": offset,
                             "dev_fixtures_excluded": dev_excluded,
                             "rejected_excluded": rejected_excluded})

    @app.post("/api/v1/djs", status_code=201)
    async def create_dj(request: Request):
        payload = await _json_request(request)
        dj = dj_service.create_dj(storage, payload=payload)
        return success_body(dj_service.dj_detail(storage, dj["dj_id"]))

    @app.post("/api/v1/djs/discover")
    async def discover_djs(request: Request):
        # Independent public-web DJ discovery, kept fully separate from the
        # radio-station pipeline. Honest contract: a real provider must be
        # configured, or this returns 503 — never a fabricated/fixture result.
        try:
            from discovery.djs.selector import select_djs_discovery_provider
            from discovery.djs.pipeline import DjsDiscoveryEngine
            from discovery.models import DiscoveryRequest
        except ImportError as exc:
            return _json(503, error_body("discovery_dependency_missing",
                                          str(exc)))
        provider = select_djs_discovery_provider(fetcher=discover_fetcher)
        if not provider.configured:
            return _json(503, error_body(
                "dj_discovery_provider_not_configured",
                "DJ discovery provider is not configured; set "
                "DJS_SEARCH_BASE_URL / DJS_SEARCH_API_KEY (see .env.example)"))
        payload = await _json_request(request)
        request = DiscoveryRequest.from_dict(payload)
        engine = DjsDiscoveryEngine(provider, fetcher=discover_fetcher)
        result = engine.run(request)
        if result.failures:
            return _json(502, error_body("dj_discovery_provider_error",
                                         "; ".join(str(f.message) for f in result.failures)))
        report = dj_service.ingest_djs(storage, result.records)
        return success_body(report)

    # -- Phase 11: automation discovery jobs ----------------------------------

    @app.post("/api/v1/discovery/jobs")
    async def run_discovery_job(request: Request):
        if not _check_automation_auth(request):
            return _json(401, error_body(
                "unauthorized",
                "an Authorization: Bearer token matching MIE_AUTOMATION_TOKEN "
                "is required for automation endpoints"))
        from discovery.jobs import prepare_discovery_job
        from discovery.models import utc_now_iso
        import uuid
        payload = await _json_request(request)
        # Validation is the only synchronous work; ValueError -> 400 via the
        # registered exception handler. No discovery happens in this request.
        org_type, config = prepare_discovery_job(payload)
        run_id = "job_" + uuid.uuid4().hex[:24]
        started_at = utc_now_iso()
        scheduler.submit(run_id, org_type, config, started_at)
        return _json(202, success_body({
            "run_id": run_id,
            "organization_type": org_type,
            "status": "queued",
            "started_at": started_at,
        }))

    @app.get("/api/v1/discovery/jobs/{run_id}")
    def get_discovery_job(run_id: str, request: Request):
        if not _check_automation_auth(request):
            return _json(401, error_body(
                "unauthorized",
                "an Authorization: Bearer token matching MIE_AUTOMATION_TOKEN "
                "is required for automation endpoints"))
        run = storage.get_discovery_job(run_id)
        if run is None:
            return _json(404, error_body(
                "run_not_found", f"unknown discovery job {run_id!r}"))
        run = {"job_type": f"{run['organization_type']}_discovery", **run}
        return success_body(run)

    @app.get("/api/v1/djs/{dj_id}")
    def get_dj(dj_id: str):
        detail = dj_service.dj_detail(storage, dj_id)
        if detail is None:
            raise DjNotFound(f"unknown dj {dj_id!r}")
        return success_body(detail)

    @app.delete("/api/v1/djs/{dj_id}")
    def delete_dj(dj_id: str):
        if dj_service.dj_detail(storage, dj_id) is None:
            raise DjNotFound(f"unknown dj {dj_id!r}")
        storage.delete_dj(dj_id)
        return success_body({"dj_id": dj_id})

    return app


def build_storage(db_path: str | None = None,
                  pg_dsn: str | None = None):
    """SQLite via --db/MIE_DATABASE_PATH; PostgreSQL via --dsn/MIE_PG_DSN.

    Precedence is deterministic: an explicit ``db_path`` ALWAYS wins over a
    PostgreSQL DSN, so an operator who asks for a local SQLite database never
    silently connects to PostgreSQL just because ``MIE_PG_DSN`` is present in
    the environment. A configured DSN is used only when no local database was
    requested.
    """
    if db_path:
        from database.service import PersistenceService
        return PersistenceService(db_path)
    if pg_dsn:
        from database.pg_store import PostgresStorage
        return PostgresStorage(dsn=pg_dsn)
    raise SystemExit(
        "--db or MIE_DATABASE_PATH (or --dsn / MIE_PG_DSN) is required")


def main(argv: list[str] | None = None) -> int:
    import argparse

    # Railway-style hosting: a PG DSN marks production. Bind to 0.0.0.0 by
    # default there so the platform's ingress can reach the server, and
    # honor the standard PORT var the platform supplies. MIE_API_HOST /
    # MIE_API_PORT always take precedence when explicitly set. Local
    # development (no DSN) keeps the historical 127.0.0.1:8788 defaults.
    production = bool(os.environ.get("MIE_PG_DSN"))
    default_host = "0.0.0.0" if production else "127.0.0.1"
    default_port = int(os.environ.get("MIE_API_PORT")
                       or os.environ.get("PORT") or "8788")

    parser = argparse.ArgumentParser(
        prog="python -m backend.app",
        description="Serve stored radio intelligence (FastAPI).")
    parser.add_argument("--db",
                        default=os.environ.get("MIE_DATABASE_PATH"),
                        help="SQLite DB path (env: MIE_DATABASE_PATH)")
    parser.add_argument("--dsn",
                        default=os.environ.get("MIE_PG_DSN"),
                        help="PostgreSQL DSN (env: MIE_PG_DSN)")
    parser.add_argument("--host",
                        default=os.environ.get("MIE_API_HOST", default_host))
    parser.add_argument("--port", type=int, default=default_port)
    args = parser.parse_args(argv)

    storage = build_storage(args.db, args.dsn)
    app = create_app(storage)
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    storage.close()
    return 0


# ---------------------------------------------------------------------------
# ASGI entrypoint (``backend.app:app``) for uvicorn / hosting platforms
# ---------------------------------------------------------------------------
#
# Railway-family platforms start the server with ``uvicorn backend.app:app``.
# Uvicorn imports the module then does ``getattr(module, "app")``. There is no
# module-level ``app`` because the FastAPI application requires an injected
# storage backend (factory pattern). PEP 562 ``__getattr__`` serves ``app``
# lazily: construction happens only when uvicorn resolves the attribute, so a
# plain ``from backend.app import create_app`` (tests, ``main``) never touches
# the database at import time. The app is built once and cached.
#
# Storage for the hosted entrypoint resolves from MIE_PG_DSN (preferred),
# then DATABASE_URL (the variable Railway's PostgreSQL plugin injects), then
# MIE_DATABASE_PATH (SQLite) — matching build_storage's env-driven behavior.

_APP: dict = {}


def _resolve_uvicorn_storage():
    pg_dsn = os.environ.get("MIE_PG_DSN") or os.environ.get("DATABASE_URL")
    db_path = os.environ.get("MIE_DATABASE_PATH")
    return build_storage(db_path=db_path, pg_dsn=pg_dsn)


def __getattr__(name: str):
    if name == "app":
        if "app" not in _APP:
            _APP["app"] = create_app(_resolve_uvicorn_storage())
        return _APP["app"]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    raise SystemExit(main())
