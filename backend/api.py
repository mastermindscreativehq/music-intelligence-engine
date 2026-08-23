"""Minimal HTTP API over stored radio intelligence (Phase 4/6).

Smallest implementation consistent with the repository's zero-dependency
policy: Python stdlib ``http.server`` serving JSON. It is a thin, stateless
adapter over the persistence service — no business logic, no crawling, no
secrets. Routing is delegated to ``backend.routes`` so this reference
server and the single-origin webapp server (``backend.webapp``, Phase 7)
stay behaviorally identical. The FastAPI application remains the
documented production stack.

Endpoints:

    GET  /api/v1/health
    GET  /api/v1/stations?limit&offset&q&status[&genre&format&country&min_confidence]
    GET  /api/v1/stations/{identity_key}
    GET  /api/v1/stations/{identity_key}/intelligence
    GET  /api/v1/stations/{identity_key}/contacts
    GET  /api/v1/stations/{identity_key}/verification
    POST /api/v1/ingest          {"records": [...], "source": str?}
    GET  /api/v1/runs/{run_id}

Envelope contract:
    success -> {"ok": true,  "data": <payload>, "error": null}
    failure -> {"ok": false, "data": null,
                "error": {"code": <str>, "message": <str>}}

Configuration comes from CLI flags or environment variable NAMES
(MIE_DATABASE_PATH / MIE_API_HOST / MIE_API_PORT). Values are never logged
or echoed into responses.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from backend.routes import dispatch

from database.schema import SCHEMA_VERSION
from database.service import PersistenceService

LOGGER = logging.getLogger("mie.api")


def build_handler(service: PersistenceService):
    """Create a request handler class bound to *service*."""

    class RadioIntelligenceAPIHandler(BaseHTTPRequestHandler):
        server_version = "MIE-API/0.6"
        protocol_version = "HTTP/1.1"

        # -- plumbing ------------------------------------------------------
        def _send_json(self, status: int, body: dict) -> None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type",
                             "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def _read_body(self) -> bytes | None:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return None
            return self.rfile.read(length)

        def _dispatch(self, method: str) -> None:
            parts = urlsplit(self.path)
            try:
                status, body = dispatch(
                    service, method, unquote(parts.path),
                    parse_qs(parts.query), self._read_body())
                self._send_json(status, body)
            except Exception:
                LOGGER.exception("handler failure")
                self._send_json(500, {
                    "ok": False, "data": None,
                    "error": {"code": "internal_error",
                              "message": "unexpected server error; "
                                         "see server logs"}})

        def log_message(self, fmt, *args):   # keep default access logs quiet
            pass

        # -- routing ---------------------------------------------------------
        def do_GET(self):   # noqa: N802 (http.server API)
            self._dispatch("GET")

        def do_POST(self):   # noqa: N802 (http.server API)
            self._dispatch("POST")

        do_PUT = do_POST
        do_DELETE = do_POST
        do_PATCH = do_POST

    return RadioIntelligenceAPIHandler


def create_server(db_path: str, host: str,
                  port: int) -> ThreadingHTTPServer:
    service = PersistenceService(db_path)
    handler = build_handler(service)
    server = ThreadingHTTPServer((host, port), handler)
    server.service = service      # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.api",
        description="Serve stored radio intelligence (read-only JSON API "
                    "plus ingestion endpoints).")
    parser.add_argument("--db", default=os.environ.get("MIE_DATABASE_PATH"),
                        help="SQLite DB path (env: MIE_DATABASE_PATH)")
    parser.add_argument("--host",
                        default=os.environ.get("MIE_API_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("MIE_API_PORT", "8787")))
    args = parser.parse_args(argv)
    if not args.db:
        parser.error("--db or MIE_DATABASE_PATH is required")

    server = create_server(args.db, args.host, args.port)
    print(f"radio intelligence API listening on http://{args.host}:{args.port}"
          f" (db schema v{SCHEMA_VERSION}); Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.service.close()   # type: ignore[attr-defined]
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
