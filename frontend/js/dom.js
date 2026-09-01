/* Minimal DOM helpers.
 *
 * Security model: dynamic data NEVER becomes markup. HTML strings are not
 * built anywhere in this application — values are attached as text nodes
 * or set as attribute values, so untrusted payload content cannot inject
 * elements. Event handlers are attached with addEventListener
 * (CSP-friendly); there are no inline handlers.
 * createDownload() is the single audited place that mints an element
 * outside el(), because anchor downloads need a detached node. */

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "class") {
      node.className = value;
    } else if (name === "dataset") {
      Object.assign(node.dataset, value);
    } else if (name.startsWith("on") && typeof value === "function") {
      node.addEventListener(name.slice(2).toLowerCase(), value);
    } else if (value === true) {
      node.setAttribute(name, "");
    } else {
      node.setAttribute(name, String(value));
    }
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function confidenceBar(value) {
  const pct = typeof value === "number" ? Math.round(value * 100) : null;
  const fill = el("span");
  if (pct !== null) fill.style.width = `${Math.max(0, Math.min(100, pct))}%`;
  return el("span", { class: "confidence", title: `confidence ${pct === null ? "unknown" : pct + "%"}` }, fill);
}

export const fmtPct = (value) =>
  typeof value === "number" ? `${Math.round(value * 100)}%` : "—";

export const fmtList = (list) =>
  Array.isArray(list) && list.length ? list.join(", ") : "—";

export function chips(items, extraClass) {
  return el(
    "span",
    { class: "chips" },
    (items || []).map((item) => el("span", { class: extraClass ? `chip ${extraClass}` : "chip" }, item)),
  );
}

/* Trigger a client-side JSON file download (recipient export). */
export function createDownload(filename, text) {
  const blob = new Blob([text], { type: "application/json" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = filename;
  link.click();
  URL.revokeObjectURL(link.href);
}
