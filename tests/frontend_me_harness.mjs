/* Behavioral harness: run the REAL frontend api.js under Node with minimal
 * browser shims, adopt a session token from an OAuth-return URL (a successful
 * Google sign-in), then call api.me() and report the Authorization header the
 * client actually produced. Node is used only as a stand-in for the browser
 * runtime; no network is touched (fetch is stubbed).
 *
 * Env: MIE_TEST_API_JS (absolute path to api.js), MIE_TEST_TOKEN (session
 * token), MIE_TEST_ORIGIN (optional console origin).
 */
import { pathToFileURL } from "node:url";

const apiPath = process.env.MIE_TEST_API_JS;
const token = process.env.MIE_TEST_TOKEN || "";
const origin = process.env.MIE_TEST_ORIGIN || "https://console.example";

if (!apiPath) {
  console.error("MIE_TEST_API_JS is not set");
  process.exit(2);
}

globalThis.window = {
  location: {
    href: `${origin}/?oauth=connected&auth=${encodeURIComponent(token)}`,
    origin,
  },
  history: { replaceState() {} },
};

globalThis.localStorage = {
  _data: {},
  getItem(key) {
    return Object.prototype.hasOwnProperty.call(this._data, key)
      ? this._data[key]
      : null;
  },
  setItem(key, value) {
    this._data[key] = String(value);
  },
  removeItem(key) {
    delete this._data[key];
  },
};

const calls = [];
globalThis.fetch = async (url, options) => {
  calls.push({
    url: String(url),
    headers: options && options.headers,
    credentials: options && options.credentials,
  });
  return {
    status: 200,
    json: async () => ({ ok: true, data: { user: { user_id: "u1" } } }),
  };
};

const mod = await import(pathToFileURL(apiPath).href);
const adopted = mod.api.adoptAuthFromUrl();
await mod.api.me();

const first = calls[0] || {};
const header = new Headers(first.headers || {});
console.log(JSON.stringify({
  adopted: adopted.adopted,
  storedToken: globalThis.localStorage.getItem("mie_auth_token"),
  requestUrl: first.url,
  authorization: header.get("Authorization"),
  credentials: first.credentials,
}));
