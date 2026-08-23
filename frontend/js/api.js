/* API client for the Phase 4-6 envelope contract.
 *
 * Every call hits the REAL same-origin backend (/api/v1/*). There are no
 * mocks and no fallback fixtures: if the backend answers ok:false or is
 * unreachable, views surface the error code/message to the operator.
 *
 * Envelope: {"ok": true, "data": ...} | {"ok": false,
 *          "error": {"code": str, "message": str}}
 */

export class ApiError extends Error {
  constructor(code, message, status) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
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
  let response;
  try {
    response = await fetch(path + searchParams(params), {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    });
  } catch (error) {
    throw new ApiError("network", "API server unreachable", 0);
  }

  let envelope;
  try {
    envelope = await response.json();
  } catch (error) {
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
}

export const api = {
  health: () => request("/api/v1/health"),
  stations: (params) => request("/api/v1/stations", params),
  station: (key) =>
    request(`/api/v1/stations/${encodeURIComponent(key)}`),
  intelligence: (key) =>
    request(`/api/v1/stations/${encodeURIComponent(key)}/intelligence`),
  contacts: (key) =>
    request(`/api/v1/stations/${encodeURIComponent(key)}/contacts`),
  verification: (key) =>
    request(`/api/v1/stations/${encodeURIComponent(key)}/verification`),
};
