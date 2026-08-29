/* Deploy-time API base URL.
 *
 * This module lives behind a build-time token so the shipped source never
 * contains a hardcoded remote origin. At deploy (see scripts/inject-config.mjs
 * / vercel.json) the token is replaced with the resolved backend origin:
 * MIE_API_BASE_URL if explicitly set, otherwise the committed Railway production
 * default. Until substituted (local single-origin serving, tests) the token is
 * left untouched and the client falls back to same-origin relative requests.
 */

const TOKEN = "__MIE_API_BASE_URL__";

export const API_BASE_URL = TOKEN.startsWith("__") ? "" : TOKEN;
