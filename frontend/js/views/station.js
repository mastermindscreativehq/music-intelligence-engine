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
      verified: canonical.verified === true,
    };
  }
  const page = submissionPages(usefulPages)[0] || null;
  if (page) {
    return {
      url: page.url,
      label: page.label || "submission page",
      source: page,
      verified: page.reachable === true,
    };
  }
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
    p.why || p.next_step
      ? el("div", { class: "dim up-why" },
        [p.why, p.next_step].filter(Boolean).join(" "))
      : null,
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
 * Section 2 — Outreach intelligence (evidence model, Phase 11)
 *
 * Renders the station's derived intelligence exactly as computed by the
 * backend: level, the single best outreach route, alternatives, and — always
 * — what is still unknown. Nothing here invents a route; when no verified
 * route exists the no-route state is shown head-on (differs from the
 * best-action tiles by design).
 * ------------------------------------------------------------------------- */

const PRIORITY_LABEL = {
  1: "P1 · Direct music submission",
  2: "P2 · Music contact",
  3: "P3 · General contact",
  4: "P4 · DJ / program directory",
};
const LEVEL_BADGE_CLASS = {
  ACTIONABLE: "intel-actionable",
  LIMITED: "intel-limited",
  INSUFFICIENT_EVIDENCE: "intel-insufficient",
  VERIFIED: "intel-verified",
  RAW: "intel-raw",
  UNKNOWN: "intel-raw",
};

function routeLink(route) {
  const value = String(route.value || "");
  if (/^https?:/i.test(value)) {
    return externalLink(value, route.title || value);
  }
  if (/^mailto:/i.test(value)) {
    return externalLink(value, route.title || value.replace(/^mailto:/, ""));
  }
  return el("span", {}, route.title || value || "—");
}

function routeChip(route) {
  return el("span", {
    class: "chip evidence",
    title: route.evidence || "",
  }, route.verification_state || route.evidence_state || "UNKNOWN");
}

function routeRow(route) {
  return el("div", { class: "route-row" },
    el("span", { class: "route-priority" },
      PRIORITY_LABEL[route.priority] || `P${route.priority} · Route`),
    el("div", { class: "route-body" },
      el("div", { class: "route-title" },
        routeLink(route),
        routeChip(route)),
      el("div", { class: "dim route-why" }, route.why || "")));
}

function unknownList(unknowns) {
  if (!unknowns || !unknowns.length) return null;
  return el("div", { class: "unknown-list" },
    el("span", { class: "dim unknown-label" }, "Still unknown: "),
    unknowns.map((item) => el("span", { class: "chip" }, item)));
}

/* Re-exported label kept as its own function so the honest copy stays
 * pinned at one spelling across cases. */
function unverifiedLabel() {
  return "No verified outreach route";
}

function outreachIntelligenceCard(detail, intel) {
  const level = String(intel.intelligence_level || "UNKNOWN");
  const reason = intel.intelligence_level_reason || "";
  const rec = intel.outreach_recommendation || null;
  const routes = intel.outreach_routes || [];
  const primary = rec && rec.route ? rec.route : null;
  const alternatives = (primary ? routes.filter((r) => r !== primary)
    : routes).filter((r) => Boolean(r.value)
      || r.verification_state === "ACTIONABLE");

  const levelBadge = el("span", {
    class: `chip intel-level ${LEVEL_BADGE_CLASS[level] || "intel-raw"}`,
  }, level);

  const verifiedBlock = el("div", { class: "intel-verified" },
    el("h3", {}, "WHAT WE VERIFIED"),
    el("div", { class: "verified-line" }, levelBadge,
      el("span", { class: "dim" }, reason || "No evidence recorded.")));

  const body = [verifiedBlock];

  if (primary) {
    body.push(el("div", { class: "intel-best" },
      el("h3", {}, "BEST OUTREACH ROUTE"),
      routeRow(primary),
      el("div", { class: "dim intel-why" },
        el("strong", {}, "Why: "), rec.why || primary.why || "",
        " · Confidence: ", el("strong", {}, rec.confidence || "—")),
      Array.isArray(rec.evidence) && rec.evidence.length
        ? el("div", { class: "dim evidence-row" },
          "Evidence: ", el("span", {}, rec.evidence.join("; ")))
        : null));
  } else {
    body.push(el("div", { class: "intel-none" },
      el("h3", {}, "BEST OUTREACH ROUTE"),
      el("p", { class: "dim" }, unverifiedLabel() + " for this station yet. "
        + "The record has been inspected; no verified path to invite music "
        + "was found, so no route is staged.")));
  }

  const shownAlts = alternatives.slice(0, 5);
  if (shownAlts.length) {
    body.push(el("div", { class: "intel-alts" },
      el("h3", {}, "ALTERNATIVE ROUTES"),
      el("div", {}, shownAlts.map(routeRow))));
  }

  body.push(el("div", { class: "intel-unknown" },
    el("h3", {}, "WHAT IS UNKNOWN"),
    unknownList(rec && rec.unknown)
    || el("p", { class: "dim" }, "Nothing else outstanding is documented.")));

  return el("section", { class: "card", id: "station-outreach-intel" },
    el("h2", {}, "Outreach intelligence"),
    el("p", { class: "dim" },
      "Evidence-backed picture of how this station accepts music outreach — "
      + "what the engine verified, the best recorded route, and what is still "
      + "unknown."),
    el("div", { class: "intel-body" }, body));
}

/* ---------------------------------------------------------------------------
 * Section 3 — Best Actions
 * ------------------------------------------------------------------------- */

function bestActionsCard(detail, intel, usefulPages, contactsPayload) {
  const website = detail.website || detail.domain || null;
  const route = bestSubmissionRoute(intel, usefulPages);
  const ranked = rankedContacts((contactsPayload && contactsPayload.contacts) || []);
  const emailContact = ranked.find((c) => verifiedEmail(c));
  const contactPage = bestOfCategory(usefulPages, "contact");
  const djDirectory = bestOfCategory(usefulPages, "dj_directory");
  const status = String(intel.submission_status || "");
  const backendAction = intel.best_action || null;

  const tiles = [];

  /* Primary tile — the single best evidence-backed submission route.
   * DIRECT: backend submit action, else canonical route.
   * CONTACT: the backend's contact/browse action IS the submission route,
   *   shown as "Submission information found" (per the evidence model),
   *   never a false "No route found" claim.
   * NONE probed/UNKNOWN: honest muted copy. */
  if (backendAction && backendAction.kind === "submit" && backendAction.url) {
    tiles.push(externalLink(backendAction.url,
      el("span", { class: "action-tile primary-tile" },
        el("strong", {}, backendAction.label || "Send music"),
        el("span", { class: "dim action-sub" },
          backendAction.detail || "submission page on station site"))));
  } else if (route) {
    tiles.push(externalLink(route.url,
      el("span", { class: "action-tile primary-tile" },
        el("strong", {}, "Send music"),
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

  /* Contact decision-maker: verified email when present; otherwise a station
   * contact page (unless it already served as the primary browse route). */
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

  /* Add-to-campaign is only actionable when a real outreach route exists
   * (verified email decision-maker or verified web-form submission route). */
  if (ranked.some((c) => isActionable(c)) || (route && route.verified)) {
    tiles.push(el("span", { class: "action-tile action-staged" },
      el("button", {
        class: "primary inline",
        id: "station-add-campaign",
      }, "Add to campaign")));
  } else {
    tiles.push(el("span", { class: "action-tile action-muted" },
      el("strong", {}, "Add to campaign"),
      el("span", { class: "dim action-sub" },
        "No verified email or web-form outreach route yet.")));
  }

  return el("section", { class: "card action-bar", id: "station-actions" },
    el("h2", {}, "Best actions"),
    el("p", { class: "dim" },
      "Evidence-backed next steps from the station site — nothing here "
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

/* A decision-role or backend-preferred person counts as a key contact. The
 * actionable set for campaign staging is narrower: a person needs a verified
 * email route. No-email decision-makers still SHOW in Key Contacts with
 * honest "no verified outreach route" copy — their role is evidence. */
function isActionable(contact) {
  return isKeyContact(contact) && Boolean(verifiedEmail(contact));
}

function roleTitle(role) {
  if (!role || role === "unknown") return null;
  return String(role).replace(/_/g, " ");
}

function evidenceStateLabel(state) {
  if (state === "VERIFIED") return "verified email";
  if (state === "EVIDENCE-BACKED") return "phone on record";
  return "on record";
}

function keyContactCard(contact, payload, identityKey, basket) {
  const uid = String(contact.contact_uid);
  const email = verifiedEmail(contact);
  const selected = basket.has(uid);
  const title = contact.name
    || roleTitle(contact.role)
    || "(unnamed contact)";
  const foundOn = contact.source_url
    || ((contact.sources && contact.sources[0]) || null);

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
        ? el("span", { class: "chip", title: contact.role_reason || "" },
          contact.role) : null,
      contact.evidence_state
        ? el("span", { class: "chip evidence",
          title: contact.role_reason || "" },
          evidenceStateLabel(contact.evidence_state)) : null,
      contact.route_class && contact.route_class !== "UNKNOWN_ROLE"
        ? el("span", { class: "chip", title: "backend route classification" },
          contact.route_class) : null,
      contact.preferred_for_submissions
        ? el("span", { class: "preferred-star",
          title: "backend-flagged preferred_for_submissions" },
          "★ preferred")
        : null),
    el("div", { class: "route-status-line" }, routeStatus),
    foundOn
      ? el("div", { class: "dim evidence-row" }, "Found on: ",
        externalLink(foundOn))
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
      "Decision-makers and verified music-relevant people, ranked by "
      + "evidence. No outreach route is invented."),
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

/* A URL-only station (no verified email decision-maker, e.g. WFMU) is still
 * selectable for outreach through its VERIFIED music-submission/contact web
 * form. This computes a stable selection uid (never a fabricated contact —
 * the real artifact is the verified submission_url). */
function webformCampaignRecipient(contactsPayload, identityKey, intel,
  usefulPages, basket) {
  const anyVerifiedEmail = (contactsPayload.contacts || [])
    .some((c) => isKeyContact(c) && verifiedEmail(c));
  if (anyVerifiedEmail) return null;          // email route exists: prefer it

  const route = bestSubmissionRoute(intel, usefulPages);
  if (!route || !route.verified) return null; // only a VERIFIED exact URL counts
  const uid = "wf_" + String(route.url).replace(/[^a-z0-9]+/gi, "_");
  if (basket.has(uid)) return { contact_uid: uid, added: false };
  const added = basket.add({
    contact_uid: uid,
    identity_key: identityKey,
    station_name: contactsPayload.station_name,
    name: typeof route.label === "string"
      ? route.label.replace(/^official /, "") : "station submission page",
    role: "musical_submission",
    email: "",
    source_url: route.source && route.source.source_url
      ? route.source.source_url : null,
    outreach_class: "webform",
    submission_url: route.url,
  });
  return { contact_uid: uid, added };
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
      outreachIntelligenceCard(detail, intel),
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
        // For a URL-only station (no verified email decision-maker), fall
        // back to its VERIFIED submission/contact web form so it is still a
        // selectable outreach route — never a fabricated email.
        const webform = webformCampaignRecipient(contactsPayload, identityKey,
          intel, intel.useful_pages, basket);
        const addedUids = [
          ...added.map((c) => String(c.contact_uid)),
          ...(webform && webform.added ? [webform.contact_uid] : []),
        ];
        if (addedUids.length === 0) {
          addCampaign.textContent = webform
            ? "no verified route"
            : "no selectable contacts";
          return;
        }
        addCampaign.textContent =
          `staged ${addedUids.length} recipient(s)`;
        const addedNames = added
          .map((c) => c.name || c.role || "contact").filter(Boolean);
        if (webform && webform.added) {
          addedNames.push(typeof webform.name === "string"
            ? webform.name : "station web form");
        }
        addCampaign.disabled = true;

        const confirm = el("div", { class: "banner-info station-confirm" },
          "Added ", el("strong", {}, `${addedUids.length} recipient(s)`),
          " to your list (", el("span", {}, addedNames.join(", ")), ").");
        const start = el("a", {
          class: "primary",
          href: outreachHref(addedUids),
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