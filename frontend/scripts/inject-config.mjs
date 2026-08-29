/* Vercel build: substitute the API origin into the static frontend.
 *
 * Runs on Vercel before the static output is published (see vercel.json
 * buildCommand). The shipped source commits __MIE_API_BASE_URL__ as a token
 * (never a hardcoded URL) and substitutes it with the MIE_API_BASE_URL
 * environment variable here. Files are copied into frontend/dist, which is
 * Vercel's outputDirectory, so committed source stays tokenized and the
 * repo keeps passing the no-remote-URL asset scans.
 *
 * When MIE_API_BASE_URL is unset/empty the token is left in place and the
 * client falls back to same-origin requests (local/single-origin use).
 */
import { readFileSync, writeFileSync, mkdirSync, copyFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const frontend = join(dirname(fileURLToPath(import.meta.url)), "..");
const dist = join(frontend, "dist");
const base = process.env.MIE_API_BASE_URL || "";

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

console.log(
  `[inject-config] API base: ${base ? base : "(none; same-origin)"} -> ${dist}`,
);
