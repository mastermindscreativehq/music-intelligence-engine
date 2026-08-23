"""Framework-free API routing shared by the stdlib servers (Phases 4-7).

Single source of truth for the stdlib HTTP surface so ``backend.api``
(read-only reference server) and ``backend.webapp`` (single-origin
operator server for the frontend) dispatch identically. The FastAPI
application (``backend.app``) remains the documented production stack;
contract tests pin all three to the same envelope and error codes.

Envelope contract (unchanged since Phase 4):

    success -> {"ok": true,  "data": <payload>, "error": null}
    failure -> {"ok": false, "data": null,
                "error": {"code": <str>, "message": <str>}}

Error codes: station_not_found | run_not_found | bad_request |
route_not_found | method_not_allowed | internal_error. Contract failures
become envelopes here; unexpected exceptions PROPAGATE so the server
layer logs them and answers internal_error.
"""

from __future__ import annotations

import json
import re

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

LIST_PARAMS = ("limit", "offset", "q", "status", "genre", "format",
               "country", "min_confidence")


class StationNotFound(Exception):
    """Unknown identity key; converted to a 404 envelope."""


class RunNotFound(Exception):
    """Unknown ingestion run id; converted to a 404 envelope."""


# (method, pattern, path_template, query_params)
ROUTE_TABLE = (
    ("GET", re.compile(r"^/api/v1/health$"), "/api/v1/health", ()),
    ("GET", re.compile(r"^/api/v1/stations$"), "/api/v1/stations",
     LIST_PARAMS),
    ("POST", re.compile(r"^/api/v1/ingest$"), "/api/v1/ingest", ()),
    ("GET", re.compile(r"^/api/v1/runs/(?P<run_id>[^/]+)$"),
     "/api/v1/runs/{run_id}", ()),
    ("GET", re.compile(r"^/api/v1/stations/(?P<key>[^/]+)/intelligence$"),
     "/api/v1/stations/{key}/intelligence", ()),
    ("GET", re.compile(r"^/api/v1/stations/(?P<key>[^/]+)/contacts$"),
     "/api/v1/stations/{key}/contacts", ()),
    ("GET", re.compile(r"^/api/v1/stations/(?P<key>[^/]+)/verification$"),
     "/api/v1/stations/{key}/verification", ()),
    ("GET", re.compile(r"^/api/v1/stations/(?P<key>[^/]+)$"),
     "/api/v1/stations/{key}", ()),
)


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


def _first(params: dict, name: str):
    return (params.get(name) or [None])[0]


def _parse_ingest_body(body: bytes | None) -> tuple[list, str]:
    """Validate the POST /api/v1/ingest body shape."""
    try:
        payload = json.loads((body or b"").decode("utf-8")) if body else None
    except (ValueError, UnicodeDecodeError):
        raise ValueError("body must be valid JSON") from None
    if not isinstance(payload, dict) \
            or not isinstance(payload.get("records"), list):
        raise ValueError(
            'body must be {"records": [...], "source": str?}')
    source = payload.get("source")
    if source is not None and not isinstance(source, str):
        raise ValueError("'source' must be a string")
    return payload["records"], source or "api"


def dispatch(service, method: str, path: str, params: dict,
             body: bytes | None = None) -> tuple[int, dict]:
    """Route one request against *service*; returns (status, envelope)."""
    matched_route = False
    for route_method, pattern, _template, _qparams in ROUTE_TABLE:
        match = pattern.match(path)
        if not match:
            continue
        matched_route = True
        if route_method != method.upper():
            break                      # right path, wrong verb -> 405
        try:
            return _handle(service, method.upper(), match, params, body)
        except StationNotFound as exc:
            return 404, error_body("station_not_found", str(exc))
        except RunNotFound as exc:
            return 404, error_body("run_not_found", str(exc))
        except (ValueError, TypeError) as exc:
            return 400, error_body("bad_request", str(exc))
    if matched_route:
        return 405, error_body("method_not_allowed",
                               "method not allowed on this route")
    return 404, error_body("route_not_found", f"no route for {path!r}")


def _handle(service, method: str, match: re.Match, params: dict,
            body: bytes | None) -> tuple[int, dict]:
    path = match.group(0)

    def require_station(key: str) -> dict:
        row = service.get_station(key)
        if row is None:
            raise StationNotFound(f"unknown station {key!r}")
        return row

    if path == "/api/v1/health":
        version = getattr(service, "version", None)
        return 200, success_body(
            {"status": "ok", "schema_version": version})

    if path == "/api/v1/stations":
        limit = _int_param(params, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
        offset = _int_param(params, "offset", 0, 0, None)
        rows, total = service.list_stations(
            limit=limit, offset=offset, q=_first(params, "q"),
            status=_first(params, "status"), genre=_first(params, "genre"),
            format_filter=_first(params, "format"),
            country=_first(params, "country"),
            min_confidence=_min_confidence(params))
        return 200, success_body({
            "stations": [station_summary(r) for r in rows],
            "total": total, "limit": limit, "offset": offset,
        })

    if path == "/api/v1/ingest":
        records, source = _parse_ingest_body(body)
        report = service.ingest_intelligence(records, source=source)
        return 200, success_body(report.to_dict())

    if path.startswith("/api/v1/runs/"):
        run = service.get_ingestion_run(match.group("run_id"))
        if run is None:
            raise RunNotFound(f"unknown run {match.group('run_id')!r}")
        return 200, success_body(run)

    key = match.group("key")
    if path.endswith("/intelligence"):
        row = require_station(key)
        return 200, success_body(intelligence_payload(
            station=row,
            emails=service.get_station_emails(key),
            phones=service.get_station_phones(key),
            contacts=service.get_station_contacts(key),
            submission=service.get_submission(key),
            fetches=service.get_fetches(key),
        ))
    if path.endswith("/contacts"):
        row = require_station(key)
        return 200, success_body(contacts_payload(
            station=row,
            contacts=service.get_station_contacts(key),
            submission=service.get_submission(key),
        ))
    if path.endswith("/verification"):
        require_station(key)
        return 200, success_body(
            {"verification": service.get_verification(key)})
    row = require_station(key)
    return 200, success_body(station_detail(row))


def _min_confidence(params: dict):
    raw = _first(params, "min_confidence")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError("'min_confidence' must be a number") from None
    if not 0.0 <= value <= 1.0:
        raise ValueError("'min_confidence' must be between 0 and 1")
    return value
