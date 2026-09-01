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
    GET  /api/v1/tracks[?limit&offset&status]              (Phase 8)
    GET  /api/v1/tracks/{track_id}                         (Phase 8)
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
SUBMISSION_STORAGE_ROOT); values are never logged or echoed into
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
import os

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.contracts import (
    contacts_payload,
    error_body,
    intelligence_payload,
    station_detail,
    station_summary,
    submission_view,
    success_body,
    track_projection,
    tracks_payload,
)

from submissions import service as submission_service
from submissions.service import TrackRejected, TrackTooLarge

from outreach import service as outreach_service

MAX_LIMIT = 200
DEFAULT_LIMIT = 50


class StationNotFound(Exception):
    """Unknown identity key; converted to a 404 envelope."""


class TrackNotFound(Exception):
    """Unknown opaque asset id; converted to a 404 envelope."""


class OutreachNotFound(Exception):
    """Unknown outreach id; converted to a 404 envelope."""


def create_app(storage, *, track_store=None, link_fetcher=None,
               allow_private=False):
    """Build the FastAPI application bound to *storage*.

    Imported lazily as a whole so importing ``backend.app`` stays optional
    for environments without FastAPI installed.
    """
    if track_store is None:
        track_store = submission_service.default_track_store()
    if link_fetcher is None:
        link_fetcher = submission_service.default_link_fetcher()
    app = FastAPI(title="Music Intelligence Engine API",
                  version="0.8",
                  docs_url="/api/v1/docs", openapi_url="/api/v1/openapi.json")

    # Cross-origin support for separately-hosted frontends (e.g. a static
    # Vercel console against this Railway backend). Origins come from the
    # MIE_CORS_ORIGINS env var (comma-separated absolute origins). Unset or
    # empty means no cross-origin is allowed, preserving the historical
    # same-origin behavior. Credentials stay off: the API is unauthenticated
    # and same-origin cookies are never needed across hosts.
    cors_origins = []
    for origin in os.environ.get("MIE_CORS_ORIGINS", "").split(","):
        origin = origin.strip()
        if not origin:
            continue
        # drop a trailing slash (and any query/fragment) so values like
        # "https://console.example.app/" still match the browser Origin header
        origin = origin.split("?")[0].split("#")[0].rstrip("/")
        if origin:
            cors_origins.append(origin)
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "Accept"],
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

    # -- routes ----------------------------------------------------------------

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
        rows, total = storage.list_stations(
            limit=limit, offset=offset, q=q, status=status, genre=genre,
            format_filter=format, country=country,
            min_confidence=min_confidence)
        return success_body({
            "stations": [station_summary(r) for r in rows],
            "total": total, "limit": limit, "offset": offset,
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
        row = submission_service.upload_track(
            storage, track_store, data, filename=filename)
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

    @app.post("/api/v1/outreach/{outreach_id}/event")
    async def outreach_event(outreach_id: str, request: Request):
        if outreach_service.get_outreach(storage, outreach_id) is None:
            raise OutreachNotFound(f"unknown outreach {outreach_id!r}")
        payload = await _json_request(request)
        event = payload.get("event")
        if not isinstance(event, str) or event not in \
                outreach_service.OUTREACH_STATUSES or event == "draft":
            raise ValueError("invalid outreach event")
        record = outreach_service.record_outreach_event(
            storage, outreach_id, event=event, meta=payload.get("meta"))
        return success_body(record)

    return app


def build_storage(db_path: str | None = None,
                  pg_dsn: str | None = None):
    """SQLite offline/tests by default; PostgreSQL via explicit DSN."""
    if pg_dsn:
        from database.pg_store import PostgresStorage
        return PostgresStorage(dsn=pg_dsn)
    from database.service import PersistenceService
    if not db_path:
        raise SystemExit(
            "--db or MIE_DATABASE_PATH (or --dsn / MIE_PG_DSN) is required")
    return PersistenceService(db_path)


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
