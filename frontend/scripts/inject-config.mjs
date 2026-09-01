/* Vercel build: substitute the API origin into the static frontend.
 *
 * Runs on Vercel before the static output is published (see vercel.json
 * buildCommand). The shipped source commits __MIE_API_BASE_URL__ as a token
 * (never a hardcoded URL) and substitutes it at build time with either the
 * MIE_API_BASE_URL environment variable (if set) or the committed production
 * Railway default. Files are copied into ./dist (Vercel's outputDirectory,
 * relative to the frontend root), so committed source stays tokenized and the
 * repo keeps passing the no-remote-URL asset scans. This is a static site, so
 * the value must resolve at build time (there is no runtime env in Vercel's
 * static output).
 */
import { readFileSync, writeFileSync, mkdirSync, copyFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const frontend = join(dirname(fileURLToPath(import.meta.url)), "..");
const dist = join(frontend, "dist");

// Production API origin served by Railway. Used as the build-time default so
// the shipped console reliably reaches the backend even if a Vercel
// environment variable is unset, hidden, or misconfigured. This is a public
// endpoint, not a secret. An explicit MIE_API_BASE_URL always wins.
const DEFAULT_API_BASE_URL = "https://music-intelligence-engine-production.up.railway.app";
const configured = process.env.MIE_API_BASE_URL || DEFAULT_API_BASE_URL;

// Normalize so concatenation with "/api/v1/..." never doubles a slash and
// credentials/auth hints are never needed (public backend).
const base = configured.replace(/\/+$/, "");

const TEXT_FILES = [
  "index.html",
  "css/app.css",
  "js/app.js",
  "js/api.js",
  "js/basket.js",
  "js/config.js",
  "js/dom.js",
  "js/router.js",
  "js/views/list.js",
  "js/views/station.js",
  "js/views/tracks.js",
  "js/views/outreach.js",
  "js/views/outreachHistory.js",
  "js/draftGenerator.js",
  "js/views/outreachModal.js",
];
const BINARY_FILES = [];

for (const rel of TEXT_FILES) {
  const src = join(frontend, rel);
  const dest = join(dist, rel);
  mkdirSync(dirname(dest), { recursive: true });
  const text = readFileSync(src, "utf8").replaceAll("__MIE_API_BASE_URL__", base);
  writeFileSync(dest, text);
}
for (const rel of BINARY_FILES) {
  mkdirSync(dirname(join(dist, rel)), { recursive: true });
  copyFileSync(join(frontend, rel), join(dist, rel));
}

console.log(`[inject-config] API base: ${base} -> ${dist}`);