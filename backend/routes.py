"""Framework-free API routing shared by the stdlib servers (Phases 4-8).

Single source of truth for the stdlib HTTP surface so ``backend.api``
(read-only reference server) and ``backend.webapp`` (single-origin
operator server for the frontend) dispatch identically. The FastAPI
application (``backend.app``) remains the documented production stack;
contract tests pin all three to the same envelope and error codes.

Envelope contract (unchanged since Phase 4):

    success -> {"ok": true,  "data": <payload>, "error": null}
    failure -> {"ok": false, "data": null,
                "error": {"code": <str>, "message": <str>}}

Error codes: station_not_found | run_not_found | track_not_found |
bad_request | payload_too_large | track_rejected | route_not_found |
method_not_allowed | internal_error. Contract failures become envelopes
here; unexpected exceptions PROPAGATE so the server layer logs them and
answers internal_error.

Phase 8 note: submission orchestration is injected via ``track_store``
and ``link_fetcher`` so all servers and tests share one code path; asset
responses carry opaque ids only — never storage locations.
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

LIST_PARAMS = ("limit", "offset", "q", "status", "genre", "format",
               "country", "min_confidence")
TRACK_LIST_PARAMS = ("limit", "offset", "status")
OUTREACH_PARAMS = ("limit", "offset", "status")


class StationNotFound(Exception):
    """Unknown identity key; converted to a 404 envelope."""


class RunNotFound(Exception):
    """Unknown ingestion run id; converted to a 404 envelope."""


class TrackNotFound(Exception):
    """Unknown opaque asset id; converted to a 404 envelope."""


class OutreachNotFound(Exception):
    """Unknown outreach id; converted to a 404 envelope."""


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
    ("POST",
     re.compile(r"^/api/v1/stations/(?P<key>[^/]+)/submission/checks$"),
     "/api/v1/stations/{key}/submission/checks", ()),
    ("GET",
     re.compile(r"^/api/v1/stations/(?P<key>[^/]+)/submission/checks$"),
     "/api/v1/stations/{key}/submission/checks", ("limit",)),
    ("GET", re.compile(r"^/api/v1/stations/(?P<key>[^/]+)/submission$"),
     "/api/v1/stations/{key}/submission", ()),
    ("GET", re.compile(r"^/api/v1/stations/(?P<key>[^/]+)$"),
     "/api/v1/stations/{key}", ()),
    ("POST", re.compile(r"^/api/v1/tracks$"), "/api/v1/tracks", ("filename",)),
    ("GET", re.compile(r"^/api/v1/tracks$"), "/api/v1/tracks",
     TRACK_LIST_PARAMS),
    ("GET",
     re.compile(r"^/api/v1/tracks/(?P<track_id>sha256:[0-9a-f]{64})$"),
     "/api/v1/tracks/{track_id}", ()),
    ("POST", re.compile(r"^/api/v1/outreach$"), "/api/v1/outreach", ()),
    ("GET", re.compile(r"^/api/v1/outreach$"), "/api/v1/outreach",
     OUTREACH_PARAMS),
    ("POST", re.compile(r"^/api/v1/outreach/(?P<outreach_id>[^/]+)/event$"),
     "/api/v1/outreach/{outreach_id}/event", ()),
    ("GET",
     re.compile(r"^/api/v1/outreach/(?P<outreach_id>om_[0-9a-f]+)$"),
     "/api/v1/outreach/{outreach_id}", ()),
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


def _parse_json_body(body: bytes | None) -> dict:
    """Validate a JSON object request body; raises ValueError on bad input."""
    if not body:
        raise ValueError("body is required")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("body must be valid JSON") from None
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")
    return payload


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
             body: bytes | None = None, *, track_store=None,
             link_fetcher=None, allow_private=False) -> tuple[int, dict]:
    """Route one request against *service*; returns (status, envelope).

    ``track_store`` / ``link_fetcher`` inject the Phase 8 submission
    dependencies; adapters construct process defaults when omitted.
    """
    matched_route = False
    for route_method, pattern, _template, _qparams in ROUTE_TABLE:
        match = pattern.match(path)
        if not match:
            continue
        matched_route = True
        if route_method != method.upper():
            # another ROUTE_TABLE row may carry this verb for the same
            # path (e.g. POST + GET on /tracks) — keep scanning; if none
            # matches we fall through to 405 below.
            continue
        try:
            return _handle(service, method.upper(), match, params, body,
                           track_store=track_store,
                           link_fetcher=link_fetcher,
                           allow_private=allow_private)
        except StationNotFound as exc:
            return 404, error_body("station_not_found", str(exc))
        except RunNotFound as exc:
            return 404, error_body("run_not_found", str(exc))
        except TrackNotFound as exc:
            return 404, error_body("track_not_found", str(exc))
        except OutreachNotFound as exc:
            return 404, error_body("outreach_not_found", str(exc))
        except TrackTooLarge as exc:
            return 413, error_body("payload_too_large", str(exc))
        except TrackRejected as exc:
            return 422, error_body("track_rejected", str(exc))
        except LookupError as exc:
            # submissions.service signals unknown stations this way
            return 404, error_body("station_not_found", str(exc))
        except (ValueError, TypeError) as exc:
            return 400, error_body("bad_request", str(exc))
    if matched_route:
        return 405, error_body("method_not_allowed",
                               "method not allowed on this route")
    return 404, error_body("route_not_found", f"no route for {path!r}")


def _handle(service, method: str, match: re.Match, params: dict,
            body: bytes | None, *, track_store=None, link_fetcher=None,
            allow_private=False) -> tuple[int, dict]:
    path = match.group(0)

    def require_station(key: str) -> dict:
        row = service.get_station(key)
        if row is None:
            raise StationNotFound(f"unknown station {key!r}")
        return row

    def require_track(track_id: str) -> dict:
        row = service.get_track(track_id)
        if row is None:
            raise TrackNotFound(f"unknown track {track_id!r}")
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

    # -- Phase 8: submission assets -------------------------------------------
    if path == "/api/v1/tracks":
        if method == "POST":
            row = submission_service.upload_track(
                service, track_store, body,
                filename=_first(params, "filename"))
            return 201, success_body(track_projection(row))
        limit = _int_param(params, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
        offset = _int_param(params, "offset", 0, 0, None)
        rows, total = submission_service.list_tracks(
            service, limit=limit, offset=offset,
            status=_first(params, "status"))
        return 200, success_body(tracks_payload(rows, total, limit, offset))

    if path.startswith("/api/v1/tracks/"):
        return 200, success_body(
            track_projection(require_track(match.group("track_id"))))

    # -- Phase 9: outreach ----------------------------------------------------
    if path == "/api/v1/outreach":
        if method == "POST":
            payload = _parse_json_body(body)
            record = outreach_service.create_outreach(service, payload=payload)
            return 201, success_body(record)
        limit = _int_param(params, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
        offset = _int_param(params, "offset", 0, 0, None)
        rows, total = outreach_service.list_outreach(
            service, limit=limit, offset=offset,
            status=_first(params, "status"))
        return 200, success_body({"outreach": rows, "total": total,
                                  "limit": limit, "offset": offset})

    if path.startswith("/api/v1/outreach/") and path.endswith("/event"):
        outreach_id = match.group("outreach_id")
        if outreach_service.get_outreach(service, outreach_id) is None:
            raise OutreachNotFound(f"unknown outreach {outreach_id!r}")
        payload = _parse_json_body(body)
        event = payload.get("event")
        if not isinstance(event, str) or event not in \
                outreach_service.OUTREACH_STATUSES or event == "draft":
            raise ValueError("invalid outreach event")
        record = outreach_service.record_outreach_event(
            service, outreach_id, event=event, meta=payload.get("meta"))
        return 200, success_body(record)

    if path.startswith("/api/v1/outreach/"):
        outreach_id = match.group("outreach_id")
        if outreach_service.get_outreach(service, outreach_id) is None:
            raise OutreachNotFound(f"unknown outreach {outreach_id!r}")
        return 200, success_body(
            outreach_service.get_outreach(service, outreach_id))

    key = match.group("key")
    if path.endswith("/submission/checks"):
        require_station(key)
        if method == "POST":
            view = submission_service.run_link_checks(
                service, link_fetcher, key,
                allow_private=allow_private)
            return 200, success_body(view)
        limit = _int_param(params, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
        history = submission_service.link_history(service, key, limit=limit)
        return 200, success_body(history)
    if path.endswith("/submission"):
        require_station(key)
        last_checks = submission_service.last_checks_by_target(
            service.get_link_checks(key, limit=50))
        return 200, success_body(
            submission_view(key, service.get_submission(key), last_checks))
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
