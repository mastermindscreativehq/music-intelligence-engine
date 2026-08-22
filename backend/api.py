"""Minimal HTTP API over stored radio intelligence (Phase 4).

Smallest implementation consistent with the repository's zero-dependency
policy: Python stdlib ``http.server`` serving JSON. It is a thin, stateless
adapter over the persistence service — no business logic, no crawling, no
secrets. FastAPI/Postgres remain the documented future stack (NOT
IMPLEMENTED in Phase 4); this layer is deliberately swappable.

Endpoints (GET only):

    /api/v1/health
    /api/v1/stations?limit&offset&q&status
    /api/v1/stations/{identity_key}
    /api/v1/stations/{identity_key}/intelligence
    /api/v1/stations/{identity_key}/contacts

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
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from backend.contracts import (
    contacts_payload,
    error_body,
    intelligence_payload,
    station_detail,
    station_summary,
    success_body,
)

from database.schema import SCHEMA_VERSION
from database.service import PersistenceService

MAX_LIMIT = 200
DEFAULT_LIMIT = 50

_ROUTE_STATION = re.compile(r"^/api/v1/stations/(?P<key>[^/]+)$")
_ROUTE_INTELLIGENCE = re.compile(
    r"^/api/v1/stations/(?P<key>[^/]+)/intelligence$")
_ROUTE_CONTACTS = re.compile(r"^/api/v1/stations/(?P<key>[^/]+)/contacts$")

LOGGER = logging.getLogger("mie.api")


class StationNotFound(Exception):
    """Signal for unknown identity keys; converted to a 404 envelope."""


def _int_param(params: dict, name: str, default: int,
               minimum: int, maximum: int | None) -> int:
    raw = (params.get(name) or [None])[0]
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"'{name}' must be an integer") from None
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"'{name}' must be between {minimum} and "
                         f"{maximum if maximum is not None else '∞'}")
    return value


def build_handler(service: PersistenceService):
    """Create a request handler class bound to *service*."""

    class RadioIntelligenceAPIHandler(BaseHTTPRequestHandler):
        server_version = "MIE-API/0.4"
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

        def log_message(self, fmt, *args):   # keep default access logs quiet
            pass

        # -- routing ---------------------------------------------------------
        def do_GET(self):   # noqa: N802 (http.server API)
            parts = urlsplit(self.path)
            try:
                self._route(unquote(parts.path), parse_qs(parts.query))
            except StationNotFound as exc:
                self._send_json(404, error_body("station_not_found",
                                                str(exc)))
            except (ValueError, TypeError) as exc:
                self._send_json(400, error_body("bad_request", str(exc)))
            except Exception:
                LOGGER.exception("handler failure")
                self._send_json(500, error_body(
                    "internal_error",
                    "unexpected server error; see server logs"))

        def _route(self, path: str, params: dict) -> None:
            if path == "/api/v1/health":
                self._send_json(200, success_body({
                    "status": "ok", "schema_version": SCHEMA_VERSION}))
                return
            if path == "/api/v1/stations":
                self._handle_list(params)
                return
            match = _ROUTE_INTELLIGENCE.match(path)
            if match:
                self._handle_intelligence(match.group("key"))
                return
            match = _ROUTE_CONTACTS.match(path)
            if match:
                self._handle_contacts(match.group("key"))
                return
            match = _ROUTE_STATION.match(path)
            if match:
                self._handle_station(match.group("key"))
                return
            self._send_json(404, error_body(
                "route_not_found", f"no route for {path!r}"))

        # -- handlers ----------------------------------------------------------
        def _require_station(self, key: str) -> dict:
            row = service.get_station(key)
            if row is None:
                raise StationNotFound(f"unknown station {key!r}")
            return row

        def _handle_list(self, params: dict) -> None:
            limit = _int_param(params, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
            offset = _int_param(params, "offset", 0, 0, None)
            q = (params.get("q") or [None])[0]
            status = (params.get("status") or [None])[0]
            rows, total = service.list_stations(limit=limit, offset=offset,
                                                q=q, status=status)
            self._send_json(200, success_body({
                "stations": [station_summary(r) for r in rows],
                "total": total, "limit": limit, "offset": offset,
            }))

        def _handle_station(self, key: str) -> None:
            row = self._require_station(key)
            self._send_json(200, success_body(station_detail(row)))

        def _handle_intelligence(self, key: str) -> None:
            row = self._require_station(key)
            self._send_json(200, success_body(intelligence_payload(
                station=row,
                emails=service.get_station_emails(key),
                phones=service.get_station_phones(key),
                contacts=service.get_station_contacts(key),
                submission=service.get_submission(key),
                fetches=service.get_fetches(key),
            )))

        def _handle_contacts(self, key: str) -> None:
            row = self._require_station(key)
            self._send_json(200, success_body(contacts_payload(
                station=row,
                contacts=service.get_station_contacts(key),
                submission=service.get_submission(key),
            )))

        def do_POST(self):
            self._send_json(405, error_body(
                "method_not_allowed", "only GET is supported"))

        do_PUT = do_DELETE = do_PATCH = do_POST

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
        description="Serve stored radio intelligence (read-only JSON API).")
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
