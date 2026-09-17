/* API client for the Phase 4-6 envelope contract.
 *
 * Every call hits the REAL same-origin backend (/api/v1/*). There are no
 * mocks and no fallback fixtures: if the backend answers ok:false or is
 * unreachable, views surface the error code/message to the operator.
 *
 * Envelope: {"ok": true, "data": ...} | {"ok": false,
 *          "error": {"code": str, "message": str}}
 */

import { API_BASE_URL } from "./config.js";

export class ApiError extends Error {
  constructor(code, message, status) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

// Human-readable origin for a request path, used so a connectivity failure
// reports WHERE the client actually tried to reach. The API origin is a
// public endpoint, never a secret or credential.
function describeOrigin(path) {
  try {
    return new URL(path, window.location.origin).origin;
  } catch (error) {
    return "unknown";
  }
}

// Every request carries a hard deadline instead of hanging forever: a stale
// keep-alive socket, a stalling proxy, or a backend that never answers must
// surface as a typed error — never leave the console on "connecting…".
const REQUEST_TIMEOUT_MS = 10000;

function deadline() {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  return { controller, timer };
}

function deadlineError(path) {
  return new ApiError(
    "timeout",
    `API request timed out after ${
      Math.round(REQUEST_TIMEOUT_MS / 1000)}s (tried ${describeOrigin(path)})`,
    0,
  );
}

function searchParams(params) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value === null || value === undefined || value === "") continue;
    query.set(key, String(value));
  }
  const asString = query.toString();
  return asString ? `?${asString}` : "";
}

export async function request(path, params) {
  const { controller, timer } = deadline();
  try {
    let response;
    try {
      response = await fetch(path + searchParams(params), {
        headers: { Accept: "application/json" },
        credentials: "same-origin",
        signal: controller.signal,
      });
    } catch (error) {
      if (error && error.name === "AbortError") {
        throw deadlineError(path);
      }
      throw new ApiError(
        "network",
        `API server unreachable (tried ${describeOrigin(path)})`,
        0,
      );
    }

    let envelope;
    try {
      envelope = await response.json();
    } catch (error) {
      if (error && error.name === "AbortError") {
        throw deadlineError(path);
      }
      throw new ApiError(
        "internal_error",
        `non-JSON response (HTTP ${response.status})`,
        response.status,
      );
    }

    if (!envelope || typeof envelope.ok !== "boolean") {
      throw new ApiError(
        "internal_error",
        "malformed envelope from API",
        response.status,
      );
    }
    if (envelope.ok) return envelope.data;

    const detail = envelope.error || {};
    throw new ApiError(
      detail.code || "internal_error",
      detail.message || "unknown error",
      response.status,
    );
  } finally {
    clearTimeout(timer);
  }
}

export const api = {
  health: () => request(API_BASE_URL + "/api/v1/health"),
  stations: (params) => request(API_BASE_URL + "/api/v1/stations", params),
  station: (key) =>
    request(API_BASE_URL + `/api/v1/stations/${encodeURIComponent(key)}`),
  intelligence: (key) =>
    request(API_BASE_URL + `/api/v1/stations/${encodeURIComponent(key)}/intelligence`),
  contacts: (key) =>
    request(API_BASE_URL + `/api/v1/stations/${encodeURIComponent(key)}/contacts`),
  verification: (key) =>
    request(API_BASE_URL + `/api/v1/stations/${encodeURIComponent(key)}/verification`),
  // Phase 8: submission assets + link accessibility. Per-track paths are
  // never constructed here; they come from each stored projection's
  // links.self value supplied by the backend.
  tracks: (params) => request(API_BASE_URL + "/api/v1/tracks", params),
  // links.self is a relative path from the backend; resolve it against the
  // configured base so a separate-origin deployment still reaches Railway.
  trackDetail: (selfPath) =>
    request(/^https?:\/\//.test(selfPath) || selfPath.startsWith("/")
      ? selfPath
      : API_BASE_URL + "/" + selfPath),
  deleteTrack: (trackId) =>
    send(API_BASE_URL + `/api/v1/tracks/${encodeURIComponent(trackId)}`,
      { method: "DELETE" }),
  stationSubmission: (key) =>
    request(API_BASE_URL + `/api/v1/stations/${encodeURIComponent(key)}/submission`),
  runSubmissionChecks: (key) =>
    send(API_BASE_URL + `/api/v1/stations/${encodeURIComponent(key)}/submission/checks`,
      { method: "POST" }),
  submissionCheckHistory: (key, params) =>
    request(API_BASE_URL + `/api/v1/stations/${encodeURIComponent(key)}/submission/checks`,
      params),
  uploadTrack: (data, filename) =>
    send(API_BASE_URL + "/api/v1/tracks",
      {
        method: "POST",
        body: data,
        headers: { "Content-Type": "audio/mpeg" },
      },
      { filename }),
  // Phase 9: outreach records/history. These POST JSON objects; send()
  // shares the envelope/typed-error conventions.
  createOutreach: (payload) =>
    send(API_BASE_URL + "/api/v1/outreach",
      { method: "POST", body: JSON.stringify(payload),
        headers: { "Content-Type": "application/json" } }),
  listOutreach: (params) =>
    request(API_BASE_URL + "/api/v1/outreach", params),
  getOutreach: (id) =>
    request(API_BASE_URL + `/api/v1/outreach/${encodeURIComponent(id)}`),
  outreachEvent: (id, event, meta) =>
    send(API_BASE_URL + `/api/v1/outreach/${encodeURIComponent(id)}/event`,
      { method: "POST", body: JSON.stringify({ event, meta: meta || null }),
        headers: { "Content-Type": "application/json" } }),
  deleteOutreach: (id) =>
    send(API_BASE_URL + `/api/v1/outreach/${encodeURIComponent(id)}`,
      { method: "DELETE" }),
  // Phase 3: opportunity intelligence for a selected release.
  opportunities: (params) =>
    request(API_BASE_URL + "/api/v1/opportunities", params),
  // Phase 4: DJ intelligence.
  djs: (params) => request(API_BASE_URL + "/api/v1/djs", params),
  dj: (id) =>
    request(API_BASE_URL + `/api/v1/djs/${encodeURIComponent(id)}`),
  createDj: (payload) =>
    send(API_BASE_URL + "/api/v1/djs",
      { method: "POST", body: JSON.stringify(payload),
        headers: { "Content-Type": "application/json" } }),
  discoverDjs: (payload) =>
    send(API_BASE_URL + "/api/v1/djs/discover",
      { method: "POST", body: JSON.stringify(payload),
        headers: { "Content-Type": "application/json" } }),
  // Phase 4c: independent public-web DJ discovery. No secrets ever sent;
  // the backend answers honestly when the provider is unconfigured.
  discoverDjs: (payload) =>
    send(API_BASE_URL + "/api/v1/djs/discover",
      { method: "POST", body: JSON.stringify(payload),
        headers: { "Content-Type": "application/json" } }),
  deleteDj: (id) =>
    send(API_BASE_URL + `/api/v1/djs/${encodeURIComponent(id)}`,
      { method: "DELETE" }),
};

/* Phase 8 addition: POST-capable companion to request(), sharing the same
 * envelope conventions and typed errors. request() above is intentionally
 * left untouched. */
export async function send(path, init, params) {
  const { controller, timer } = deadline();
  try {
    let response;
    try {
      const headers = { Accept: "application/json" };
      if (init && init.headers) Object.assign(headers, init.headers);
      response = await fetch(path + searchParams(params), {
        credentials: "same-origin",
        ...init,
        headers,
        signal: controller.signal,
      });
    } catch (error) {
      if (error && error.name === "AbortError") {
        throw deadlineError(path);
      }
      throw new ApiError(
        "network",
        `API server unreachable (tried ${describeOrigin(path)})`,
        0,
      );
    }

    let envelope;
    try {
      envelope = await response.json();
    } catch (error) {
      if (error && error.name === "AbortError") {
        throw deadlineError(path);
      }
      throw new ApiError(
        "internal_error",
        `non-JSON response (HTTP ${response.status})`,
        response.status,
      );
    }

    if (!envelope || typeof envelope.ok !== "boolean") {
      throw new ApiError(
        "internal_error",
        "malformed envelope from API",
        response.status,
      );
    }
    if (envelope.ok) return envelope.data;

    const detail = envelope.error || {};
    throw new ApiError(
      detail.code || "internal_error",
      detail.message || "unknown error",
      response.status,
    );
  } finally {
    clearTimeout(timer);
  }
}
