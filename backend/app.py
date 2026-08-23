"""FastAPI application over stored radio intelligence (Phase 6).

Primary Phase 6 API server per docs/architecture.md ("backend owns
persistence and business queries"). Serves the SAME envelope contract,
routes, and payload shapes as the Phase 4 stdlib server
(``backend.api``), plus the Phase 6 additions:

    GET  /api/v1/health
    GET  /api/v1/stations?limit&offset&q&status[&genre&format&country&min_confidence]
    GET  /api/v1/stations/{identity_key}
    GET  /api/v1/stations/{identity_key}/intelligence
    GET  /api/v1/stations/{identity_key}/contacts
    GET  /api/v1/stations/{identity_key}/verification      (NEW)
    POST /api/v1/ingest                                    (NEW)
    GET  /api/v1/runs/{run_id}                             (NEW)

Envelope contract (unchanged):
    success -> {"ok": true,  "data": <payload>, "error": null}
    failure -> {"ok": false, "data": null,
                "error": {"code": <str>, "message": <str>}}

The storage backend is injected (``create_app(storage)``): SQLite
``PersistenceService`` offline/tests, ``PostgresStorage`` against a live
PostgreSQL/Supabase instance. FastAPI/Starlette are imported at module
level — Phases 1-5 suites simply never import this module, so they never
require the dependency. Annotations must stay resolvable at module scope
(PEP 563 + FastAPI dependency resolution). Configuration uses env var
NAMES only (MIE_DATABASE_PATH / MIE_PG_DSN / MIE_API_HOST / MIE_API_PORT);
values are never logged or echoed into responses. No crawling logic, no
secrets.
"""

from __future__ import annotations

import json
import os

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.contracts import (
    contacts_payload,
    error_body,
    intelligence_payload,
    station_detail,
    station_summary,
    success_body,
)

MAX_LIMIT = 200
DEFAULT_LIMIT = 50


class StationNotFound(Exception):
    """Unknown identity key; converted to a 404 envelope."""


def create_app(storage):
    """Build the FastAPI application bound to *storage*.

    Imported lazily as a whole so importing ``backend.app`` stays optional
    for environments without FastAPI installed.
    """
    app = FastAPI(title="Music Intelligence Engine API",
                  version="0.6",
                  docs_url="/api/v1/docs", openapi_url="/api/v1/openapi.json")

    def _json(status: int, body: dict) -> JSONResponse:
        return JSONResponse(status_code=status, content=body)

    @app.exception_handler(StationNotFound)
    async def _station_not_found(request: Request, exc: StationNotFound):
        return _json(404, error_body("station_not_found", str(exc)))

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
                        default=os.environ.get("MIE_API_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("MIE_API_PORT", "8788")))
    args = parser.parse_args(argv)

    storage = build_storage(args.db, args.dsn)
    app = create_app(storage)
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    storage.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
