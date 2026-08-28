"""Single-origin operator server: static frontend + JSON API (Phase 7).

Serves the zero-dependency SPA in ``frontend/`` and the Phase 4-6 API
under one origin so the operator UI needs no build step, no CDN, and no
cross-origin configuration:

    python -m backend.webapp --db path/to/db.sqlite [--static frontend]

Security posture:
- Static files resolve strictly under the static root; traversal attempts
  (``..``, absolute paths, encoded variants) are rejected.
- Only a small extension allow-list is served; dotfiles are never served.
- Every response carries ``X-Content-Type-Options: nosniff``; API routes
  additionally send ``Cache-Control: no-store``.
- ``/api/v1/*`` dispatch is delegated to ``backend.routes`` (shared with
  the reference server), so behavior is identical across servers.

Configuration uses CLI flags or environment variable NAMES only
(MIE_DATABASE_PATH / MIE_WEB_HOST / MIE_WEB_PORT). Values are never
logged or echoed into responses.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from backend.routes import dispatch

from database.service import PersistenceService
from submissions import service as submission_service

DEFAULT_STATIC_ROOT = Path(__file__).resolve().parents[1] / "frontend"

_ALLOWED_SUFFIXES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
}


def build_handler(service, static_root: Path, track_store=None,
                  link_fetcher=None, allow_private: bool = False):
    """Create a request handler bound to *service* + *static_root*.

    ``track_store``/``link_fetcher`` inject the Phase 8 submission
    dependencies; process defaults are constructed when omitted.
    """
    if track_store is None:
        track_store = submission_service.default_track_store()
    if link_fetcher is None:
        link_fetcher = submission_service.default_link_fetcher()

    class OperatorWebappHandler(BaseHTTPRequestHandler):
        server_version = "MIE-WEB/0.7"
        protocol_version = "HTTP/1.1"

        # -- plumbing ------------------------------------------------------
        def _send(self, status: int, content_type: str, payload: bytes,
                  extra_headers: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Content-Type-Options", "nosniff")
            if extra_headers:
                for name, value in extra_headers.items():
                    self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(self, status: int, body: dict) -> None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self._send(status, "application/json; charset=utf-8", payload,
                       {"Cache-Control": "no-store"})

        def _read_body(self) -> bytes | None:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return None
            return self.rfile.read(length)

        def log_message(self, fmt, *args):
            pass

        # -- routing ---------------------------------------------------------
        def do_GET(self):   # noqa: N802 (http.server API)
            parts = urlsplit(self.path)
            path = unquote(parts.path)
            if path == "/api/v1" or path.startswith("/api/v1/"):
                self._api("GET", path, parse_qs(parts.query))
            else:
                self._static(path)

        def do_POST(self):   # noqa: N802 (http.server API)
            parts = urlsplit(self.path)
            path = unquote(parts.path)
            if path == "/api/v1" or path.startswith("/api/v1/"):
                self._api("POST", path, parse_qs(parts.query))
            else:
                self._send_json(404, {
                    "ok": False, "data": None,
                    "error": {"code": "route_not_found",
                              "message": f"no route for {path!r}"}})

        do_PUT = do_POST
        do_DELETE = do_POST
        do_PATCH = do_POST

        # -- api -------------------------------------------------------------
        def _api(self, method: str, path: str, params: dict) -> None:
            try:
                status, body = dispatch(
                    service, method, path, params, self._read_body(),
                    track_store=track_store, link_fetcher=link_fetcher,
                    allow_private=allow_private)
                self._send_json(status, body)
            except Exception:
                self._send_json(500, {
                    "ok": False, "data": None,
                    "error": {"code": "internal_error",
                              "message": "unexpected server error; "
                                         "see server logs"}})

        # -- static ------------------------------------------------------------
        def _static(self, path: str) -> None:
            if "\x00" in path or "\\" in path:
                self._send(403, "text/plain; charset=utf-8", b"forbidden")
                return
            relative = path.lstrip("/")
            if not relative:
                relative = "index.html"
            candidate = (static_root / relative).resolve()
            root = static_root.resolve()
            if not str(candidate).startswith(str(root) + os.sep) \
                    and candidate != root:
                self._send(403, "text/plain; charset=utf-8", b"forbidden")
                return
            suffix = candidate.suffix.lower()
            if suffix not in _ALLOWED_SUFFIXES \
                    or candidate.name.startswith("."):
                self._send(404, "text/plain; charset=utf-8", b"not found")
                return
            try:
                payload = candidate.read_bytes()
            except OSError:
                self._send(404, "text/plain; charset=utf-8", b"not found")
                return
            self._send(200, _ALLOWED_SUFFIXES[suffix], payload,
                       {"Cache-Control": "no-cache"})

    return OperatorWebappHandler


def create_server(db_path: str, host: str, port: int,
                  static_root: Path | None = None, *,
                  track_store=None, link_fetcher=None,
                  allow_private: bool = False) -> ThreadingHTTPServer:
    service = PersistenceService(db_path) if isinstance(db_path, str) \
        else db_path
    handler = build_handler(service,
                            Path(static_root or DEFAULT_STATIC_ROOT),
                            track_store=track_store,
                            link_fetcher=link_fetcher,
                            allow_private=allow_private)
    server = ThreadingHTTPServer((host, port), handler)
    server.service = service      # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.webapp",
        description="Serve the operator UI and the intelligence API from "
                    "one origin.")
    parser.add_argument("--db", default=os.environ.get("MIE_DATABASE_PATH"),
                        help="SQLite DB path (env: MIE_DATABASE_PATH)")
    parser.add_argument("--host",
                        default=os.environ.get("MIE_WEB_HOST",
                                               os.environ.get(
                                                   "MIE_API_HOST",
                                                   "127.0.0.1")))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get(
                            "MIE_WEB_PORT",
                            os.environ.get("MIE_API_PORT", "8788"))))
    parser.add_argument("--static", default=str(DEFAULT_STATIC_ROOT),
                        help="directory containing index.html")
    args = parser.parse_args(argv)
    if not args.db:
        parser.error("--db or MIE_DATABASE_PATH is required")

    server = create_server(args.db, args.host, args.port,
                           Path(args.static))
    print(f"operator UI listening on http://{args.host}:{args.port}"
          f" (db schema v{getattr(server.service, 'version', '?')}); "
          f"Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        close = getattr(server.service, "close", None)
        if callable(close):
            close()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
