/* Station intelligence view — the artist outreach workflow.
 *
 * Purpose: understand the station, identify relevance, find the best verified
 * submission route, find the music decision-maker, and act. The page leads
 * with a clean overview, a short set of evidence-backed actions, the handful
 * of people who actually decide about music, and a maximum of three useful
 * station pages. Everything rendered comes verbatim from the Phase 4-8
 * endpoints; this view adds presentation only.
 *
 * Data-integrity rules mirrored from the backend:
 *   - a requestable action exists ONLY when the backend stored the exact
 *     route (submission.submission_url Fact, a discovered useful page URL,
 *     or a contact's verified email);
 *   - URL selection never fabricates or constructs routes, and URL variants
 *     of the same page collapse into one action;
 *   - contacts are ranked by backend role relevance and the preferred flag,
 *     and non-qualified people are never dumped into this view.
 */

import { api, ApiError } from "../api.js";
import {
  chips,
  confidenceBar,
  el,
  fmtList,
  fmtPct,
} from "../dom.js";
import { outreachHref } from "../router.js";
import { openOutreachModal } from "./outreachModal.js";

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

function statusSpan(status) {
  return el("span",
    { class: `status-${STATUS_CLASSES.includes(status) ? status : "unverified"}` },
    String(status ?? "unknown"));
}

function externalLink(url, text) {
  return el("a", {
    href: url,
    target: "_blank",
    rel: "noopener noreferrer",
  }, text ?? url);
}

function locationOf(detail) {
  return [detail.city, detail.state_or_region, detail.country]
    .filter(Boolean).join(", ") || null;
}

/* ---------------------------------------------------------------------------
 * Section 1 — Overview
 * ------------------------------------------------------------------------- */

function overviewSection(detail) {
  const lowIntel = typeof detail.confidence_score !== "number"
    || detail.confidence_score <= 0;
  const location = locationOf(detail);
  return el("section", { class: "card detail-head" },
    el("h1", {}, detail.name || "(unnamed station)"),
    el("div", { class: "overview-row" },
      detail.website
        ? externalLink(detail.website, detail.domain ?? detail.website)
        : el("span", { class: "dim" }, "no website on record"),
      location ? el("span", {}, ` · ${location}`) : null),
    el("div", { class: "overview-row" },
      chips(detail.genres), " ", chips(detail.formats)),
    el("div", { class: "actions-row" },
      confidenceBar(detail.confidence_score),
      el("span", {}, `overall ${fmtPct(detail.confidence_score)} · `),
      statusSpan(detail.status)),
    lowIntel
      ? el("p", { class: "dim note-honest" },
        "Limited intelligence available — no enrichment has been recorded "
        + "for this station yet.")
      : null);
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

/* Submission-classed pages from the single evidence-backed list. The backend
 * orders useful pages most-outreach-relevant first, so the first member of
 * this subset is the highest-priority discovered submission page. */
function submissionPages(usefulPages) {
  return (usefulPages || []).filter((p) => p
    && typeof p.url === "string" && /^https?:\/\//i.test(p.url)
    && (p.category === "send_music"
      || p.category === "submission_guidelines"));
}

/* The single best Send Music route: the canonical backend submission_url
 * Fact first; otherwise the best discovered submission-classed useful page.
 * Returns {url, label, source} or null. Never constructs a route. */
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
    };
  }
  const page = submissionPages(usefulPages)[0] || null;
  if (page) return { url: page.url, label: page.label || "submission page", source: page };
  return null;
}

/* Junk rejection for curated useful pages. Each category only surfaces a
 * page whose label/URL genuinely belongs to that category — never donate/
 * blog/news/about/events/merch/personal-profile/archive/random-dir pages. */
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

/* Curated Useful Pages: strict categories only, deduped by normalized URL,
 * at most 3 rows. Picks the single best page per group, ordered by
 * outreach priority (Music submission > DJ directory > Programming >
 * Contact). Never dumps raw discovery lists. */
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
    el("span", { class: "dim up-meta" }, USEFUL_LABELS[p.category] || p.category));
}

function usefulPagesCard(usefulPages, canonicalRoute) {
  const rows = curatedUsefulPages(usefulPages, canonicalRoute);
  const suppressedSubmission = submissionPageSuppressed(canonicalRoute);
  return el("section", { class: "card" },
    el("h2", {}, "Useful pages"),
    el("p", { class: "dim" },
      "The few highest-value station pages the engine verified — each opens "
      + "the exact discovered URL, never a guessed route."),
    rows.length
      ? el("div", { class: "up-list" },
        rows.map((r) => usefulPageRow(r.best)))
      : el("p", { class: "dim" },
        suppressedSubmission
          ? "No verified useful pages beyond the best submission route "
            + "already shown above."
          : "No verified useful pages were discovered."));
}

/* ---------------------------------------------------------------------------
 * Section 2 — Best Actions
 * ------------------------------------------------------------------------- */

function bestActionsCard(detail, intel, usefulPages, contactsPayload) {
  const website = detail.website || detail.domain || null;
  const route = bestSubmissionRoute(intel, usefulPages);
  const ranked = rankedContacts((contactsPayload && contactsPayload.contacts) || []);
  const emailContact = ranked.find((c) => verifiedEmail(c));
  const contactPage = bestOfCategory(usefulPages, "contact");
  const djDirectory = bestOfCategory(usefulPages, "dj_directory");

  const tiles = [];

  tiles.push(route
    ? externalLink(route.url,
      el("span", { class: "action-tile primary-tile" },
        el("strong", {}, "Send music"),
        el("span", { class: "dim action-sub" }, route.label)))
    : el("span", { class: "action-tile action-muted" },
      el("strong", {}, "Send music"),
      el("span", { class: "dim action-sub" },
        "No verified submission route found.")));

  if (emailContact) {
    tiles.push(externalLink(`mailto:${emailContact.email}`,
      el("span", { class: "action-tile" },
        el("strong", {}, contactActionLabel(emailContact)),
        el("span", { class: "dim action-sub" }, emailContact.email))));
  } else if (contactPage) {
    tiles.push(externalLink(contactPage.url,
      el("span", { class: "action-tile" },
        el("strong", {}, "Contact station"),
        el("span", { class: "dim action-sub" },
          contactPage.label || "station contact page"))));
  }

  if (djDirectory) {
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

  tiles.push(el("span", { class: "action-tile action-staged" },
    el("button", {
      class: "primary inline",
      id: "station-add-campaign",
    }, "Add to campaign")));

  return el("section", { class: "card action-bar", id: "station-actions" },
    el("h2", {}, "Best actions"),
    el("p", { class: "dim" },
      "Verified, high-value next steps from backend evidence — nothing here "
      + "is guessed."),
    el("div", { class: "action-grid" }, tiles));
}

function contactActionLabel(contact) {
  const role = String(contact.role || "").toLowerCase();
  if (role === "music_director") return "Contact music director";
  if (role === "program_director") return "Contact program director";
  if (role.startsWith("music_")) return "Contact music department";
  return "Contact music department";
}

/* ---------------------------------------------------------------------------
 * Section 3 — Key Contacts
 *
 * Contacts are ranked by the same role relevance the backend uses for
 * presentation (music_director first …), then by the backend-preferred flag,
 * then by a verified email bonus. Only music-relevant people are shown;
 * nobody is dumped into this view.
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

function roleTitle(role) {
  if (!role || role === "unknown") return null;
  return String(role).replace(/_/g, " ");
}

function keyContactCard(contact, payload, identityKey, basket) {
  const uid = String(contact.contact_uid);
  const email = verifiedEmail(contact);
  const selected = basket.has(uid);
  const title = contact.name
    || roleTitle(contact.role)
    || "(unnamed contact)";

  const routeStatus = email
    ? el("span", { class: "route-status ok" },
      "Verified email · ", el("strong", {}, email))
    : el("span", { class: "route-status none" },
      contact.phone
        ? `phone only: ${contact.phone}`
        : "No verified outreach route found");

  let reachControl;
  if (email) {
    if (selected) {
      reachControl = el("span", {},
        el("button", {
          class: "subtle",
          onClick: () => basket.remove(uid),
        }, "✓ added"),
        " ",
        el("span", { class: "linkish", role: "button" }, "Reach Out"));
    } else {
      const reach = el("button", { class: "primary inline" }, "Reach Out");
      reach.addEventListener("click", () => {
        basket.add({
          contact_uid: uid,
          identity_key: identityKey,
          station_name: payload.station_name,
          name: contact.name,
          role: contact.role,
          email: contact.email,
          source_url: contact.source_url || null,
        });
        openOutreachModal({
          contact_uid: uid,
          identity_key: identityKey,
          name: contact.name,
          role: contact.role,
          station_name: payload.station_name,
          email: contact.email,
          source_url: contact.source_url || null,
        });
      });
      reachControl = reach;
    }
  } else {
    reachControl = el("span", { class: "dim" }, "not reachable");
  }

  return el("article", { class: "contact-card key" },
    el("div", { class: "head" },
      el("span", { class: "name" }, title),
      contact.role && contact.role !== "unknown"
        ? el("span", { class: "chip" }, contact.role) : null,
      contact.preferred_for_submissions
        ? el("span", { class: "preferred-star",
          title: "backend-flagged preferred_for_submissions" },
          "★ preferred")
        : null),
    el("div", { class: "route-status-line" }, routeStatus),
    contact.source_url
      ? el("div", { class: "dim evidence-row" }, "Found on: ",
        externalLink(contact.source_url))
      : null,
    el("div", { class: "actions-row" },
      el("span", { class: "dim" },
        "Confidence ", el("strong", {}, fmtPct(contact.confidence_score))),
      confidenceBar(contact.confidence_score),
      el("span", { class: "grow" }, null),
      reachControl));
}

function keyContactsCard(contacts, payload, identityKey, basket) {
  const ranked = rankedContacts(contacts);
  const keys = ranked.filter(isKeyContact);
  const more = ranked.filter(isMoreRelevantContact);
  const shown = keys.slice(0, 3);
  const extraKeys = keys.slice(3);

  const cards = [
    ...shown.map((c) => keyContactCard(c, payload, identityKey, basket)),
  ];

  if (extraKeys.length > 0 || more.length > 0) {
    const extraNet = [...extraKeys, ...more];
    const extraBody = el("div",
      { class: "key-more", style: "display:none" },
      extraNet.map((c) => keyContactCard(c, payload, identityKey, basket)));
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
        "Additional music-relevant people with verified contact routes. "
        + "Not a full directory."));
    cards.push(extraBody, toggle);
  }

  return el("section", { class: "card", id: "station-contacts" },
    el("h2", {}, "Key contacts"),
    el("p", { class: "dim" },
      "The people most likely to decide about music, ranked by evidence. "
      + "Only verified, music-relevant contacts appear."),
    cards.length
      ? cards
      : el("p", { class: "dim" },
        "No verified music decision-maker found."));
}

/* ---------------------------------------------------------------------------
 * Add to campaign: stages this station's verified, email-reachable key
 * contacts (the qualified outreach set) into the recipient basket.
 * ------------------------------------------------------------------------- */

function addAllToCampaign(detail, contactsPayload, identityKey, basket) {
  const staged = [];
  for (const contact of contactsPayload.contacts || []) {
    if (!isKeyContact(contact)) continue;
    if (!verifiedEmail(contact)) continue;
    if (basket.add({
      contact_uid: String(contact.contact_uid),
      identity_key: identityKey,
      station_name: contactsPayload.station_name,
      name: contact.name,
      role: contact.role,
      email: contact.email,
      source_url: contact.source_url || null,
    })) {
      staged.push(contact);
    }
  }
  return staged;
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
    ["location", locationOf(detail)],
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
    root.replaceChildren(
      overviewSection(detail),
      bestActionsCard(detail, intel, intel.useful_pages, contactsPayload),
      keyContactsCard(contactsPayload.contacts, contactsPayload,
        identityKey, basket),
      usefulPagesCard(intel.useful_pages, route),
      intelligenceDetails(detail, intel, verification, submissionData,
        identityKey));

    const addCampaign = document.getElementById("station-add-campaign");
    if (addCampaign) {
      addCampaign.addEventListener("click", () => {
        const added = addAllToCampaign(detail, contactsPayload, identityKey,
          basket);
        if (added.length === 0) {
          addCampaign.textContent = "no selectable contacts";
          return;
        }
        addCampaign.textContent = `staged ${added.length} recipient(s)`;
        const addedNames = added
          .map((c) => c.name || c.role || "contact").filter(Boolean);
        addCampaign.disabled = true;

        const confirm = el("div", { class: "banner-info station-confirm" },
          "Added ", el("strong", {}, `${added.length} recipient(s)`),
          " to your list (", el("span", {}, addedNames.join(", ")), ").");
        const start = el("a", {
          class: "primary",
          href: outreachHref(added.map((c) => String(c.contact_uid))),
        }, "Start outreach →");
        const actionsCard = document.getElementById("station-actions");
        if (actionsCard) {
          actionsCard.append(confirm,
            el("div", { class: "actions-row" }, start));
        }
      });
    }
  }).catch((error) => {
    root.replaceChildren(errorBanner(error));
  });
}

export function teardownStationView() {
  for (const off of unsubscribeFns.splice(0)) off();
}