/* Station page — action-first outreach workflow.
 *
 * Primary jobs: Find Contacts, Send Music (submission route), Visit Official
 * Website, Add to Campaign (stage this station's contacts as recipients).
 * Technical detail (confidence reasons, classification evidence, timestamps,
 * provenance, verification history, link accessibility) is available but
 * collapsed behind an "Intelligence Details" disclosure so the default page
 * leads with actions and useful navigation — not crawl evidence.
 *
 * All content comes verbatim from the Phase 4-8 endpoints; this view adds
 * presentation only. Inference labels are displayed as inferences, never
 * promoted. URL routing: a single added contact reaches the composer
 * immediately; several stage in the recipient basket for review/selection.
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
  "unverified", "unsupported"];

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

function factSource(fact) {
  if (!fact || !fact.source_url) return null;
  return externalLink(fact.source_url);
}

/* Submission-classed pages from the single evidence-backed list. The backend
 * orders useful pages most-outreach-relevant first, so the first member of
 * this subset is the highest-priority discovered submission page. Only exact
 * discovered URLs are eligible — never anything constructed. */
function submissionPages(usefulPages) {
  return (usefulPages || []).filter((p) => p
    && typeof p.url === "string" && /^https?:\/\//i.test(p.url)
    && (p.category === "send_music"
      || p.category === "submission_guidelines"));
}

/* ---------------------------------------------------------------------------
 * Primary action bar
 *
 * The Send Music action is governed by ONE evidence-backed source: the
 * station's ``useful_pages``. A submission-page link is only clickable when
 * the engine actually discovered a submission-classified page (category
 * ``send_music``/``submission_guidelines``) and stored its exact href; the
 * action opens that exact discovered URL. If none exists there is no fake
 * clickable route — the tile states "No verified submission route found."
 *
 * No URL is ever derived from ``submission.submission_url`` (a separate
 * path-regex-selected representation) or constructed from a label/domain.
 * ------------------------------------------------------------------------- */

function actionBar(detail, usefulPages) {
  const website = detail.website || detail.domain || null;
  const submissionRoute = submissionPages(usefulPages)[0] || null;

  const findContacts = el("button", { class: "primary" }, "Find contacts");
  findContacts.addEventListener("click", () => {
    const card = document.getElementById("station-contacts");
    if (card) card.scrollIntoView({ behavior: "smooth", block: "start" });
  });

  return el("section", { class: "card action-bar", id: "station-actions" },
    el("h2", {}, "Actions"),
    el("div", { class: "action-grid" },
      findContacts,
      submissionRoute
        ? externalLink(submissionRoute.url,
          el("span", { class: "action-tile" },
            el("strong", {}, "Send music"),
            el("span", { class: "dim action-sub" },
              submissionRoute.label || "submission page")))
        : el("span", { class: "action-tile action-muted" },
          el("strong", {}, "Send music"),
          el("span", { class: "dim action-sub" },
            "No verified submission route found.")),
      website
        ? externalLink(website,
          el("span", { class: "action-tile" },
            el("strong", {}, "Visit website"),
            el("span", { class: "dim action-sub" },
              detail.domain ?? website)))
        : el("span", { class: "action-tile action-muted" },
          el("strong", {}, "Visit website"),
          el("span", { class: "dim action-sub" }, "no website on record")),
      el("span", { class: "action-tile action-staged" },
        el("button", {
          class: "primary inline",
          id: "station-add-campaign",
        }, "Add to campaign"))));
}

/* ---------------------------------------------------------------------------
 * Header + useful pages
 * ------------------------------------------------------------------------- */

function detailHead(detail) {
  return el(
    "section",
    { class: "card detail-head" },
    el("h1", {}, detail.name || "(unnamed station)"),
    el("div", {},
      detail.website
        ? externalLink(detail.website, detail.domain ?? detail.website)
        : el("span", { class: "dim" }, "no website on record"),
      " ",
      chips(detail.genres), " ", chips(detail.formats)),
    el("div", { class: "actions-row" },
      confidenceBar(detail.confidence_score),
      el("span", {}, `overall ${fmtPct(detail.confidence_score)} · `),
      statusSpan(detail.status)),
  );
}

/* Evidence-backed station-level Useful Pages — navigational actions only.
 *
 * Each entry is an exact link the engine discovered verbatim on a crawled
 * page: ``label`` = the actual anchor text, ``url`` = the exact resolved
 * href, ``source_url`` = the page it was found on. The URL is never derived
 * from a label/domain/route convention and never fabricated when evidence is
 * absent. These are station pages, kept separate from individual people and
 * verified outreach routes.
 *
 * The section ALWAYS renders. With zero qualifying pages it states the honest
 * empty state instead of disappearing the whole feature. */
function usefulPagesCard(usefulPages) {
  const pages = (usefulPages || []).filter((p) =>
    p && typeof p.url === "string" && /^https?:\/\//i.test(p.url));

  // Show the first (backend-ordered, most outreach-relevant) discovered page
  // per category as the highlighted route while keeping every other
  // discovered page available below it.
  const primaryCategories = new Set();
  for (const p of pages) {
    if (p.category && !primaryCategories.has(p.category)) {
      primaryCategories.add(p.category);
      p._primary = true;
    }
  }

  const hostOf = (raw) => {
    try { return new URL(raw).hostname; } catch (error) { return raw; }
  };

  const chips = pages.map((p) => {
    const label = (p.label && p.label.trim()) || p.url;
    const reach = p.reachable === true ? "reachable"
      : p.reachable === false ? "unreachable"
        : null;
    const meta = [
      p.source_url ? `found on ${hostOf(p.source_url)}` : null,
      reach ? (reach + (p.status != null ? ` · ${p.status}` : "")) : null,
    ].filter(Boolean).join(" · ");
    return el("span",
      { class: `chip usable-page${p._primary ? " primary" : ""}` },
      label, " ", externalLink(p.url),
      el("span", { class: "dim" },
        meta ? ` (${meta})` : ""));
  });

  return el("section", { class: "card" },
    el("h2", {}, "Useful pages"),
    el("p", { class: "dim" },
      "Exact links the engine discovered on this station's site — each opens ",
      "the precise URL found, never a guessed route. Station pages only; ",
      "not individual people."),
    pages.length
      ? el("div", { class: "chips" }, chips)
      : el("p", { class: "dim" },
        "No verified useful pages were discovered."));
}

/* ---------------------------------------------------------------------------
 * Evidence-based recipient model
 *
 * A discovered person and a contactable person are DIFFERENT things:
 *   person discovery   — someone was found on a credible/official source
 *   role / relevance   — evidence that they may matter (role + identity_state)
 *   verified route     — the EXACT email or URL the backend actually stored
 *   outreach action    — the user reaches out from their own mail system
 *
 * Data-integrity rule: nothing is guessed or constructed. An outreach route
 * exists ONLY when the backend stored the exact route (contact.email for a
 * person, submission.submission_url for a station page). The frontend never
 * builds an email from a name, never appends /submissions|/contact|/music to
 * a domain, and never promotes a staff-directory page to a personal route.
 * ------------------------------------------------------------------------- */

const IDENTITY_BADGES = {
  named: { label: "named contact", class: "kind-badge named" },
  role_based: { label: "role-based contact", class: "kind-badge role" },
  unattributed_observation:
    { label: "observed value", class: "kind-badge observed" },
};

/* Roles the engine classifies as directly relevant to music outreach
 * (mirrors the backend role vocabulary; the frontend never invents roles). */
const RELEVANT_ROLES = new Set([
  "music_director", "program_director", "music_programmer",
]);

function identityBadge(contact) {
  const state = contact.identity_state || "unattributed_observation";
  const badge = IDENTITY_BADGES[state] ||
    IDENTITY_BADGES.unattributed_observation;
  const bits = [el("span", { class: badge.class }, badge.label)];
  if (state === "unattributed_observation") {
    bits.push(el("span", { class: "dim" },
      contact.method === "email" ? "email, no person or role identified"
        : contact.method === "phone" ? "phone, no person or role identified"
          : "no person or role identified"));
  }
  return bits;
}

/* The exact, actually-discovered email — null if none was recorded. */
function verifiedEmail(contact) {
  const email = String(contact.email || "").trim();
  return email || null;
}

/* Qualified outreach target? Evidence-driven: exact relevant role or a
 * backend-preferred flag. Bare existence of a name is never enough. */
function isRecommended(contact) {
  if (contact.preferred_for_submissions) return true;
  const role = String(contact.role || "").trim().toLowerCase();
  return RELEVANT_ROLES.has(role);
}

/* Evidence pages actually stored by the backend: the page the contact was
 * found on, then any per-fact provenance source pages. Never constructed. */
function evidenceUrls(contact) {
  const urls = [];
  const seen = new Set();
  const add = (url) => {
    if (url && typeof url === "string" && url.trim() && !seen.has(url)) {
      seen.add(url); urls.push(url);
    }
  };
  add(contact.source_url);
  for (const prov of contact.provenance || []) {
    if (prov && prov.source_url) add(prov.source_url);
  }
  return urls;
}

/* One discovered recipient as an individual professional card. */
function recipientCard(contact, stationName, identityKey, basket) {
  const uid = String(contact.contact_uid);
  const email = verifiedEmail(contact);
  const evidence = evidenceUrls(contact);
  const state = contact.identity_state || "unattributed_observation";
  const title = contact.name
    || (contact.role && contact.role !== "unknown" ? contact.role : null)
    || "(unnamed contact)";
  const selected = basket.has(uid);
  const reachable = Boolean(email);

  let routeStatus;
  let reachControl;
  if (reachable) {
    routeStatus = el("span", { class: "route-status ok" },
      "Verified email · ", el("strong", {}, email));
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
          station_name: stationName,
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
          station_name: stationName,
          email: contact.email,
          source_url: contact.source_url || null,
        });
      });
      reachControl = reach;
    }
  } else {
    routeStatus = el("span", { class: "route-status none" },
      "No verified outreach route found");
    reachControl = el("span", { class: "dim" },
      contact.phone ? `phone only: ${contact.phone}` : "not reachable");
  }

  const evidenceRow = evidence.length
    ? el("div", { class: "evidence-row" },
      el("span", { class: "dim" }, "Found on: "),
      el("a", {
        class: "evidence-link",
        href: evidence[0], target: "_blank", rel: "noopener noreferrer",
      }, evidence[0]),
      evidence.length > 1
        ? el("span", { class: "dim" }, ` (+${evidence.length - 1} more)`) : null)
    : el("div", { class: "dim evidence-row" }, "Found on: unknown");

  return el("article", { class: "contact-card recipient" },
    el("div", { class: "head" },
      el("span", { class: "name" }, title),
      ...identityBadge(contact),
      contact.preferred_for_submissions
        ? el("span", { class: "preferred-star",
          title: "backend-flagged preferred_for_submissions" },
          "★ preferred")
        : null,
      contact.role && contact.role !== "unknown"
        ? el("span", { class: "chip" }, contact.role) : null),
    el("div", { class: "route-status-line" }, routeStatus),
    evidenceRow,
    el("div", { class: "actions-row" },
      el("span", { class: "dim" },
        "Confidence ", el("strong", {}, fmtPct(contact.confidence_score))),
      confidenceBar(contact.confidence_score),
      el("span", { class: "grow" }, null),
      reachControl));
}

function contactsSection(title, hint, contacts, payload, identityKey, basket,
  empty, sectionId) {
  const cards = contacts.map((contact) =>
    recipientCard(contact, payload.station_name, identityKey, basket));
  const attrs = sectionId ? { class: "card", id: sectionId } : { class: "card" };
  return el("section", attrs,
    el("h2", {}, title),
    hint ? el("p", { class: "dim" }, hint) : null,
    cards.length
      ? cards
      : el("p", { class: "dim" }, empty || "None recorded."));
}

/* Section 1 — Recommended Contacts: highest value, evidence-backed targets. */
function recommendedContactsCard(contacts, payload, identityKey, basket) {
  const rec = (contacts || [])
    .filter(isRecommended)
    .sort((a, b) => b.confidence_score - a.confidence_score);
  return contactsSection(
    `Recommended contacts (${rec.length})`,
    "Backend-qualified outreach targets: exact relevant role or flagged "
      + "preferred_for_submissions.",
    rec, payload, identityKey, basket,
    "No recommended contacts with qualifying evidence on record.",
    "station-contacts");
}

/* Section 2 — Other Discovered People: lower relevance or weaker evidence. */
function otherContactsCard(contacts, payload, identityKey, basket) {
  const other = (contacts || [])
    .filter((c) => !isRecommended(c))
    .sort((a, b) => b.confidence_score - a.confidence_score);
  return contactsSection(
    `Other discovered people (${other.length})`,
    "Discovered but not yet qualified for outreach — lower confidence or "
      + "no clear music-outreach relevance. Shown for intelligence, not as "
      + "verified outreach targets.",
    other, payload, identityKey, basket,
    "No other discovered people on record.");
}

/* Section 3 — Station-Level Contact Routes: exact evidence-backed routes.
 *
 * URL routes come from the SAME single evidence-backed ``useful_pages`` list
 * that drives the Send Music action and Useful Pages section — never from the
 * separately path-selected ``submission.submission_url`` representation.
 * Submission-classified pages are shown here as openable routes; a station-
 * level email is a mailto handoff to the station's playlist/music team,
 * clearly labeled as a STATION route, never attributed to any individual.
 */
function stationRoutesSection(detail, intel, usefulPages, submissionData) {
  const urlRows = [];
  const seen = new Set();
  const addUrlRoute = (url, kind, note) => {
    if (!url || typeof url !== "string" || !/^https?:\/\//i.test(url)) return;
    if (seen.has(url)) return;
    seen.add(url);
    urlRows.push({ url, kind, note });
  };
  for (const p of submissionPages(usefulPages)) {
    addUrlRoute(p.url, "submission page",
      `${(p.label || "discovered submission page").trim()} — exact URL `
      + `discovered with provenance${p.source_url ? ` on ${p.source_url}` : ""}`);
  }

  // Reachability evidence recorded by the backend's link checks (read-only).
  const checks = (submissionData && submissionData.last_checks) || [];
  const reach = {};
  for (const c of checks) {
    if (c && c.url) reach[c.url] = c;
  }

  // Station-level email from exact backend evidence (never constructed).
  const stationEmail = extractStationEmail((intel && intel.submission) || null);

  const show = urlRows.length > 0 || stationEmail || detail.website;
  if (!show) return null;

  const intro = "Official routes the backend actually discovered — never a "
    + "constructed URL or address. These are station-level submission/"
    + "contact routes, not a specific person.";

  const urlRowsEl = urlRows.length > 0
    ? el("div", { class: "route-list" },
      urlRows.map((r) => {
        const check = reach[r.url];
        return el("div", { class: "route-row" },
          el("span", { class: "chip route-kind" }, r.kind),
          el("div", {},
            el("a", {
              href: r.url, target: "_blank", rel: "noopener noreferrer",
            }, r.url),
            el("div", { class: "dim" }, r.note),
            check
              ? el("div", { class: "dim" },
                check.ok === true
                  ? `reachable · status ${check.status}`
                  : `unreachable · status ${check.status ?? "n/a"}`)
              : null),
          el("a", { class: "open-route",
            href: r.url, target: "_blank", rel: "noopener noreferrer" },
            "Open Verified Contact Route"));
      }))
    : null;

  let emailRow = null;
  if (stationEmail) {
    emailRow = el("div", { class: "route-row station-email-route" },
      el("span", { class: "chip route-kind" }, "submission email"),
      el("div", {},
        el("span", { class: "route-address" }, stationEmail),
        el("div", { class: "dim" },
          "Station-level email from backend evidence (not an individual).")),
      el("a", { class: "open-route",
        href: `mailto:${stationEmail}` },
        "Open in My Email"));
  }

  return el("section", { class: "card", id: "station-routes" },
    el("h2", {}, "Station-level contact routes"),
    el("p", { class: "dim" }, intro),
    emailRow,
    urlRowsEl,
    (urlRows.length === 0 && !stationEmail)
      ? el("p", { class: "dim" },
        "No verified station-level contact route on record.")
      : null);
}

/* Exact station-level email from backend evidence; returns string or null.
 * Never constructed/inferred — only the stored value is accepted. */
function extractStationEmail(sub) {
  if (!sub) return null;
  let email = sub.submission_email;
  if (email && typeof email === "object") email = email.value;
  email = String(email || "").trim();
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) ? email : null;
}

function addAllToCampaign(detail, contactsPayload, identityKey, basket) {
  /* Bulk staging only ever adds RECOMMENDED, email-reachable contacts.
   * This keeps bulk outreach aligned with qualified, verified targets. */
  const staged = [];
  for (const contact of contactsPayload.contacts || []) {
    if (!isRecommended(contact)) continue;
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
 * Supporting cards (submission + collapsed intelligence details)
 * ------------------------------------------------------------------------- */

function submissionCard(submission, usefulPages) {
  if (!submission) {
    return el("section", { class: "card" },
      el("h2", {}, "Send music"),
      el("p", { class: "dim" }, "No submission route recorded."));
  }
  // The submission page is the evidence-backed useful page only — never the
  // separate path-selected submission_url representation.
  const submissionPage = submissionPages(usefulPages)[0] || null;
  return el("section", { class: "card" },
    el("h2", {}, "Send music"),
    el("dl", { class: "kv" }, [
      ["submission page", submissionPage
        ? el("span", {},
          externalLink(submissionPage.url, submissionPage.label || submissionPage.url),
          " ", factSource(submissionPage.source_url ? { source_url: submissionPage.source_url } : null))
        : "—"],
      ["email", submission.submission_email
        ? submission.submission_email.value
        : "—"],
      ["instructions", submission.instructions || "—"],
      ["restrictions", fmtList(submission.restrictions)],
    ]),
    el("p", { class: "dim" },
      submissionPage
        ? "Send music opens the exact discovered submission page above."
        : "Use the Send music action above; no verified submission route "
          + "was discovered for this station."));
}

function overviewCardTech(detail) {
  const socials = Object.entries(detail.social_urls || {});
  return kvCard("Station overview", [
    ["description", detail.description],
    ["language", detail.language],
    ["location", [detail.city, detail.state_or_region, detail.country]
      .filter(Boolean).join(", ") || null],
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

/* ---------------------------------------------------------------------------
 * View assembly
 * ------------------------------------------------------------------------- */

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
    root.replaceChildren(
      detailHead(detail),
      actionBar(detail, intel.useful_pages),
      usefulPagesCard(intel.useful_pages),
      recommendedContactsCard(contactsPayload.contacts, contactsPayload,
        identityKey, basket),
      otherContactsCard(contactsPayload.contacts, contactsPayload,
        identityKey, basket),
      stationRoutesSection(detail, intel, intel.useful_pages, submissionData),
      submissionCard(intel.submission, intel.useful_pages),
      intelligenceDetails(detail, intel, verification, submissionData,
        identityKey));

    // "Add to campaign" stages this station's selectable contacts.
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
