/* Deploy-time API base URL.
 *
 * This module lives behind a build-time token so the shipped source never
 * contains a hardcoded remote origin. When served on a separate host (e.g.
 * a static frontend on Vercel against a Railway-hosted FastAPI backend),
 * the build substitutes the real origin for __MIE_API_BASE_URL__ (see
 * scripts/inject-config.mjs / vercel.json). Until then the token is left
 * untouched and the client falls back to same-origin relative requests,
 * preserving the historical single-origin behavior.
 */

const TOKEN = "__MIE_API_BASE_URL__";

export const API_BASE_URL = TOKEN.startsWith("__") ? "" : TOKEN;
