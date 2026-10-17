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

import hmac
import json
import os
import re
import uuid
from urllib.parse import quote

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

from djs import service as dj_service

from outreach import service as outreach_service

from opportunity import service as opportunity_service

MAX_LIMIT = 200
DEFAULT_LIMIT = 50

# Development fixtures are quarantined from production display on the read
# path only (storage is never mutated and fixtures are never deleted). The
# SQL reservation lives in database.service._DEV_FIXTURE_EXCLUSION_SQL:
# geo-name seeds and reserved-TLD hosts are dev artifacts, not stations, and
# never surface in the list response.

LIST_PARAMS = ("limit", "offset", "q", "status", "genre", "format",
               "country", "min_confidence")
DJ_PARAMS = ("limit", "offset", "q", "genre", "country", "location",
             "station", "sort", "order")
# Phase 4c: independent public-web DJ discovery. This crawler independently
# seeks live candidates and is deliberately never radio-station-derived.
DJ_DISCOVER_PARAMS = ("limit", "offset", "q", "country", "location",
                      "genre", "verified_only")
TRACK_LIST_PARAMS = ("limit", "offset", "status")
OUTREACH_PARAMS = ("limit", "offset", "status")
OPPORTUNITY_PARAMS = ("track_id", "limit", "offset", "tier", "genre",
                      "country", "station_type", "has_contact", "has_route",
                      "outreach_status", "sort", "order")


class StationNotFound(Exception):
    """Unknown identity key; converted to a 404 envelope."""


class RunNotFound(Exception):
    """Unknown ingestion run id; converted to a 404 envelope."""


class TrackNotFound(Exception):
    """Unknown opaque asset id; converted to a 404 envelope."""


class OutreachNotFound(Exception):
    """Unknown outreach id; converted to a 404 envelope."""


class DjNotFound(Exception):
    """Unknown DJ id; converted to a 404 envelope."""


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
    # Authenticated operator station-field edits. Fail-closed bearer gate on
    # MIE_OPERATOR_TOKEN; writes go through set_station_overrides() only, so
    # a correction is recorded (with the displaced automated value
    # snapshotted) and survives re-ingestion. Never a way to change identity.
    ("PATCH", re.compile(r"^/api/v1/stations/(?P<key>[^/]+)/overrides$"),
     "/api/v1/stations/{key}/overrides", ()),
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
    # Create a release from metadata alone (My Music's primary workflow —
    # no binary upload). Required: title, artist_name, music_url; optional:
    # storyline, genre. Reuses the tracks table/upsert and returns the same
    # opaque projection as an upload.
    ("POST", re.compile(r"^/api/v1/tracks/releases$"),
     "/api/v1/tracks/releases", ()),
    ("GET", re.compile(r"^/api/v1/tracks$"), "/api/v1/tracks",
     TRACK_LIST_PARAMS),
    ("GET",
     re.compile(r"^/api/v1/tracks/(?P<track_id>sha256:[0-9a-f]{64})$"),
     "/api/v1/tracks/{track_id}", ()),
    ("DELETE",
     re.compile(r"^/api/v1/tracks/(?P<track_id>sha256:[0-9a-f]{64})$"),
     "/api/v1/tracks/{track_id}", ()),
    # Edit the one operator-owned presentation field on a stored release:
    # its official listening URL. JSON body {"listen_url": "https://…"} sets
    # it, an empty value clears it, and an invalid value is a 400 — the
    # backend never fabricates a link.
    ("PATCH",
     re.compile(r"^/api/v1/tracks/(?P<track_id>sha256:[0-9a-f]{64})$"),
     "/api/v1/tracks/{track_id}", ()),
    ("POST", re.compile(r"^/api/v1/outreach$"), "/api/v1/outreach", ()),
    ("GET", re.compile(r"^/api/v1/outreach$"), "/api/v1/outreach",
     OUTREACH_PARAMS),
    # Phase 13: song-story-aware AI email DRAFT. Requires an authenticated
    # operator (email session — never Google OAuth) and enforces release
    # ownership; returns a draft only, never creating an outreach record or
    # sending anything.
    ("POST", re.compile(r"^/api/v1/outreach/draft-email$"),
     "/api/v1/outreach/draft-email", ()),
    ("POST", re.compile(r"^/api/v1/outreach/(?P<outreach_id>[^/]+)/event$"),
     "/api/v1/outreach/{outreach_id}/event", ()),
    ("POST", re.compile(r"^/api/v1/outreach/(?P<outreach_id>[^/]+)/send$"),
     "/api/v1/outreach/{outreach_id}/send", ()),
    ("GET",
     re.compile(r"^/api/v1/outreach/(?P<outreach_id>om_[0-9a-f]+)$"),
     "/api/v1/outreach/{outreach_id}", ()),
    ("DELETE",
     re.compile(r"^/api/v1/outreach/(?P<outreach_id>om_[0-9a-f]+)$"),
     "/api/v1/outreach/{outreach_id}", ()),
    ("GET", re.compile(r"^/api/v1/opportunities$"), "/api/v1/opportunities",
     OPPORTUNITY_PARAMS),
    # Phase 4c: DJ discovery — a crawler POST that independently seeks
    # live public-web candidates (never radio-station-derived).
    ("POST", re.compile(r"^/api/v1/djs/discover$"),
     "/api/v1/djs/discover", DJ_DISCOVER_PARAMS),
    ("POST", re.compile(r"^/api/v1/auth/login$"),
     "/api/v1/auth/login", ()),
    ("POST", re.compile(r"^/api/v1/auth/logout$"),
     "/api/v1/auth/logout", ()),
    ("GET", re.compile(r"^/api/v1/auth/me$"),
     "/api/v1/auth/me", ()),
    ("GET", re.compile(r"^/api/v1/auth/google/start$"),
     "/api/v1/auth/google/start", ()),
    ("GET", re.compile(r"^/api/v1/auth/google/connect$"),
     "/api/v1/auth/google/connect", ()),
    ("GET", re.compile(r"^/api/v1/auth/google/callback$"),
     "/api/v1/auth/google/callback", ()),
    ("POST", re.compile(r"^/api/v1/auth/google/disconnect$"),
     "/api/v1/auth/google/disconnect", ()),
    ("PATCH", re.compile(r"^/api/v1/auth/profile$"),
     "/api/v1/auth/profile", ()),
    ("GET", re.compile(r"^/api/v1/plan$"), "/api/v1/plan", ()),
    ("GET", re.compile(r"^/api/v1/djs$"), "/api/v1/djs", DJ_PARAMS),
    ("POST", re.compile(r"^/api/v1/djs$"), "/api/v1/djs", ()),
    ("GET", re.compile(r"^/api/v1/djs/(?P<dj_id>dj_[0-9a-f]+)$"),
     "/api/v1/djs/{dj_id}", ()),
    ("DELETE", re.compile(r"^/api/v1/djs/(?P<dj_id>dj_[0-9a-f]+)$"),
     "/api/v1/djs/{dj_id}", ()),
    # Phase 11: automation discovery jobs — a scheduled, ledger-backed
    # discovery run exposed for n8n (or any scheduler). Optional shared
    # secret (MIE_AUTOMATION_TOKEN) via Authorization: Bearer; enforced
    # only when the env var is set, so local dev stays zero-config.
    ("POST", re.compile(r"^/api/v1/discovery/jobs$"),
     "/api/v1/discovery/jobs", ()),
    ("GET",
     re.compile(r"^/api/v1/discovery/jobs/(?P<run_id>job_[0-9a-f]+)$"),
     "/api/v1/discovery/jobs/{run_id}", ()),
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


_TRUE_VALUES = frozenset(("1", "true", "yes", "on"))
_FALSE_VALUES = frozenset(("0", "false", "no", "off"))


def _opt_bool(params: dict, name: str) -> bool | None:
    """Optional bool query param; None when absent, else validated."""
    raw = _first(params, name)
    if raw is None:
        return None
    if raw.lower() in _TRUE_VALUES:
        return True
    if raw.lower() in _FALSE_VALUES:
        return False
    raise ValueError(f"'{name}' must be true or false")


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


def _automation_token_ok(headers) -> bool:
    """Optional shared-secret gate for the automation endpoints.

    Returns True when ``MIE_AUTOMATION_TOKEN`` is not configured (the gate
    is off). When configured, an exactly-matching ``Authorization: Bearer``
    header must be present; comparison is constant-time and the token value
    is never logged or echoed.
    """
    expected = os.environ.get("MIE_AUTOMATION_TOKEN", "").strip()
    if not expected:
        return True
    raw = headers.get("Authorization") if headers is not None else None
    if not isinstance(raw, str):
        return False
    scheme, _, credential = raw.partition(" ")
    if scheme.strip().lower() != "bearer" or not credential.strip():
        return False
    return hmac.compare_digest(credential.strip(), expected)


def _get_current_user(service, headers):
    auth = headers.get("authorization") if headers else ""
    if not auth or not auth.startswith("Bearer "):
        auth = headers.get("Authorization") if headers else ""
    if not isinstance(auth, str) or not auth.startswith("Bearer "):
        return None
    token = auth[7:].strip()
    from auth.service import AuthService
    svc = AuthService(service)
    sess = svc.get_session(token)
    if not sess:
        return None
    return svc.get_user_by_id(sess["user_id"])


# --- browser origins (the console is hosted separately from this API) ------
# The operator console is a Vercel deployment; this API runs on Railway, so
# every browser call is cross-origin and the post-OAuth redirect must return
# the browser to the CONSOLE origin. A relative "/?oauth=..." would otherwise
# resolve against the API host and strand the freshly minted session token in
# a place the SPA never reads. The exact production console origin is allowed
# by default for the same reason it is in ``backend.app``: it is public (not a
# secret) and a missing/typo'd deploy variable must not silently break every
# request. MIE_CORS_ORIGINS adds further allowed origins (a dev console, a
# custom domain); MIE_FRONTEND_ORIGIN overrides the single redirect origin.
# Never a wildcard: the API answers Authorization-bearing requests, where "*"
# is both wrong for credentialed use and needlessly permissive.
DEFAULT_CONSOLE_ORIGINS = (
    "https://music-intelligence-engine-five.vercel.app",
)


def console_origins() -> list[str]:
    """Allowed browser origins: production default + MIE_CORS_ORIGINS.

    Trailing slashes and any query/fragment are stripped so a value like
    "https://console.example.app/" still matches the browser Origin header.
    """
    origins: list[str] = []
    for origin in [*DEFAULT_CONSOLE_ORIGINS,
                   *os.environ.get("MIE_CORS_ORIGINS", "").split(",")]:
        origin = origin.strip().split("?")[0].split("#")[0].rstrip("/")
        if origin and origin not in origins:
            origins.append(origin)
    return origins


def frontend_origin() -> str:
    """Configured origin of the browser console, for post-OAuth redirects.

    Empty (the default) keeps redirects same-origin/relative, which is correct
    when the console and API share a process (local ``backend.webapp``). The
    split production deploy sets MIE_FRONTEND_ORIGIN to the Vercel console.
    """
    return os.environ.get("MIE_FRONTEND_ORIGIN", "").strip() \
        .split("?")[0].split("#")[0].rstrip("/")


def absolute_redirect(target: str | None, *,
                      fallback_origin: str | None = None) -> str | None:
    """Rewrite a relative server-side redirect target onto the console origin.

    ``_finish_google_callback`` emits a same-origin relative target
    ("/?oauth=..."). When MIE_FRONTEND_ORIGIN is configured (split production
    console/API), prefix it so the browser lands on the console and the SPA
    can adopt the session; otherwise the relative target is returned unchanged.

    ``fallback_origin`` is an optional last resort used ONLY when
    MIE_FRONTEND_ORIGIN is unset. The API-only ASGI app (``backend.app``) has
    no static console of its own, so a relative redirect there would strand the
    browser on the API host and silently break sign-in; it passes the known
    production console origin so a missing/typo'd deploy variable degrades to
    "wrong console" rather than "no console". The stdlib server
    (``backend.webapp``) MAY co-serve the console in-process, so it leaves this
    unset and keeps relative redirects. Non-relative or empty targets pass
    through untouched.
    """
    origin = frontend_origin() or (fallback_origin or "")
    if origin and isinstance(target, str) and target.startswith("/") \
            and not target.startswith("//"):
        return origin + target
    return target


def redirect_target(body) -> str | None:
    """Relative redirect target inside a success envelope, else None.

    The Google OAuth callback finishes through the standard envelope with
    ``data.redirect``; every server layer translates that into an HTTP
    redirect so the browser's top-level OAuth hop never lands on a JSON
    screen. Only same-origin relative targets are ever emitted (the value is
    built server-side, but a path check keeps any future caller honest).
    """
    if not isinstance(body, dict) or body.get("ok") is not True:
        return None
    data = body.get("data")
    if not isinstance(data, dict):
        return None
    target = data.get("redirect")
    if isinstance(target, str) and target.startswith("/") \
            and not target.startswith("//"):
        return target
    return None


def oauth_error_target(body) -> str | None:
    """Console redirect target for a Google-callback FAILURE envelope.

    A cancelled or rejected OAuth hop reaches the callback as a top-level
    browser navigation, so answering ``ok:false`` as JSON strands the operator
    on the API host with no way back to the console ("stuck / signed out").
    This mirrors :func:`redirect_target` for the failure case: it turns the
    typed error code into a same-origin ``/?oauth=error&reason=<code>`` target
    the server layers turn into a redirect. The code is a fixed server-side
    token (``oauth_denied``/``oauth_state_invalid``/``oauth_exchange_failed``/
    ``oauth_email_missing``/``oauth_not_configured``/``bad_request``), never
    free-form provider text, so it is safe to echo in the query string.
    """
    if not isinstance(body, dict) or body.get("ok") is not False:
        return None
    detail = body.get("error")
    code = detail.get("code") if isinstance(detail, dict) else None
    if not isinstance(code, str) or not code:
        code = "oauth_failed"
    return f"/?oauth=error&reason={quote(code, safe='')}"


def _finish_google_callback(svc, service, code: str, state: str) -> tuple[int, dict]:
    """Finish a Google OAuth hop: validate+consume state, exchange the code,
    and complete the flow the state was minted for.

    Two SEPARATE flows share this callback, told apart by the state's binding:

    - Sign-in (``user_id`` NULL, minted by ``/auth/google/start``): the Google
      identity IS the operator. The callback resolves/creates their user row
      and mints a fresh session. It stores NO Gmail credentials — signing in
      is identity only.
    - Connect Gmail (``user_id`` set, minted by ``/auth/google/connect`` for a
      signed-in operator): the callback attaches the Gmail SEND credentials to
      that already-authenticated account and returns no new session.

    Either way the state row is single-use, ten-minute CSRF protection.
    Success answers a redirect target the server layers turn into a Location
    header — the SPA must NEVER see raw tokens, and the browser must never
    render a JSON screen.
    """
    bound = svc.consume_oauth_state(state)
    if bound is None:
        return 400, error_body(
            "oauth_state_invalid", "expired or reused OAuth state")
    from auth.google_oauth import GoogleOAuth
    oauth = GoogleOAuth()
    if not oauth.configured:
        return 503, error_body(
            "oauth_not_configured", "Google OAuth not configured")

    connect_gmail = bool(bound.get("user_id"))
    scopes = (GoogleOAuth.GMAIL_SEND_SCOPES if connect_gmail
              else GoogleOAuth.SIGNIN_SCOPES)
    try:
        tokens = oauth.exchange_code(code, bound.get("code_verifier"), scopes)
    except Exception as exc:  # noqa: BLE001 — surface the provider reason
        return 400, error_body("oauth_exchange_failed", str(exc))

    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials(
        token=tokens.get("access_token"),
        refresh_token=tokens.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ.get("GOOGLE_CLIENT_ID"),
        client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
    )
    oauth2 = build("oauth2", "v2", credentials=creds, cache_discovery=False)
    info = oauth2.userinfo().get().execute()
    email = (info.get("email") or "").strip().lower()
    if not email:
        return 400, error_body(
            "oauth_email_missing", "Google did not return an account email")

    from auth.service import iso_now
    now = iso_now()

    if connect_gmail:
        # Attach the send credentials to the operator who started the flow;
        # never to whichever Google identity happens to answer.
        user = svc.get_user_by_id(bound["user_id"])
        if user is None:
            return 400, error_body(
                "oauth_state_invalid",
                "the account that started this flow no longer exists")
        service.auth_save_google_account({
            "user_id": user["user_id"],
            "google_id": info.get("id"),
            "email": email,
            "picture": info.get("picture"),
            "access_token": tokens.get("access_token"),
            "refresh_token": tokens.get("refresh_token"),
            "token_expiry": tokens.get("token_expiry"),
            "scopes": ",".join(tokens.get("scopes") or scopes),
            "created_at": now,
            "updated_at": now,
        })
        return 200, success_body({"redirect": "/?oauth=connected"})

    # Sign-in: resolve/create the operator from the Google identity and hand
    # the SPA a fresh session. No Gmail credentials are stored here.
    user = svc.get_user_by_email(email) or svc.create_user(
        email, info.get("name"))
    session = svc.create_session(user["user_id"])
    return 200, success_body({
        "redirect": f"/?oauth=connected&auth={session['session_token']}"})


def _operator_token_ok(headers) -> bool:
    """Fail-closed shared-secret gate for the operator endpoints.

    Mirrors ``_automation_token_ok`` (Bearer scheme, constant-time compare,
    never logged) but INVERTS the unset-token behavior. An automation token
    that is absent disables its gate for zero-config local dev, which is fine
    for a scheduler trigger and wrong for a data-mutation endpoint: here a
    missing or empty ``MIE_OPERATOR_TOKEN`` denies every request. There is no
    branch that falls back to unauthenticated access.
    """
    expected = os.environ.get("MIE_OPERATOR_TOKEN", "").strip()
    if not expected:
        return False
    raw = headers.get("Authorization") if headers is not None else None
    if not isinstance(raw, str):
        return False
    scheme, _, credential = raw.partition(" ")
    if scheme.strip().lower() != "bearer" or not credential.strip():
        return False
    return hmac.compare_digest(credential.strip(), expected)


def dispatch(service, method: str, path: str, params: dict,
             body: bytes | None = None, *, track_store=None,
             link_fetcher=None, allow_private=False,
             discover_fetcher=None, headers=None,
             discovery_scheduler=None) -> tuple[int, dict]:
    """Route one request against *service*; returns (status, envelope).

    ``track_store`` / ``link_fetcher`` inject the Phase 8 submission
    dependencies; adapters construct process defaults when omitted.
    ``headers`` (``self.headers`` from stdlib handlers) feeds the optional
    automation-token gate. ``discovery_scheduler`` injects the background
    job executor for the automation endpoint (202 submit when present;
    legacy synchronous execution otherwise).
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
                           allow_private=allow_private,
                           discover_fetcher=discover_fetcher,
                           headers=headers,
                           discovery_scheduler=discovery_scheduler)
        except StationNotFound as exc:
            return 404, error_body("station_not_found", str(exc))
        except RunNotFound as exc:
            return 404, error_body("run_not_found", str(exc))
        except TrackNotFound as exc:
            return 404, error_body("track_not_found", str(exc))
        except OutreachNotFound as exc:
            return 404, error_body("outreach_not_found", str(exc))
        except DjNotFound as exc:
            return 404, error_body("dj_not_found", str(exc))
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
            allow_private=False, discover_fetcher=None,
            headers=None, discovery_scheduler=None) -> tuple[int, dict]:
    path = match.group(0)

    # Auth routes
    if path.startswith("/api/v1/auth"):
        from auth.service import AuthService
        svc = AuthService(service)
        if path == "/api/v1/auth/login" and method == "POST":
            payload = _parse_json_body(body) if body else {}
            email = str(payload.get("email") or "").strip().lower()
            if not email:
                raise ValueError("email required")
            user = svc.get_user_by_email(email) or svc.create_user(email)
            sess = svc.create_session(user["user_id"])
            return 200, success_body({"user": {"user_id": user["user_id"], "email": user["email"], "name": user.get("name")}, "session": sess})
        if path == "/api/v1/auth/logout" and method == "POST":
            auth = headers.get("authorization") or headers.get("Authorization") or ""
            if isinstance(auth, str) and auth.startswith("Bearer "):
                svc.revoke_session(auth[7:].strip())
            return 200, success_body({"ok": True})
        if path == "/api/v1/auth/me" and method == "GET":
            user = _get_current_user(service, headers)
            if not user:
                return 401, error_body("unauthorized", "not logged in")
            from gmail_provider import GmailProvider
            gp = GmailProvider(service)
            account = gp.connected_account(user["user_id"])
            return 200, success_body({
                "user": {
                    "user_id": user["user_id"],
                    "email": user["email"],
                    "name": user.get("name"),
                },
                "gmail_connected": account is not None,
                "gmail_email": (account.get("email") or user.get("email"))
                if account else None,
            })
        if path == "/api/v1/auth/google/start" and method == "GET":
            # Sign-in flow: identity only. Requests no Gmail scopes and mints
            # the operator's session at the callback. Anonymous by design.
            from auth.google_oauth import GoogleOAuth
            oauth = GoogleOAuth()
            if not oauth.configured:
                return 503, error_body(
                    "oauth_not_configured", "Google OAuth not configured")
            verifier = oauth.new_code_verifier()
            state = svc.create_oauth_state(None, verifier)
            auth_url, _ = oauth.get_auth_url(
                state, verifier, GoogleOAuth.SIGNIN_SCOPES)
            return 200, success_body({"auth_url": auth_url})
        if path == "/api/v1/auth/google/connect" and method == "GET":
            # Gmail-connect flow: an ALREADY signed-in operator grants the
            # Gmail SEND scope. Kept separate from sign-in so identity and
            # mailbox access are never bundled.
            user = _get_current_user(service, headers)
            if not user:
                return 401, error_body(
                    "unauthorized", "sign in before connecting Gmail")
            from auth.google_oauth import GoogleOAuth
            oauth = GoogleOAuth()
            if not oauth.configured:
                return 503, error_body(
                    "oauth_not_configured", "Google OAuth not configured")
            verifier = oauth.new_code_verifier()
            state = svc.create_oauth_state(user["user_id"], verifier)
            auth_url, _ = oauth.get_auth_url(
                state, verifier, GoogleOAuth.GMAIL_SEND_SCOPES)
            return 200, success_body({"auth_url": auth_url})
        if path == "/api/v1/auth/google/callback" and method == "GET":
            raw_state = (params.get("state") or [None])[0]
            error = (params.get("error") or [None])[0]
            if error:
                # Consent denied or the request was rejected by Google.
                return 400, error_body(
                    "oauth_denied",
                    f"Google authorization was not completed: {error}")
            if not raw_state:
                return 400, error_body(
                    "bad_request", "state required")
            code = (params.get("code") or [None])[0]
            if not code:
                return 400, error_body("bad_request", "code required")
            return _finish_google_callback(svc, service, code, raw_state)
        if path == "/api/v1/auth/google/disconnect" and method == "POST":
            # Disconnect Gmail without ending the session: the operator stays
            # signed in, but their stored Google credentials are removed so
            # outreach sends fail closed until they reconnect.
            user = _get_current_user(service, headers)
            if not user:
                return 401, error_body("unauthorized", "not logged in")
            from gmail_provider import GmailProvider
            gp = GmailProvider(service)
            disconnected = gp.disconnect_account(user["user_id"])
            return 200, success_body({
                "gmail_connected": False,
                "gmail_email": None,
                "disconnected": disconnected,
            })
        if path == "/api/v1/auth/profile" and method == "PATCH":
            # Operator-editable profile: the display name only. The email is
            # the account identity and is never editable here.
            user = _get_current_user(service, headers)
            if not user:
                return 401, error_body("unauthorized", "not logged in")
            payload = _parse_json_body(body) if body else {}
            name = payload.get("name")
            if name is not None and not isinstance(name, str):
                raise ValueError("name must be a string")
            name = (name or "").strip() or None
            updated = svc.update_user_name(user["user_id"], name)
            return 200, success_body({"user": {
                "user_id": updated["user_id"],
                "email": updated["email"],
                "name": updated.get("name"),
            }})
        return 405, error_body("method_not_allowed", "method not allowed on this route")

    if path == "/api/v1/plan" and method == "GET":
        # Truthful entitlement report. This deployment ships NO billing and
        # NO paid tiers, so there is nothing to upgrade to: every signed-in
        # operator has full access. Nothing here invents a price or a
        # feature gate. ``signed_in`` lets the profile page tailor its copy.
        user = _get_current_user(service, headers)
        return 200, success_body({
            "plan": {
                "id": "full",
                "name": "Full access",
                "billing_configured": False,
                "upgrade_available": False,
                "note": (
                    "This deployment has no billing or paid tiers configured. "
                    "Every feature is available to signed-in operators; there "
                    "is no upgrade to purchase."
                ),
            },
            "signed_in": user is not None,
        })

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
        status = _first(params, "status")
        if status is None:
            rows, total, dev_excluded, quarantined_excluded = \
                service.list_stations(
                    limit=limit, offset=offset, q=_first(params, "q"),
                    status=None, genre=_first(params, "genre"),
                    format_filter=_first(params, "format"),
                    country=_first(params, "country"),
                    min_confidence=_min_confidence(params),
                    exclude_dev=True, exclude_quarantined=True)
        else:
            # A status filter is a refinement of the SAME normal listing; it
            # must never re-admit dev fixtures or quarantined (rejected /
            # needs_review) rows that the unfiltered listing hides.
            rows, total, dev_excluded, quarantined_excluded = \
                service.list_stations(
                    limit=limit, offset=offset, q=_first(params, "q"),
                    status=status, genre=_first(params, "genre"),
                    format_filter=_first(params, "format"),
                    country=_first(params, "country"),
                    min_confidence=_min_confidence(params),
                    exclude_dev=True, exclude_quarantined=True)
        return 200, success_body({
            "stations": [station_summary(r) for r in rows],
            "total": total, "limit": limit, "offset": offset,
            "dev_fixtures_excluded": dev_excluded,
            "quarantined_excluded": quarantined_excluded,
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
            user = _get_current_user(service, headers)
            row = submission_service.upload_track(
                service, track_store, body,
                filename=_first(params, "filename"),
                owner_user_id=user["user_id"] if user else None)
            return 201, success_body(track_projection(row))
        limit = _int_param(params, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
        offset = _int_param(params, "offset", 0, 0, None)
        rows, total = submission_service.list_tracks(
            service, limit=limit, offset=offset,
            status=_first(params, "status"))
        return 200, success_body(tracks_payload(rows, total, limit, offset))

    if path == "/api/v1/tracks/releases":
        # Metadata-only release creation. A signed-in operator owns the
        # release; an anonymous operator may still create one (shared,
        # single-operator behavior preserved) exactly like an anonymous
        # upload. Validation failures map to 400 bad_request.
        user = _get_current_user(service, headers)
        payload = _parse_json_body(body)
        row = submission_service.create_release(
            service, payload,
            owner_user_id=user["user_id"] if user else None)
        return 201, success_body(track_projection(row))

    if path.startswith("/api/v1/tracks/"):
        track_id = match.group("track_id")
        track = require_track(track_id)
        if method == "DELETE":
            service.delete_track(track_id)
            return 200, success_body({"track_id": track_id})
        if method == "PATCH":
            # Ownership guard on the mutating path: a release that has an
            # owner (was uploaded by a signed-in user) may only be edited by
            # that user. Legacy/anonymous uploads keep owner_user_id NULL and
            # remain editable exactly as before.
            user = _get_current_user(service, headers)
            owner = track.get("owner_user_id")
            if owner and (not user or user["user_id"] != owner):
                return 403, error_body(
                    "forbidden", "this release belongs to another user")
            payload = _parse_json_body(body)
            row = submission_service.update_track_fields(
                service, track_id, payload)
            return 200, success_body(track_projection(row))
        return 200, success_body(track_projection(track))

    # -- Phase 9: outreach ----------------------------------------------------
    if path == "/api/v1/outreach":
        if method == "POST":
            payload = _parse_json_body(body)
            user = _get_current_user(service, headers)
            record = outreach_service.create_outreach(
                service, payload=payload,
                owner_user_id=user["user_id"] if user else None)
            return 201, success_body(record)
        limit = _int_param(params, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
        offset = _int_param(params, "offset", 0, 0, None)
        user = _get_current_user(service, headers)
        rows, total, dev_excluded = outreach_service.list_outreach(
            service, limit=limit, offset=offset,
            status=_first(params, "status"), exclude_dev=True,
            owner_user_id=user["user_id"] if user else None)
        return 200, success_body({"outreach": rows, "total": total,
                                  "limit": limit, "offset": offset,
                                  "dev_fixtures_excluded": dev_excluded})

    if path == "/api/v1/outreach/draft-email" and method == "POST":
        # An authenticated operator composes a draft from a release they are
        # authorized for. Draft-only: this never persists or sends.
        user = _get_current_user(service, headers)
        if not user:
            return 401, error_body(
                "unauthorized", "sign in to write an email with AI")
        payload = _parse_json_body(body)
        track_id = payload.get("track_id")
        if not isinstance(track_id, str) or not track_id.strip():
            raise ValueError("track_id required")
        track = service.get_track(track_id)
        if track is None:
            raise TrackNotFound(f"unknown track {track_id!r}")
        owner = track.get("owner_user_id")
        if owner and owner != user["user_id"]:
            return 403, error_body(
                "forbidden", "this release belongs to another user")
        from outreach import ai_writer
        draft = ai_writer.compose_email(
            track=track, payload=payload, client=ai_writer.default_client())
        return 200, success_body(draft)

    if path.startswith("/api/v1/outreach/") and path.endswith("/event"):
        outreach_id = match.group("outreach_id")
        user = _get_current_user(service, headers)
        if outreach_service.get_outreach(
                service, outreach_id,
                owner_user_id=user["user_id"] if user else None) is None:
            raise OutreachNotFound(f"unknown outreach {outreach_id!r}")
        payload = _parse_json_body(body)
        event = payload.get("event")
        if not isinstance(event, str) or event not in \
                outreach_service.OUTREACH_EVENTS:
            raise ValueError("invalid outreach event")
        record = outreach_service.record_outreach_event(
            service, outreach_id, event=event, meta=payload.get("meta"))
        return 200, success_body(record)

    if path.startswith("/api/v1/outreach/") and path.endswith("/send"):
        outreach_id = match.group("outreach_id")
        user = _get_current_user(service, headers)
        if not user:
            return 401, error_body("unauthorized", "not logged in")
        record = outreach_service.get_outreach(
            service, outreach_id, owner_user_id=user["user_id"])
        if record is None:
            raise OutreachNotFound(f"unknown outreach {outreach_id!r}")
        recipient = record.get("recipient") or {}
        recipient_email = recipient.get("email")
        if not recipient_email:
            return 400, error_body(
                "bad_request", "recipient has no email address; "
                "open in your email app instead")
        from gmail_provider import GmailProvider, GmailSendError
        gp = GmailProvider(service)
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
                return 400, error_body(
                    "no_connected_gmail", "Connect Gmail first")
            outreach_service.record_outreach_event(
                service, outreach_id, event="failed",
                meta={"error": str(e), "provider": "gmail_user"})
            if code == "gmail_reauth_required":
                return 400, error_body(
                    code, "Your Google authorization expired or was "
                    "revoked - reconnect Gmail to send.")
            if code == "gmail_scope_missing":
                return 403, error_body(
                    code, "Gmail send permission is missing - reconnect "
                    "Gmail and allow sending.")
            return 500, error_body("gmail_send_failed", str(e))
        except Exception as e:  # noqa: BLE001
            outreach_service.record_outreach_event(
                service, outreach_id, event="failed",
                meta={"error": str(e), "provider": "gmail_user"})
            return 500, error_body("send_failed", str(e))
        record = outreach_service.record_outreach_event(
            service,
            outreach_id,
            event="sent",
            meta={"provider": "gmail_user",
                  "message_id": res.get("message_id"),
                  "thread_id": res.get("thread_id"),
                  "sender_email": res.get("sender_email")},
        )
        return 200, success_body(record)

    if path.startswith("/api/v1/outreach/"):
        outreach_id = match.group("outreach_id")
        user = _get_current_user(service, headers)
        record = outreach_service.get_outreach(
            service, outreach_id,
            owner_user_id=user["user_id"] if user else None)
        if record is None:
            raise OutreachNotFound(f"unknown outreach {outreach_id!r}")
        if method == "DELETE":
            service.delete_outreach(outreach_id)
            return 200, success_body({"outreach_id": outreach_id})
        return 200, success_body(record)

    # -- Phase 3: opportunity intelligence -------------------------------------
    if path == "/api/v1/opportunities":
        track_id = _first(params, "track_id")
        if not track_id:
            raise ValueError("No release selected.")
        track = require_track(track_id)
        limit = _int_param(params, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
        offset = _int_param(params, "offset", 0, 0, None)
        raw_tiers = _first(params, "tier") or ""
        tiers = [t.strip().upper() for t in raw_tiers.split(",") if t.strip()]
        for tier in tiers:
            if tier not in ("HIGH", "MEDIUM", "LOW"):
                raise ValueError("'tier' must be HIGH, MEDIUM or LOW")
        outreach_status = _first(params, "outreach_status")
        if outreach_status and outreach_status not in ("new", "contacted"):
            raise ValueError("'outreach_status' must be new or contacted")
        sort = _first(params, "sort") or "score"
        order = _first(params, "order") or "desc"
        if sort not in ("score", "name", "relevance"):
            raise ValueError("'sort' must be score, name or relevance")
        if order not in ("asc", "desc"):
            raise ValueError("'order' must be asc or desc")
        payload = opportunity_service.compute_opportunities(
            service, track, limit=limit, offset=offset,
            tier=tiers or None, genre=_first(params, "genre"),
            country=_first(params, "country"),
            station_type=_first(params, "station_type"),
            has_contact=_opt_bool(params, "has_contact"),
            has_route=_opt_bool(params, "has_route"),
            outreach_status=outreach_status, sort=sort, order=order)
        return 200, success_body(payload)

    # -- Phase 11: automation discovery jobs ----------------------------------
    if path == "/api/v1/discovery/jobs":
        if method != "POST":
            return 405, error_body("method_not_allowed", "jobs requires POST")
        if not _automation_token_ok(headers):
            return 401, error_body(
                "unauthorized",
                "an Authorization: Bearer token matching MIE_AUTOMATION_TOKEN "
                "is required for automation endpoints")
        from discovery.djs.http_provider import (
            DiscoveryProviderNotConfigured,
        )
        payload = _parse_json_body(body)
        if discovery_scheduler is None:
            # Legacy synchronous contract (no scheduler wired, e.g. direct
            # test callers). Execution blocks until discovery finishes.
            from discovery.jobs import run_discovery_job
            try:
                report = run_discovery_job(
                    service, payload, fetcher=discover_fetcher)
            except DiscoveryProviderNotConfigured as exc:
                return 503, error_body("discovery_provider_not_configured",
                                       str(exc))
            data = {"job_type": f"{report['organization_type']}_discovery",
                    **report}
            return 200, success_body(data)

        # Async contract: validate + enqueue and answer 202 immediately. The
        # worker runs the same pipeline; status is polled via GET below.
        from discovery.jobs import prepare_discovery_job
        from discovery.models import utc_now_iso
        org_type, config = prepare_discovery_job(payload)  # ValueError -> 400
        run_id = "job_" + uuid.uuid4().hex[:24]
        started_at = utc_now_iso()
        discovery_scheduler.submit(run_id, org_type, config, started_at)
        return 202, success_body({
            "run_id": run_id,
            "organization_type": org_type,
            "status": "queued",
            "started_at": started_at,
        })

    if path.startswith("/api/v1/discovery/jobs/"):
        run_id = match.group("run_id")
        if not _automation_token_ok(headers):
            return 401, error_body(
                "unauthorized",
                "an Authorization: Bearer token matching MIE_AUTOMATION_TOKEN "
                "is required for automation endpoints")
        run = service.get_discovery_job(run_id)
        if run is None:
            raise RunNotFound(f"unknown discovery job {run_id!r}")
        run = {"job_type": f"{run['organization_type']}_discovery", **run}
        return 200, success_body(run)

    # -- Phase 4c: independent public-web DJ discovery -------------------------
    if path == "/api/v1/djs/discover":
        if method != "POST":
            return 405, error_body("method_not_allowed",
                                   "discover requires POST")
        try:
            from discovery.djs.selector import select_djs_discovery_provider
            from discovery.djs.pipeline import DjsDiscoveryEngine
            from discovery.models import DiscoveryRequest
        except ImportError as exc:
            return 503, error_body("discovery_dependency_missing", str(exc))
        provider = select_djs_discovery_provider(fetcher=discover_fetcher)
        if not provider.configured:
            # honest, never fabricated: no cause seed, no station derivation,
            # no auto-outreach — just an unconfigured live provider
            return 503, error_body(
                "dj_discovery_provider_not_configured",
                "DJ discovery provider is not configured; set "
                "DJS_SEARCH_BASE_URL / DJS_SEARCH_API_KEY (see .env.example)")
        payload = _parse_json_body(body)
        request = DiscoveryRequest.from_dict(payload)
        engine = DjsDiscoveryEngine(provider, fetcher=discover_fetcher)
        result = engine.run(request)
        if result.failures:
            return 502, error_body("dj_discovery_provider_error",
                                   "; ".join(str(f.message) for f in result.failures))
        report = dj_service.ingest_dj_discovery(service, result.records)
        return 200, success_body(report)

    # -- Phase 4: DJ intelligence ---------------------------------------------
    if path == "/api/v1/djs":
        if method == "POST":
            dj_payload = _parse_json_body(body)
            dj = dj_service.create_dj(service, payload=dj_payload)
            return 201, success_body(
                dj_service.dj_detail(service, dj["dj_id"]))
        limit = _int_param(params, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
        offset = _int_param(params, "offset", 0, 0, None)
        sort = _first(params, "sort") or "name"
        order = _first(params, "order") or "asc"
        if sort not in ("name", "station", "discovered"):
            raise ValueError("'sort' must be name, station or discovered")
        if order not in ("asc", "desc"):
            raise ValueError("'order' must be asc or desc")
        rows, total, dev_excluded, rejected_excluded = dj_service.list_djs(
            service, limit=limit, offset=offset, q=_first(params, "q"),
            genre=_first(params, "genre"), country=_first(params, "country"),
            location=_first(params, "location"),
            station=_first(params, "station"),
            dj_type=_first(params, "dj_type"),
            platform=_first(params, "platform"),
            has_contact=_opt_bool(params, "has_contact"),
            sort=sort, order=order, exclude_dev=True,
            exclude_rejected=True)
        return 200, success_body({
            "djs": [dj_summary(r) for r in rows],
            "total": total, "limit": limit, "offset": offset,
            "dev_fixtures_excluded": dev_excluded,
            "rejected_excluded": rejected_excluded,
        })

    if path.startswith("/api/v1/djs/"):
        dj_id = match.group("dj_id")
        detail = dj_service.dj_detail(service, dj_id)
        if detail is None:
            raise DjNotFound(f"unknown dj {dj_id!r}")
        if method == "DELETE":
            service.delete_dj(dj_id)
            return 200, success_body({"dj_id": dj_id})
        return 200, success_body(detail)

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
    if path.endswith("/overrides"):
        if not _operator_token_ok(headers):
            return 401, error_body(
                "unauthorized",
                "an Authorization: Bearer token matching MIE_OPERATOR_TOKEN "
                "is required for operator endpoints")
        try:
            payload = _parse_json_body(body)
        except ValueError as exc:
            return 400, error_body("bad_request", str(exc))
        overrides, error = validate_override_payload(payload)
        if error is not None:
            return 400, error_body("bad_request", error)
        # Returns False for an unknown identity_key; this path never creates
        # a row.
        if not service.set_station_overrides(key, overrides):
            raise StationNotFound(f"unknown station {key!r}")
        return 200, success_body(station_detail(require_station(key)))
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
