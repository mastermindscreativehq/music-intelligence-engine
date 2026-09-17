/* Station page — the primary outreach intake (stabilized 2026-09).
 *
 * Canonical structure for every station:
 *   1. STATION PROFILE          overview (name, site, location, genres)
 *   2. SUBMISSION INFORMATION   evidence-backed submission routes + pages
 *   3. CONTACTS                 music decision-makers, ONE action each:
 *                               "Add to outreach" (stage for this station)
 *   4. OUTREACH                 staged recipients + release selector +
 *                               "Start outreach" which creates a real
 *                               outreach RECORD per recipient (POST /outreach)
 *
 * Everything rendered comes verbatim from the backend endpoints; this view
 * adds presentation only. URLs are never fabricated: an action exists ONLY
 * when the backend stored the exact route. The word "campaign" is gone —
 * the canonical unit is the outreach record (music + station + contact). */

import { api, ApiError } from "../api.js";
import {
  chips,
  el,
  fmtList,
  fmtPct,
} from "../dom.js";
import { outreachHref } from "../router.js";
import { stationLocation } from "./stationLocation.js";
import {
  contactStatusChip,
  stationResearchChip,
  submissionStatusChip,
} from "./status.js";

const STATUS_CLASSES = ["verified", "conflicting", "failed", "stale",
  "unverified", "unsupported", "enriched", "new", "broken"];

const unsubscribeFns = [];

function errorBanner(error) {
  const detail = error instanceof ApiError
    ? `${error.code}: ${error.message}`
    : String(error);
  return el("div", { class: "banner-error", role: "alert" },
    "Could not load this station. ", el("strong", {}, detail));
}

function externalLink(url, text) {
  return el("a", {
    href: url,
    target: "_blank",
    rel: "noopener noreferrer",
  }, text ?? url);
}

/* ---------------------------------------------------------------------------
 * Section 1 — STATION PROFILE
 * ------------------------------------------------------------------------- */

function overviewSection(detail) {
  const location = stationLocation(detail) || null;
  return el("section", { class: "card detail-head", id: "station-profile" },
    el("p", { class: "dim section-label" }, "Station profile"),
    el("h1", {}, detail.name || "(unnamed station)"),
    el("div", { class: "overview-row" },
      detail.website
        ? externalLink(detail.website, detail.domain ?? detail.website)
        : el("span", { class: "dim" }, "no website on record"),
      detail.website && location ? el("span", {}, ` · ${location}`) : null),
    el("div", { class: "overview-row" },
      stationResearchChip(detail.research_status),
      " ", chips(detail.genres), " ", chips(detail.formats)));
}

/* ---------------------------------------------------------------------------
 * Useful-page helpers
 * ------------------------------------------------------------------------- */

function normalizePageUrl(raw) {
  try {
    const u = new URL(raw);
    let path = u.pathname.replace(/\/+$/, "") || "/";
    return `${u.protocol}//${u.hostname}${u.port ? ":" + u.port : ""}${path}`;
  } catch (error) {
    return String(raw || "").trim();
  }
}

function submissionPages(usefulPages) {
  return (usefulPages || []).filter((p) => p
    && typeof p.url === "string" && /^https?:\/\//i.test(p.url)
    && (p.category === "send_music"
      || p.category === "submission_guidelines"));
}

/* The single best Send Music route: the canonical backend submission_url
 * Fact first; otherwise the best discovered submission-classed useful page.
 * Returns {url, label, source, kind} or null. Never constructs a route. */
function bestSubmissionRoute(intel, usefulPages) {
  const canonical = intel && intel.submission && intel.submission.submission_url;
  if (canonical && canonical.value
    && /^https?:\/\//i.test(String(canonical.value))) {
    return {
      url: canonical.value,
      label: canonical.source_type === "official_website_page"
        ? "official submission page"
        : "verified submission page",
      source: canonical,
      verified: canonical.verified === true,
      kind: "webform",
    };
  }
  const page = submissionPages(usefulPages)[0] || null;
  if (page) {
    return {
      url: page.url,
      label: page.label || "submission page",
      source: page,
      verified: page.reachable === true,
      kind: "webform",
    };
  }
  return null;
}

/* When a station publishes no submission page, its official contact page (or
 * program/DJ page) is the honest available route. Same shape as
 * bestSubmissionRoute. Never constructs a route. */
function fallbackStationRoute(usefulPages) {
  const contact = bestOfCategory(usefulPages, "contact");
  const djDirectory = bestOfCategory(usefulPages, "dj_directory")
    || bestOfCategory(usefulPages, "programming");
  const page = contact || djDirectory;
  if (!page) return null;
  return {
    url: page.url,
    label: page.label || "station contact page",
    source: page,
    verified: page.reachable === true,
    kind: contact ? "contact" : "dj",
  };
}

const JUNK_LABEL = /donate|sponsor|advertis|newsletter|press|blog|merch|volunteer|news\b|event|calendar|plan.?your|archive|podcast|episode|playlist|staff[\s_-]?favorites|keywords|settings|login|sign\s?in/i;
const PROFILE_URL = /(\/profile|\/artists?|\/keywords|email\.php|wp-login|mailchimp)/i;

function usablePage(p, category) {
  if (!p || typeof p.url !== "string" || !/^https?:\/\//i.test(p.url)) return null;
  if (p.category !== category) return null;
  const label = String(p.label || "").trim();
  if (JUNK_LABEL.test(label) || PROFILE_URL.test(p.url)) return null;
  if (label.toLowerCase() === "here") return null;
  if (category === "dj_directory") {
    if (/^\s*(view\s+)?(dj|deejay)[\s']/i.test(label)) return null;
    if (/with\b/i.test(label)) return null;
    if (/\/playlists?[\/?#]|\/(archives?|schedule)[\/?#]/i.test(p.url)) return null;
    return label.length > 2 ? p : null;
  }
  if (category === "contact") {
    return label.length > 2 ? p : null;
  }
  if (category === "programming") {
    return label.length > 2 ? p : null;
  }
  return p;
}

/* Canonical submission route present -> no duplicate Send Music page row. */
function submissionPageSuppressed(canonicalRoute) {
  return Boolean(canonicalRoute);
}

const USEFUL_PRIORITY = [
  "send_music", "submission_guidelines", "dj_directory", "programming",
  "contact",
];
const USEFUL_LABELS = {
  send_music: "Send music",
  submission_guidelines: "Send music",
  dj_directory: "DJ directory",
  programming: "Programming",
  contact: "Contact",
};

function bestOfCategory(pages, category) {
  const candidates = (pages || [])
    .map((p) => usablePage(p, category))
    .filter(Boolean);
  const score = (c) => (c.reachable === true ? 0 : c.reachable === false ? 2 : 1);
  candidates.sort((a, b) => score(a) - score(b) || b.label.length - a.label.length);
  return candidates[0] || null;
}

function curatedUsefulPages(usefulPages, canonicalRoute) {
  const seen = new Set();
  const unique = [];
  for (const p of (usefulPages || [])) {
    if (!p || typeof p.url !== "string") continue;
    const key = normalizePageUrl(p.url);
    if (seen.has(key)) continue;
    seen.add(key);
    unique.push(p);
  }
  const suppressed = submissionPageSuppressed(canonicalRoute);
  const rows = [];
  for (const category of USEFUL_PRIORITY) {
    if (suppressed && (category === "send_music"
      || category === "submission_guidelines")) continue;
    if (rows.some((r) => r.category === category)) continue;
    const best = bestOfCategory(unique, category);
    if (best) rows.push({ best, category });
    if (rows.length >= 3) break;
  }
  return rows;
}

function usefulPageRow(p) {
  return el("div", { class: "up-row" },
    el("div", { class: "up-main" },
      externalLink(p.url, el("span", { class: "up-label" }, p.label || p.url)),
      el("span", { class: "dim up-url" }, normalizePageUrl(p.url))),
    p.why || p.next_step
      ? el("div", { class: "dim up-why" },
        [p.why, p.next_step].filter(Boolean).join(" "))
      : null,
    el("span", { class: "dim up-meta" }, USEFUL_LABELS[p.category] || p.category));
}

/* ---------------------------------------------------------------------------
 * Section 2 — SUBMISSION INFORMATION (evidence only; no staging here)
 * ------------------------------------------------------------------------- */

function submissionInformationCard(detail, intel, usefulPages, contactsPayload) {
  const website = detail.website || detail.domain || null;
  const route = bestSubmissionRoute(intel, usefulPages);
  const ranked = rankedContacts((contactsPayload && contactsPayload.contacts) || []);
  const emailContact = ranked.find((c) => verifiedEmail(c));
  const contactPage = bestOfCategory(usefulPages, "contact");
  const djDirectory = bestOfCategory(usefulPages, "dj_directory");
  const status = String(intel.submission_status || "");
  const backendAction = intel.best_action || null;

  const tiles = [];

  if (backendAction && backendAction.kind === "submit" && backendAction.url) {
    tiles.push(externalLink(backendAction.url,
      el("span", { class: "action-tile primary-tile" },
        el("strong", {}, backendAction.label || "Send music"),
        el("span", { class: "dim action-sub" },
          backendAction.detail || "submission page on station site"))));
  } else if (route) {
    tiles.push(externalLink(route.url,
      el("span", { class: "action-tile primary-tile" },
        el("strong", {}, route.verified ? "Verified submission route" : "Submission page"),
        el("span", { class: "dim action-sub" }, route.label))));
  } else if (backendAction
    && (backendAction.kind === "contact" || backendAction.kind === "browse")
    && backendAction.url) {
    tiles.push(externalLink(backendAction.url,
      el("span", { class: "action-tile primary-tile" },
        el("strong", {}, "Submission information found"),
        el("span", { class: "dim action-sub" },
          backendAction.reason || backendAction.label))));
  } else if (backendAction && backendAction.kind === "contact"
    && backendAction.detail) {
    tiles.push(el("span", { class: "action-tile action-muted" },
      el("strong", {}, backendAction.label),
      el("span", { class: "dim action-sub" }, backendAction.detail)));
  } else {
    tiles.push(el("span", { class: "action-tile action-muted" },
      el("strong", {}, "Send music"),
      el("span", { class: "dim action-sub" },
        status === "NO_PUBLIC_SUBMISSION_ROUTE_FOUND"
          ? "No public submission route found after investigation."
          : "No verified submission route found.")));
  }

  if (emailContact) {
    tiles.push(externalLink(`mailto:${emailContact.email}`,
      el("span", { class: "action-tile" },
        el("strong", {}, contactActionLabel(emailContact)),
        el("span", { class: "dim action-sub" }, emailContact.email))));
  } else if (contactPage
    && !(backendAction && backendAction.kind === "browse"
      && backendAction.url === contactPage.url)) {
    tiles.push(externalLink(contactPage.url,
      el("span", { class: "action-tile" },
        el("strong", {}, "Contact station"),
        el("span", { class: "dim action-sub" },
          contactPage.label || "station contact page"))));
  }

  if (djDirectory
    && !(backendAction && backendAction.kind === "browse"
      && backendAction.url === djDirectory.url)) {
    tiles.push(externalLink(djDirectory.url,
      el("span", { class: "action-tile" },
        el("strong", {}, "DJ directory"),
        el("span", { class: "dim action-sub" },
          djDirectory.label || "DJ directory"))));
  }

  if (website) {
    tiles.push(externalLink(website,
      el("span", { class: "action-tile" },
        el("strong", {}, "Visit station"),
        el("span", { class: "dim action-sub" }, detail.domain ?? website))));
  }

  return el("section", { class: "card action-bar", id: "station-actions" },
    el("p", { class: "dim section-label" }, "Submission information"),
    el("h2", {}, "How to send your music"),
    el("p", { class: "dim" },
      "Real submission routes and contact points this station actually ",
      "publishes. Use these below when you prepare outreach."),
    el("div", { class: "chips" },
      submissionStatusChip(status),
      " ",
      contactStatusChip(emailContact, contactPage)),
    el("div", { class: "action-grid" }, tiles),
    usefulPagesCard(usefulPages, route));
}

function usefulPagesCard(usefulPages, canonicalRoute) {
  const rows = curatedUsefulPages(usefulPages, canonicalRoute);
  const suppressedSubmission = submissionPageSuppressed(canonicalRoute);
  return el("div", { class: "card up-pages-card" },
    el("h3", {}, "Pages on this station's site"),
    el("p", { class: "dim" },
      "Submission, DJ, programming, and contact pages we found on the ",
      "station's own website."),
    rows.length
      ? el("div", { class: "up-list" },
        rows.map((r) => usefulPageRow(r.best)))
      : el("p", { class: "dim" },
        suppressedSubmission
          ? "No additional pages beyond the submission route already shown."
          : "No submission or contact pages were found."));
}

function contactActionLabel(contact) {
  const role = String(contact.role || "").toLowerCase();
  if (role === "music_director") return "Contact music director";
  if (role === "program_director") return "Contact program director";
  if (role.startsWith("music_")) return "Contact music department";
  return "Contact music department";
}

/* ---------------------------------------------------------------------------
 * Section 3 — CONTACTS (music decision-makers, one "Add to outreach" action)
 * ------------------------------------------------------------------------- */

const DECISION_ROLE_RANK = {
  music_director: 0,
  program_director: 1,
  music_programmer: 2,
  music_submission: 3,
  programming: 4,
  music_scheduler: 5,
  music_coordinator: 6,
};
const MORE_RELEVANT_ROLES = {
  host: 7,
  dj: 8,
};

function normRole(contact) {
  return String(contact.role || "").trim().toLowerCase();
}

function verifiedEmail(contact) {
  const email = String(contact.email || "").trim();
  return email || null;
}

function roleGrade(role) {
  if (role in DECISION_ROLE_RANK) return DECISION_ROLE_RANK[role];
  if (role in MORE_RELEVANT_ROLES) return MORE_RELEVANT_ROLES[role];
  return 99;
}

function isKeyContact(contact) {
  return Boolean(contact.preferred_for_submissions)
    || normRole(contact) in DECISION_ROLE_RANK;
}

function isMoreRelevantContact(contact) {
  return normRole(contact) in MORE_RELEVANT_ROLES && Boolean(verifiedEmail(contact));
}

function rankedContacts(contacts) {
  return (contacts || []).filter(Boolean).sort((a, b) => {
    const ga = roleGrade(normRole(a));
    const gb = roleGrade(normRole(b));
    if (ga !== gb) return ga - gb;
    const pa = Number(Boolean(a.preferred_for_submissions));
    const pb = Number(Boolean(b.preferred_for_submissions));
    if (pa !== pb) return pb - pa;
    const ea = Number(Boolean(verifiedEmail(a)));
    const eb = Number(Boolean(verifiedEmail(b)));
    if (ea !== eb) return eb - ea;
    return (b.confidence_score || 0) - (a.confidence_score || 0);
  });
}

/* A key contact is selectable when ANY sendable route exists through the
 * backend contract: their own verified email, or — when the station
 * publishes no email decision-maker — a verified official station route. */
function contactActionable(contact) {
  if (typeof contact.can_add_to_campaign === "boolean") {
    return contact.can_add_to_campaign;
  }
  return Boolean(verifiedEmail(contact));   // pre-annotation safety fallback
}

function roleTitle(role) {
  if (!role || role === "unknown") return null;
  return String(role).replace(/_/g, " ");
}

const ROUTE_KIND_LABELS = {
  email: "email",
  webform: "submission page",
  contact: "contact page",
  phone: "phone",
  dj: "DJ / program page",
};

function routeKindLabel(kind) {
  return ROUTE_KIND_LABELS[kind] || kind || "route";
}

/* Stage ONE recipient for a single key contact (the canonical "Add to
 * outreach" action). The recipient always points at a real, station-published
 * route: the person's own verified email when recorded, otherwise the best
 * verified station route. Never fabricates an address. */
function stageKeyContact(contact, payload, identityKey, basket) {
  const uid = String(contact.contact_uid);
  if (basket.has(uid)) return false;
  const email = verifiedEmail(contact);
  const best = contact.best_outreach_route || null;
  const routeUrl = best && /^https?:\/\//i.test(String(best.value || ""))
    ? best.value : null;
  const kind = email ? "email"
    : (best && best.kind) || "webform";
  return basket.add({
    contact_uid: uid,
    identity_key: identityKey,
    station_name: payload.station_name,
    name: contact.name,
    role: contact.role,
    email: email || "",
    source_url: contact.source_url || null,
    outreach_class: email ? "email" : kind,
    submission_url: routeUrl,
    route_kind: kind,
    route_label: email ? null : (best && best.title)
      || routeKindLabel(kind),
  });
}

function keyContactCard(contact, payload, identityKey, basket, onStagedChange) {
  const uid = String(contact.contact_uid);
  const email = verifiedEmail(contact);
  const title = contact.name
    || roleTitle(contact.role)
    || "(unnamed contact)";
  const foundOn = contact.source_url
    || ((contact.sources && contact.sources[0]) || null);
  const best = contact.best_outreach_route || null;
  const routes = contact.outreach_routes || [];
  const relevance = contact.relevance || null;
  const selectable = contactActionable(contact);

  const routeStatus = email
    ? el("span", { class: "route-status ok" },
      "Verified email · ", el("strong", {}, email))
    : best
      ? el("span", { class: "route-status ok" },
        "Reachable via ", el("strong", {},
          best.title || routeKindLabel(best.kind)),
        best.channel === "station" ? " (station route)" : "")
      : el("span", { class: "route-status none" },
        contact.phone
          ? `phone channel only: ${contact.phone}`
          : "No route found — marked for further investigation");

  const routeRows = routes.slice(0, 4).map((route) => {
    const value = route.value || route.detail;
    const label = route.title || routeKindLabel(route.kind) || value;
    return el("li", { class: "contact-route" },
      el("span", { class: "route-kind-chip" },
        routeKindLabel(route.kind)),
      value && /^(https?:|mailto:)/i.test(value)
        ? externalLink(value, label)
        : el("span", {}, label),
      route.channel === "station"
        ? el("span", { class: "dim" }, "station route") : null);
  });
  const bestLine = best === null ? null : el("div", { class: "dim best-line" },
    "Best route: ",
    best.value && /^(https?:|mailto:)/i.test(best.value)
      ? externalLink(best.value, best.title || routeKindLabel(best.kind))
      : el("span", {}, best.title || routeKindLabel(best.kind)));

  const control = el("span", {}, null);
  const render = () => {
    if (basket.has(uid)) {
      control.replaceChildren(
        el("span", { class: "chip status-ready" }, "staged"),
        " ",
        el("span", {
          class: "linkish",
          role: "button",
          title: "remove from your working list",
          onClick: () => { basket.remove(uid); onStagedChange(); },
        }, "remove"));
      return;
    }
    if (!selectable) {
      control.replaceChildren(
        el("span", { class: "dim" }, "marked for investigation"));
      return;
    }
    const add = el("button", { class: "primary inline" }, "Add to outreach");
    add.addEventListener("click", () => {
      stageKeyContact(contact, payload, identityKey, basket);
      onStagedChange();
      render();
    });
    control.replaceChildren(add);
  };
  render();

  return el("article", { class: "contact-card key" },
    el("div", { class: "head" },
      el("span", { class: "name" }, title),
      contact.role && contact.role !== "unknown"
        ? el("span", { class: "chip", title: contact.role_reason || "" },
          contact.role) : null,
      relevance
        ? el("span", {
          class: `chip relevance-${String(relevance.label).toLowerCase()}`,
          title: relevance.reason || "",
        }, `Relevance: ${relevance.label}`) : null,
      contact.preferred_for_submissions
        ? el("span", { class: "preferred-star",
          title: "flagged as the station's preferred submission contact" },
          "★ preferred")
        : null),
    el("div", { class: "route-status-line" }, routeStatus),
    bestLine,
    routes.length
      ? el("div", { class: "contact-routes-box" },
        el("div", { class: "dim contact-routes-title" },
          "Available outreach routes"),
        el("ul", { class: "contact-routes" }, routeRows))
      : null,
    foundOn
      ? el("div", { class: "dim evidence-row" }, "Found on: ",
        externalLink(foundOn))
      : null,
    el("div", { class: "actions-row" },
      el("span", { class: "grow" }, null),
      control));
}

function keyContactsCard(contacts, payload, identityKey, basket, onStagedChange) {
  const ranked = rankedContacts(contacts);
  const keys = ranked.filter(isKeyContact);
  const more = ranked.filter(isMoreRelevantContact);
  const shown = keys.slice(0, 3);
  const extraKeys = keys.slice(3);

  const cards = [
    ...shown.map((c) =>
      keyContactCard(c, payload, identityKey, basket, onStagedChange)),
  ];

  if (extraKeys.length > 0 || more.length > 0) {
    const extraNet = [...extraKeys, ...more];
    const extraBody = el("div",
      { class: "key-more", style: "display:none" },
      extraNet.map((c) =>
        keyContactCard(c, payload, identityKey, basket, onStagedChange)));
    const toggle = el("div", { class: "key-more-toggle" },
      el("button", {
        class: "linkish",
        onClick: () => {
          const open = extraBody.style.display !== "none";
          extraBody.style.display = open ? "none" : "grid";
          toggle.querySelector("button").textContent =
            open ? "View more relevant contacts +"
              : "Hide additional relevant contacts −";
        },
      }, `View ${extraNet.length} more relevant contacts +`),
      el("p", { class: "dim" },
        "Additional music-relevant people with a reachable route. "
        + "Not a full directory."));
    cards.push(extraBody, toggle);
  }

  return el("section", { class: "card", id: "station-contacts" },
    el("p", { class: "dim section-label" }, "Contacts"),
    el("h2", {}, "Music contacts"),
    el("p", { class: "dim" },
      "The people who decide about music, as published on the station's ",
      "own site. Add the ones you want to reach, then prepare outreach below."),
    cards.length
      ? cards
      : el("p", { class: "dim" },
        "No published music contact found."));
}

/* ---------------------------------------------------------------------------
 * Section 4 — OUTREACH: staged recipients + release selector + Start
 * ------------------------------------------------------------------------- */

/* A URL-only station (no verified email decision-maker) is still selectable
 * through a VERIFIED station-published route — its music-submission web
 * form, or failing that its official contact page. One anonymous "station
 * submission point" marker per route; never a fabricated person. */
function stationRouteRecipient(contactsPayload, identityKey, intel,
  usefulPages, basket) {
  const anyVerifiedEmail = (contactsPayload.contacts || [])
    .some((c) => isKeyContact(c) && verifiedEmail(c));
  if (anyVerifiedEmail) return null;          // email route exists: prefer it

  const route = bestSubmissionRoute(intel, usefulPages)
    || fallbackStationRoute(usefulPages);
  if (!route || !route.verified) return null; // only a VERIFIED exact URL counts
  const uid = "wf_" + String(route.url).replace(/[^a-z0-9]+/gi, "_");
  if (basket.has(uid)) return { contact_uid: uid, added: false, route };
  const kind = route.kind || "webform";
  const added = basket.add({
    contact_uid: uid,
    identity_key: identityKey,
    station_name: contactsPayload.station_name,
    name: typeof route.label === "string"
      ? route.label.replace(/^official /, "") : "station web form",
    role: "musical_submission",
    email: "",
    source_url: route.source && route.source.source_url
      ? route.source.source_url : null,
    outreach_class: "webform",
    submission_url: route.url,
    route_kind: kind,
    route_label: route.label,
  });
  return { contact_uid: uid, added, route };
}

function releaseSelector() {
  const select = el("select", { name: "release", id: "station-release" });
  select.append(el("option", { value: "" }, "no music attached"));
  api.tracks({ status: "ready", limit: 200 })
    .then((data) => {
      const list = data.tracks || [];
      if (list.length === 0) {
        select.append(el("option", { value: "", disabled: true },
          "(My music is empty — upload an MP3 first)"));
        return;
      }
      for (const track of list) {
        const option = el("option", { value: track.track_id },
          track.original_filename || track.track_id);
        select.append(option);
      }
    })
    .catch(() => { /* selector stays with the "no music attached" default */ });
  return select;
}

/* Start outreach: create one record per staged recipient (POST /outreach),
 * then clear the staged set for this station and hand off to the outreach
 * page where each record is composed and handed to email. */
async function startOutreach(identityKey, stationName, basket, ui, status) {
  const staged = basket.items.filter((item) =>
    item.identity_key === identityKey);
  if (staged.length === 0) {
    status.textContent = "Add at least one recipient above first.";
    return;
  }
  ui.disabled = true;
  ui.textContent = "preparing records…";
  const release = document.getElementById("station-release");
  const trackId = release ? release.value : "";
  const track = trackId ? { track_id: trackId } : null;
  if (track && release.selectedOptions && release.selectedOptions[0]) {
    const label = release.selectedOptions[0].textContent.trim();
    if (label && label !== trackId) track.original_filename = label;
  }
  const subject = (document.getElementById("station-subject-template").value || "")
    .trim();
  const message = (document.getElementById("station-message-template").value || "")
    .trim();
  const created = [];
  try {
    for (const item of staged) {
      const record = await api.createOutreach({
        recipient: {
          contact_uid: item.contact_uid,
          identity_key: item.identity_key,
          target_type: item.target_type || "station",
          name: item.name || null,
          role: item.role || null,
          organization: stationName,
          email: String(item.email || "").trim(),
          outreach_class: item.outreach_class || "email",
          submission_url: item.submission_url || null,
          source_url: item.source_url || null,
        },
        track,
        subject,
        message,
      });
      created.push(record);
    }
  } catch (error) {
    ui.disabled = false;
    ui.textContent = "Start outreach";
    status.textContent = `Could not create records: ${
      error instanceof ApiError ? error.message : String(error)}`;
    return;
  }
  for (const item of staged) basket.remove(item.contact_uid);
  status.textContent =
    `Created ${created.length} outreach record${created.length === 1 ? "" : "s"} — ` +
    "opening Outreach so you can hand them off.";
  window.location.hash = outreachHref;
}

function outreachSection(detail, contactsPayload, intel, usefulPages,
  identityKey, basket) {
  const status = el("p", { class: "dim station-outreach-status",
    role: "status" });
  const stagedBox = el("div", { class: "staged-box" });

  const renderStaged = () => {
    const staged = basket.items.filter((item) =>
      item.identity_key === identityKey);
    if (staged.length === 0) {
      stagedBox.replaceChildren(
        el("p", { class: "dim" },
          "Nothing staged for this station yet. Use the Add to outreach ",
          "buttons above, or add the station's submission form below."));
      return;
    }
    stagedBox.replaceChildren(
      el("ul", { class: "staged-list" },
        staged.map((item) =>
          el("li", { class: "staged-item" },
            el("span", {}, item.name || item.station_name || "recipient"),
            el("span", { class: "dim" },
              item.email ? ` · ${item.email}`
                : (item.submission_url ? " · submission form" : "")),
            el("span", {
              class: "linkish",
              role: "button",
              title: "remove from your working list",
              onClick: () => {
                basket.remove(item.contact_uid);
                renderStaged();
              },
            }, "remove")))));
  };
  renderStaged();
  const off = basket.subscribe(renderStaged);
  unsubscribeFns.push(off);

  const start = el("button", { class: "primary" }, "Start outreach");
  start.addEventListener("click", () =>
    startOutreach(identityKey, detail.name, basket, start, status));

  const addAll = el("button", { class: "subtle" }, "Add all selectable contacts");
  addAll.addEventListener("click", () => {
    let added = 0;
    for (const contact of contactsPayload.contacts || []) {
      if (!isKeyContact(contact)) continue;
      if (!contactActionable(contact)) continue;
      if (stageKeyContact(contact, contactsPayload, identityKey, basket)) {
        added += 1;
      }
    }
    /* Station-web-form last resort ONLY when no named key contact staged. */
    if (added === 0) {
      const wf = stationRouteRecipient(contactsPayload, identityKey, intel,
        usefulPages, basket);
      if (wf && wf.added) added = 1;
    }
    status.textContent = added > 0
      ? `Staged ${added} recipient${added === 1 ? "" : "s"} for outreach.`
      : "No selectable outreach route found for this station.";
  });

  const subjectInput = el("input", {
    type: "text", id: "station-subject-template",
    placeholder: "Subject (optional, e.g. \"New release for consideration\")",
    autocomplete: "off",
  });
  const messageInput = el("textarea", {
    id: "station-message-template", rows: "5",
    placeholder: "Message template (optional) — will be used for every record " +
      "created here; personalize each one later on the Outreach page.",
    autocomplete: "off",
  });

  return el("section", { class: "card", id: "station-outreach" },
    el("p", { class: "dim section-label" }, "Outreach"),
    el("h2", {}, "Prepare outreach for this station"),
    el("p", { class: "dim" },
      "Pick a release, pick who to reach, and click Start outreach to ",
      "create an outreach record for each recipient. Records land on the ",
      "Outreach page where you compose and hand them off."),
    el("div", { class: "staged-grid" },
      el("div", { class: "staged-col" },
        el("label", { class: "field" },
          el("span", {}, "Release (from My music)"),
          releaseSelector(),
          el("span", { class: "dim hint" },
            "Attached to every record created below.")),
        stagedBox),
      el("div", { class: "staged-col" },
        el("label", { class: "field" },
          el("span", {}, "Subject"),
          subjectInput),
        el("label", { class: "field" },
          el("span", {}, "Message template"),
          messageInput))),
    el("div", { class: "actions-row" },
      start,
      addAll,
      el("a", { class: "linkish", href: outreachHref },
        "View prepared records →")),
    status);
}

/* ---------------------------------------------------------------------------
 * Intelligence Details (collapsed): overview record, epistemology,
 * verification history, link accessibility.
 * ------------------------------------------------------------------------- */

function kvCard(title, pairs) {
  const rows = pairs
    .filter(([, value]) =>
      value !== null && value !== undefined &&
      !(Array.isArray(value) && value.length === 0))
    .map(([key, value]) => [el("dt", {}, key), el("dd", {}, value)]);
  if (rows.length === 0) return null;
  return el("section", { class: "card" },
    el("h2", {}, title), el("dl", { class: "kv" }, rows.flat()));
}

function overviewCardTech(detail) {
  const socials = Object.entries(detail.social_urls || {});
  return kvCard("Station record", [
    ["description", detail.description],
    ["language", detail.language],
    ["location", stationLocation(detail) || null],
    ["location status", detail.location_status || null],
    ["market area", detail.market_area],
    ["station type", detail.station_type],
    ["classification", detail.classification_confidence === null ||
      detail.classification_confidence === undefined
      ? null
      : fmtPct(detail.classification_confidence)],
    ["classification evidence", fmtList(detail.classification_evidence)],
    ["socials", socials.length
      ? el("span", {}, socials.map(([platform, url], index) => [
        index > 0 ? " · " : null,
        externalLink(url, platform)]))
      : null],
    ["first stored", detail.first_stored_at],
    ["last observed", detail.last_observed_at],
    ["last verified", detail.last_verified_at],
    ["confidence reasons", fmtList(detail.confidence_reasons)],
  ]);
}

function checkEntryRow(entry) {
  const reachable = entry.ok === true;
  const facts = [
    entry.status != null ? `status ${entry.status}` : null,
    entry.error_kind || null,
    entry.latency_ms != null ? `${entry.latency_ms} ms` : null,
    entry.checked_at || null,
  ].filter(Boolean).join(" · ");
  return el("div", { class: "check-row" },
    el("span", {
      class: `chip ${reachable ? "check-ok" : "check-fail"}`,
    }, reachable ? "reachable" : "unreachable"),
    el("span", { class: "check-url" },
      externalLink(entry.url),
      el("div", { class: "dim" }, String(entry.target_kind ?? ""))),
    el("span", { class: "dim" }, facts || "—"));
}

function accessibilityCard(identityKey, container) {
  const card = el("div", {}, el("h3", {}, "Link accessibility"));
  card.append(el("p", { class: "dim" },
    "Backend-recorded reachability of this station's submission links. ",
    "Checks run on demand against the stored submission targets."));

  const rowsBox = el("div", {});
  const renderRows = (entries) => {
    rowsBox.replaceChildren(
      ...(entries && entries.length
        ? entries.map(checkEntryRow)
        : [el("p", { class: "dim" },
          "No link checks recorded yet.")]));
  };
  renderRows(container ? container.last_checks : []);

  const runButton = el("button", { class: "primary" }, "run checks now");
  runButton.addEventListener("click", async () => {
    runButton.disabled = true;
    runButton.textContent = "checking…";
    try {
      const summary = await api.runSubmissionChecks(identityKey);
      renderRows(summary.checks);
      rowsBox.prepend(el("p", { class: "dim" },
        `latest run: ${summary.reachable} of ${summary.targets} targets reachable`));
    } catch (error) {
      rowsBox.prepend(errorBanner(error));
    } finally {
      runButton.disabled = false;
      runButton.textContent = "run checks now";
    }
  });

  card.append(rowsBox, el("div", { class: "actions-row" }, runButton));
  return card;
}

function epistemologyCard(epi) {
  if (!epi) return null;
  return el("div", {},
    el("h3", {}, "How to read this record"),
    el("dl", { class: "kv" }, [
      ["facts recorded", epi.facts_count],
      ["inferred fields", fmtList(epi.inferred_fields)],
      ["unknown fields", fmtList(epi.unknown_fields)],
    ]),
    el("ul", { class: "provenance-list" },
      (epi.notes || []).map((note) => el("li", {}, note))));
}

function verificationCard(container) {
  const data = container && container.verification;
  const head = el("h3", {}, "Verification history");
  if (!data || (!(data.runs || []).length && !(data.results || []).length)) {
    return el("div", {}, head,
      el("p", { class: "dim" },
        "No verification runs recorded for this station yet."));
  }
  const runCards = (data.runs || []).map((run) =>
    el("article", { class: "run-card" },
      el("div", {},
        el("strong", {}, "run "), run.run_id, " ",
        el("span", { class: "dim" },
          `${run.started_at || "?"} → ${run.completed_at || "?"}`
          + ` · source: ${run.source}`)),
      el("div", { class: "chips" },
        Object.entries(run.summary || {}).map(([status, count]) =>
          el("span",
            { class: `chip status-${STATUS_CLASSES.includes(status) ? status : "unverified"}` },
            `${status}: ${count}`)))));
  const resultRows = (data.results || []).map((result) =>
    el("div", { class: "result-row" },
      el("span", {
        class: `status-${STATUS_CLASSES.includes(result.status) ? result.status : "unverified"}`,
      }, result.status),
      el("span", {},
        el("div", {}, result.claim),
        (result.reasons || []).length
          ? el("div", { class: "dim" }, result.reasons.join("; "))
          : null,
        (result.evidence || []).map((item) =>
          el("div", { class: "dim" },
            `${item.value} ← ${(item.sources || []).join(", ")}`))),
      el("span", { class: "dim" }, result.checked_at)));
  return el("div", {}, head, runCards,
    el("h4", {}, "Claim results"), resultRows);
}

function intelligenceDetails(detail, intel, verification, submissionData,
  identityKey) {
  const parts = [
    overviewCardTech(detail),
    epistemologyCard(intel.epistemology),
    verificationCard(verification),
    accessibilityCard(identityKey, submissionData),
  ].filter(Boolean);
  return el("details", { class: "card detail-collapse" },
    el("summary", {}, "Intelligence Details"),
    el("div", { class: "detail-body" }, parts));
}

/* ---------------------------------------------------------------------------
 * View assembly
 * ------------------------------------------------------------------------- */

export function renderStationView(root, identityKey, basket) {
  for (const off of unsubscribeFns.splice(0)) off();

  root.append(el("p", { class: "dim" }, "Loading…"));

  Promise.all([
    api.station(identityKey),
    api.intelligence(identityKey),
    api.contacts(identityKey),
    api.verification(identityKey).catch((error) =>
      error instanceof ApiError && error.status === 404
        ? null
        : Promise.reject(error)),
    api.stationSubmission(identityKey).catch((error) =>
      error instanceof ApiError && error.status === 404
        ? null
        : Promise.reject(error)),
  ]).then(([detail, intel, contactsPayload, verification,
    submissionData]) => {
    const route = bestSubmissionRoute(intel, intel.useful_pages);
    const onStagedChange = () => { /* cards re-render via basket subscription */ };
    root.replaceChildren(
      overviewSection(detail),
      submissionInformationCard(detail, intel, intel.useful_pages,
        contactsPayload),
      keyContactsCard(contactsPayload.contacts, contactsPayload,
        identityKey, basket, onStagedChange),
      outreachSection(detail, contactsPayload, intel, intel.useful_pages,
        identityKey, basket),
      intelligenceDetails(detail, intel, verification, submissionData,
        identityKey));
  }).catch((error) => {
    root.replaceChildren(errorBanner(error));
  });
}

export function teardownStationView() {
  for (const off of unsubscribeFns.splice(0)) off();
}