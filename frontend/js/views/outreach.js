/* Outreach list — the action-first saved-stations list.
 *
 * Displays every station saved to the outreach basket with its best
 * available official route and a single clear action button. The page
 * must never be blank: if the list is empty it shows a helpful empty
 * state; if a recipient uid cannot be resolved the full basket is
 * rendered anyway. No tracks, no draft composer, no blocking gates —
 * only real routes the station itself publishes.
 */

import { api } from "../api.js";
import { el } from "../dom.js";
import { stationHref } from "../router.js";
import { openOutreachModal } from "./outreachModal.js";

const ROUTE_LABELS = {
  webform: "submission page",
  email: "email",
  contact: "contact page",
  dj: "DJ / program page",
};

function externalLink(url, text) {
  return el("a", {
    href: url,
    target: "_blank",
    rel: "noopener noreferrer",
  }, text ?? url);
}

function routeLabel(item) {
  if (item.route_label) return item.route_label;
  if (item.outreach_class === "webform" && item.submission_url) return "submission page";
  return ROUTE_LABELS[item.route_kind] || "station page";
}

function routeDetail(item) {
  const email = (item.email && item.email.trim()) || null;
  const url = item.submission_url || item.source_url || null;
  const parts = [routeLabel(item)];
  if (email) parts.push(email);
  if (url) parts.push(url);
  return el("div", { class: "dim outreach-route" }, parts.join(" · "));
}

/* Queue this recipient as a draft record in the outreach activity ledger.
 * The recipient payload is the exact basket evidence: a verified email XOR a
 * verified submission/contact URL — never both, never fabricated. */
function queueOutreach(item) {
  return api.createOutreach({
    recipient: {
      contact_uid: item.contact_uid,
      identity_key: item.identity_key,
      name: item.name || null,
      role: item.role || null,
      organization: item.station_name || item.name || null,
      email: (item.email && item.email.trim()) || "",
      submission_url: item.submission_url || null,
      source_url: item.source_url || null,
    },
    subject: "",
    message: "",
  });
}

function stationCard(item, basket, onRemoveAll) {
  const stationName = item.station_name || item.name || "Saved station";
  const website = item.website || null;
  const email = (item.email && item.email.trim()) || null;
  const routeUrl = item.submission_url
    || (email ? null : item.source_url) || null;
  const statusRow = el("div",
    { class: "outreach-queue-status", role: "status" });

  const head = el("div", { class: "outreach-head" },
    el("a", { class: "station-name", href: stationHref(item.identity_key) },
      stationName),
    website
      ? externalLink(website, el("span", { class: "dim" }, website))
      : null);
  const detail = el("div", { class: "outreach-detail" },
    routeDetail(item));

  const removeBtn = el("button", { class: "linkish" }, "remove");
  removeBtn.addEventListener("click", () => {
    basket.remove(item.contact_uid);
    onRemoveAll();
  });

  /* Primary hand-off: verified email opens the personalized-outreach
   * composer; a URL route opens the station's real page/form. */
  let primary;
  if (email) {
    primary = el("button", { class: "buttonish" }, "Reach out");
    primary.addEventListener("click", () => {
      openOutreachModal({
        contact_uid: item.contact_uid,
        identity_key: item.identity_key,
        name: item.name,
        role: item.role,
        station_name: item.station_name || item.name,
        email,
        source_url: item.source_url || null,
      });
    });
  } else {
    primary = externalLink(routeUrl || "#",
      el("span", { class: "buttonish" }, "Open route"));
    if (!routeUrl) primary.classList.add("dim");
  }

  const queueBtn = el("button", { class: "subtle" }, "Queue");
  queueBtn.addEventListener("click", async () => {
    if (queueBtn.disabled) return;
    queueBtn.disabled = true;
    queueBtn.textContent = "queuing…";
    try {
      const record = await queueOutreach(item);
      statusRow.replaceChildren(
        "Queued as ", el("strong", {}, record.status || "draft"),
        " — ", el("a", { class: "linkish", href: "#/outreach-history" },
          "view activity log"));
    } catch (error) {
      statusRow.replaceChildren(
        "Could not queue: ", el("strong", {},
          error && error.message ? error.message : String(error)),
        " — open the route yourself instead.");
      queueBtn.disabled = false;
      queueBtn.textContent = "Queue";
    }
  });

  const actions = el("div", { class: "actions-row" },
    primary, queueBtn,
    el("a", { class: "linkish", href: stationHref(item.identity_key) },
      "View station"),
    removeBtn);
  return el("section", { class: "card outreach-card" },
    head, detail, statusRow, actions);
}

function emptyView() {
  return [
    el("h1", {}, "Outreach list"),
    el("p", { class: "dim" },
      "Stations you add show up here with their real submission route."),
    el("section", { class: "card" },
      el("p", { class: "dim" },
        "Your outreach list is empty. Search for a station and click ",
        "\"Send music\" to get started."),
      el("div", { class: "actions-row" },
        el("a", { class: "primary", href: "#/" }, "Find stations")))
  ];
}

function fullView(basket, rerender) {
  return [
    el("h1", {}, "Outreach list"),
    el("p", { class: "dim" },
      "Stations you saved, the real route each publishes, and the queue for ",
      "your outreach activity log."),
    el("div", { class: "actions-row" },
      el("a", { class: "linkish", href: "#/outreach-history" },
        "Outreach activity log →")),
    ...basket.items.map((item) => stationCard(item, basket, rerender)),
  ];
}

/* If the basket carries no website for an item (older session data),
 * fetch the station detail once per unique identity_key and patch the
 * basket item in-place so future renders are immediate. Errors are
 * silently swallowed — the card renders without a website link. */
async function enrichWebsites(basket) {
  const missing = new Map();
  for (const item of basket.items) {
    if (item.website) continue;
    const key = item.identity_key;
    if (missing.has(key)) continue;
    missing.set(key, item);
  }
  for (const [key] of missing) {
    try {
      const detail = await api.station(key);
      const website = detail.website || null;
      if (!website) continue;
      for (const item of basket.items) {
        if (item.identity_key === key) item.website = website;
      }
    } catch (_error) {
      /* item stays without website — acceptable */
    }
  }
}

export function renderOutreachView(root, uids, basket) {
  const rerender = () => {
    root.replaceChildren(
      ...(basket.items.length > 0 ? fullView(basket, rerender) : emptyView()));
  };
  rerender();
  enrichWebsites(basket).then(rerender).catch(() => {});
}
