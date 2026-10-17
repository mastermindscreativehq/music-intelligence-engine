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
// surface as a typed error — never leave the console on "connecting…". The
// deadline is a ceiling for genuinely-slow-but-valid reads (the Railway DJ
// listing can take several seconds under cold-start; a fixed 10s deadline
// turned those into user-visible false timeouts).
const REQUEST_TIMEOUT_MS = 30000;

// Transient transport failures (connection stall, edge reset, or a deadline
// reached while the backend was still working) are retried once with a short
// backoff — idempotent GET reads only. Mutating calls via send() are NEVER
// retried, so an outreach/ingest action can never be applied twice.
const RETRY_ATTEMPTS = 1;
const RETRY_BACKOFF_MS = 700;

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

function authToken() {
  try {
    return localStorage.getItem("mie_auth_token") || "";
  } catch (error) {
    return "";
  }
}

function setAuthToken(token) {
  try {
    if (token) {
      localStorage.setItem("mie_auth_token", token);
    } else {
      localStorage.removeItem("mie_auth_token");
    }
  } catch (error) {
    /* ignore */
  }
}

// Adopt the session handed back by the Google OAuth redirect. The callback
// lands the browser on "/?oauth=connected[&auth=<token>]"; we store the
// token and strip it from the address bar (history.replaceState) so it never
// lingers in the URL, browser history, or a shared link. Returns a small
// descriptor so boot code can route the operator back to the composer.
export function adoptAuthFromUrl() {
  let url;
  try {
    url = new URL(window.location.href);
  } catch (error) {
    return { adopted: false, oauth: null, reason: null };
  }
  const token = url.searchParams.get("auth") || "";
  const oauth = url.searchParams.get("oauth");
  const reason = url.searchParams.get("reason");
  if (token) {
    setAuthToken(token);
  }
  if (token || oauth) {
    url.searchParams.delete("auth");
    url.searchParams.delete("oauth");
    url.searchParams.delete("reason");
    const cleaned = url.pathname + (url.search || "") + url.hash;
    try {
      window.history.replaceState(null, "", cleaned);
    } catch (error) {
      /* ignore */
    }
  }
  return { adopted: Boolean(token), oauth, reason };
}

function mergeHeaders(base, extra) {
  // The bearer is attached for EVERY request, not only when an `extra` map is
  // supplied. The earlier `if (!extra) return base;` early-return skipped this
  // block for plain GET reads (e.g. /api/v1/auth/me), so an adopted session
  // token was stored but never sent and every authenticated read 401'd.
  const h = new Headers(base || {});
  if (extra) {
    for (const [k, v] of Object.entries(extra)) {
      if (v === null || v === undefined) continue;
      h.set(k, v);
    }
  }
  const token = authToken();
  if (token) {
    h.set("Authorization", "Bearer " + token);
  }
  return h;
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

async function requestOnce(path, params) {
  const { controller, timer } = deadline();
  try {
    let response;
    try {
      response = await fetch(path + searchParams(params), {
        headers: mergeHeaders({ Accept: "application/json" }),
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

export async function request(path, params) {
  let lastError;
  for (let attempt = 0; attempt <= RETRY_ATTEMPTS; attempt++) {
    if (attempt > 0) {
      await new Promise((resolve) => setTimeout(resolve, RETRY_BACKOFF_MS));
    }
    try {
      return await requestOnce(path, params);
    } catch (error) {
      if (!(error instanceof ApiError)
          || (error.code !== "network" && error.code !== "timeout")) {
        throw error;
      }
      lastError = error;
    }
  }
  throw lastError;
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
  // links.self is a backend-relative path supplied by the API projection.
  // Resolve it against the configured API base so a separate-origin
  // deployment (frontend on Vercel, backend on Railway) reaches the API
  // instead of the static frontend origin. Only a fully-qualified URL is
  // used verbatim.
  trackDetail: (selfPath) =>
    request(/^https?:\/\//.test(selfPath)
      ? selfPath
      : API_BASE_URL
        + (selfPath.startsWith("/") ? selfPath : "/" + selfPath)),
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
  // Phase 14: create a release from metadata alone (no binary upload) — the
  // primary My Music workflow. The backend validates every field and derives
  // the opaque track id; nothing is invented here.
  createRelease: (payload) =>
    send(API_BASE_URL + "/api/v1/tracks/releases",
      {
        method: "POST",
        body: JSON.stringify(payload),
        headers: { "Content-Type": "application/json" },
      }),
  // Phase 14: edit a release's operator-owned metadata (title, artist_name,
  // genre) alongside its listening URL / song story. Any subset is accepted;
  // the backend validates before writing.
  updateTrackRelease: (trackId, payload) =>
    send(API_BASE_URL + `/api/v1/tracks/${encodeURIComponent(trackId)}`,
      {
        method: "PATCH",
        body: JSON.stringify(payload),
        headers: { "Content-Type": "application/json" },
      }),
  // Edit the release's official listening URL. An empty string clears it;
  // the backend validates and refuses anything that is not an absolute
  // http(s) URL, so a bad link surfaces as a typed error, never a silent fix.
  updateTrackListenUrl: (trackId, listenUrl) =>
    send(API_BASE_URL + `/api/v1/tracks/${encodeURIComponent(trackId)}`,
      {
        method: "PATCH",
        body: JSON.stringify({ listen_url: listenUrl }),
        headers: { "Content-Type": "application/json" },
      }),
  // Phase 13: edit the release's song story / creative brief — the
  // operator's own account of the song, used as source material for
  // outreach. An empty string clears it; it is never inferred.
  updateTrackSongStory: (trackId, songStory) =>
    send(API_BASE_URL + `/api/v1/tracks/${encodeURIComponent(trackId)}`,
      {
        method: "PATCH",
        body: JSON.stringify({ song_story: songStory }),
        headers: { "Content-Type": "application/json" },
      }),
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
  sendOutreach: (id) =>
    send(API_BASE_URL + `/api/v1/outreach/${encodeURIComponent(id)}/send`,
      { method: "POST" }),
  // Phase 13: song-story-aware outreach DRAFT. Requires a signed-in operator
  // (email session). Draft ONLY — never creates a record and never sends.
  composeEmail: (payload) =>
    send(API_BASE_URL + "/api/v1/outreach/draft-email",
      { method: "POST", body: JSON.stringify(payload),
        headers: { "Content-Type": "application/json" } }),
  // Auth
  login: async (email) => {
    const res = await send(API_BASE_URL + "/api/v1/auth/login",
      { method: "POST", body: JSON.stringify({ email }),
        headers: { "Content-Type": "application/json" } });
    if (res && res.session && res.session.session_token) {
      setAuthToken(res.session.session_token);
    }
    return res;
  },
  me: () => request(API_BASE_URL + "/api/v1/auth/me"),
  // Operator-editable display name. The email is the sign-in identity and is
  // never changed here; an empty string clears the name.
  updateProfile: (name) =>
    send(API_BASE_URL + "/api/v1/auth/profile",
      { method: "PATCH", body: JSON.stringify({ name }),
        headers: { "Content-Type": "application/json" } }),
  // Truthful entitlement report for this deployment (no billing is
  // configured; every signed-in operator has full access).
  plan: () => request(API_BASE_URL + "/api/v1/plan"),
  logout: async () => {
    // Revoke server-side FIRST (send() attaches the current bearer), then
    // drop the local token — clearing first would strand the session.
    try {
      return await send(API_BASE_URL + "/api/v1/auth/logout",
        { method: "POST" });
    } finally {
      setAuthToken(null);
    }
  },
  // Sign-in only (openid / email / profile). Never requests Gmail scopes.
  googleStart: () => request(API_BASE_URL + "/api/v1/auth/google/start"),
  // Separate, authenticated Gmail-connect flow: requests the Gmail send
  // scope only, and only for an already signed-in operator.
  googleConnect: () =>
    request(API_BASE_URL + "/api/v1/auth/google/connect"),
  disconnectGmail: () =>
    send(API_BASE_URL + "/api/v1/auth/google/disconnect",
      { method: "POST" }),
  isLoggedIn: () => Boolean(authToken()),
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
  deleteDj: (id) =>
    send(API_BASE_URL + `/api/v1/djs/${encodeURIComponent(id)}`,
      { method: "DELETE" }),
  setAuthToken,
  adoptAuthFromUrl,
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
      const token = authToken();
      if (token) headers.Authorization = "Bearer " + token;
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
