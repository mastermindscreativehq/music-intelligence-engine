/* Station inspection view: overview, contacts (confidence + source
 * attribution), submission path, epistemology, verification history,
 * and — additively since Phase 8 — backend-recorded link accessibility.
 *
 * All content comes verbatim from the Phase 4-8 endpoints; this view adds
 * presentation only. Inference labels (e.g. submission.methods
 * kind:"inference") are displayed AS inferences and never promoted. */

import { api, ApiError } from "../api.js";
import {
  chips,
  confidenceBar,
  el,
  fmtList,
  fmtPct,
} from "../dom.js";

const STATUS_CLASSES = ["verified", "conflicting", "failed", "stale",
  "unverified", "unsupported"];

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

const IDENTITY_BADGES = {
  named: { label: "named contact", class: "kind-badge named" },
  role_based: { label: "role-based contact", class: "kind-badge role" },
  unattributed_observation:
    { label: "observed value", class: "kind-badge observed" },
};

function identityBadge(contact) {
  const state = contact.identity_state || "unattributed_observation";
  const badge = IDENTITY_BADGES[state] ||
    IDENTITY_BADGES.unattributed_observation;
  const methodLabel = contact.method === "email" ? "email"
    : contact.method === "phone" ? "phone" : null;
  const bits = [el("span", { class: badge.class }, badge.label)];
  if (methodLabel && state === "unattributed_observation") {
    bits.push(el("span", {
      class: "dim",
    }, methodLabel + ", no person or role identified"));
  }
  return bits;
}

function contactCard(contact, stationName, identityKey, basket) {
  const uid = String(contact.contact_uid);

  const provenance = (contact.provenance || []).map((entry) =>
    el("li", {},
      entry.source_url
        ? externalLink(entry.source_url, entry.value ?? entry.source_url)
        : String(entry.value ?? "")));

  const reasons = (contact.confidence_reasons || []).length
    ? el("ul", { class: "provenance-list" },
      contact.confidence_reasons.map((reason) => el("li", {}, reason)))
    : null;

  let article;
  const toggle = el("button", {}, "");
  toggle.addEventListener("click", () => {
    if (basket.has(uid)) {
      basket.remove(uid);
    } else {
      basket.add({
        contact_uid: uid,
        identity_key: identityKey,
        station_name: stationName,
        name: contact.name,
        role: contact.role,
        email: contact.email,
      });
    }
    article.replaceWith(
      contactCard(contact, stationName, identityKey, basket));
  });

  const attributed = contact.identity_state === "named"
    || contact.identity_state === "role_based";
  const titleText = contact.name
    || (contact.role && contact.role !== "unknown" ? contact.role : null)
    || "(unnamed contact)";
  const normalizedNote =
    contact.value_normalized && contact.value_raw !== contact.value_normalized
      ? el("span", { class: "dim mono" },
        "normalized: " + contact.value_normalized)
      : null;
  const observationsNote = Number(contact.observations) > 1
    ? el("span", { class: "dim" },
      "same value stored on " + contact.observations + " pages")
    : null;

  article = el("article", { class: "contact-card" },
    el("div", { class: "head" },
      el("span", { class: "name" }, titleText),
      ...identityBadge(contact),
      contact.preferred_for_submissions
        ? el("span", { class: "preferred-star",
          title: "backend-flagged preferred_for_submissions" },
          "★ preferred")
        : null,
      attributed && contact.role
        ? el("span", { class: "chip" }, contact.role) : null),
    el("div", {},
      contact.email ? el("span", {}, contact.email + " ") : null,
      contact.phone ? el("span", { class: "dim" }, contact.phone) : null,
      normalizedNote ? el("div", {}, normalizedNote) : null,
      observationsNote),
    el("div", { class: "actions-row" },
      confidenceBar(contact.confidence_score),
      el("span", { class: "dim" }, fmtPct(contact.confidence_score)),
      attributed ? toggle : el("span", {
        class: "dim",
        title: "recipients need a person or role; anonymous observed "
          + "values are not selectable",
      }, "not a submission recipient")),
    reasons,
    provenance.length
      ? el("ul", { class: "provenance-list" },
        el("li", { class: "dim" }, "provenance:"), provenance)
      : null);

  const sync = () => {
    const selected = basket.has(uid);
    toggle.textContent = selected
      ? "✓ remove from recipients"
      : "+ add to recipients";
    toggle.className = selected ? "subtle" : "";
  };
  sync();
  unsubscribeFns.push(basket.subscribe(() => sync()));

  return article;
}

const unsubscribeFns = [];

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
    // Phase 8: submission + accessibility snapshot (tolerated missing).
    api.stationSubmission(identityKey).catch((error) =>
      error instanceof ApiError && error.status === 404
        ? null
        : Promise.reject(error)),
  ]).then(([detail, intel, contactsPayload, verification,
    submissionData]) => {
    root.replaceChildren(
      detailHead(detail),
      overviewCard(detail),
      contactsCard(contactsPayload, basket, identityKey),
      submissionCard(intel.submission),
      accessibilityCard(identityKey, submissionData),
      epistemologyCard(intel.epistemology),
      verificationCard(verification));
  }).catch((error) => {
    root.replaceChildren(errorBanner(error));
  });
}

export function teardownStationView() {
  for (const off of unsubscribeFns.splice(0)) off();
}

function overviewCard(detail) {
  const socials = Object.entries(detail.social_urls || {});
  return kvCard("Overview", [
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

function contactsCard(payload, basket, identityKey) {
  const cards = (payload.contacts || []).map((contact) =>
    contactCard(contact, payload.station_name, identityKey, basket));
  return el("section", { class: "card" },
    el("h2", {}, `Contacts (${cards.length})`),
    payload.preferred_submission_contacts &&
    payload.preferred_submission_contacts.length
      ? el("p", { class: "dim" },
        "★ marks backend-preferred submission contacts (computed by the ",
        "engine; this console does not re-rank).")
      : null,
    cards.length ? cards : el("p", { class: "dim" }, "No contacts recorded."));
}

function submissionCard(submission) {
  if (!submission) {
    return el("section", { class: "card" }, el("h2", {}, "Submission path"),
      el("p", { class: "dim" }, "No submission path recorded."));
  }
  const methods = submission.methods || {};
  const isInference = methods.kind === "inference";
  return kvCard("Submission path", [
    ["url", submission.submission_url
      ? el("span", {},
        externalLink(submission.submission_url.value), " ",
        factSource(submission.submission_url))
      : "—"],
    ["email", submission.submission_email
      ? submission.submission_email.value
      : "—"],
    ["methods", el("span", {},
      chips(methods.methods, isInference ? "inference" : ""),
      isInference
        ? el("span", { class: "chip inference",
          title: "labeled inference by the engine" }, "inference")
        : null)],
    ["method reasons", fmtList(methods.reasons)],
    ["instructions", submission.instructions],
    ["restrictions", fmtList(submission.restrictions)],
    ["path confidence", fmtPct(submission.confidence_score)],
    ["reasons", fmtList(submission.confidence_reasons)],
  ]);
}

/* Phase 8 (additive): backend-recorded reachability of this station's
 * submission links. Renders GET snapshot rows as reported; "run checks
 * now" triggers POST and shows the fresh summary. No URLs are ever built
 * client-side — every value displayed comes from the backend payloads. */
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
  const card = el("section", { class: "card" }, el("h2", {},
    "Link accessibility"));
  card.append(el("p", { class: "dim" },
    "Backend-recorded reachability of this station's submission links. ",
    "Checks run on demand against the stored submission targets; results "
    + "are recorded server-side and shown exactly as reported."));

  const rowsBox = el("div", {});
  const renderRows = (entries) => {
    rowsBox.replaceChildren(
      ...(entries && entries.length
        ? entries.map(checkEntryRow)
        : [el("p", { class: "dim" },
          "No link checks recorded for this station yet.")]));
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
        `latest run: ${summary.reachable} of ${summary.targets} targets `
        + "reachable"));
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
  return el("section", { class: "card" },
    el("h2", {}, "How to read this record"),
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
  const head = el("h2", {}, "Verification history");
  if (!data || (!(data.runs || []).length && !(data.results || []).length)) {
    return el("section", { class: "card" }, head,
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
  return el("section", { class: "card" }, head, runCards,
    el("h2", {}, "Claim results"), resultRows);
}
