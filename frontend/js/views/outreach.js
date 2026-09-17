/* Outreach page (#/outreach) — ACTIVE outreach records.
 *
 * This is the record system: every row is a persisted outreach record
 * (recipient + station + release + status + attempt history) from the
 * backend ledger, not an ephemeral browser list. Records are prepared on a
 * station page (CONTACTS → OUTREACH → Start outreach) and managed here:
 * open the station, compose/hand off the record, and log real events
 * (sent / responded / follow_up / failed / closed) that advance the status.
 *
 * Active statuses: ready | sent | responded | follow_up. The full ledger
 * (including failed/closed + every attempt) lives on #/outreach-history.
 *
 * The single source of truth is the backend; this view only renders it. */

import { api, ApiError } from "../api.js";
import { el } from "../dom.js";
import { outreachHistoryHref, stationHref, stationsHref } from "../router.js";
import { openOutreachModal } from "./outreachModal.js";
import {
  attemptRow,
  detailToggle,
  removeOutreachButton,
  statusChip,
  trackLine,
} from "./outreachRecords.js";

const ACTIVE_STATUSES = ["ready", "sent", "responded", "follow_up"];
const EVENT_CHOICES = [
  ["sent", "sent"],
  ["responded", "responded"],
  ["follow_up", "follow up"],
  ["failed", "failed"],
  ["closed", "closed"],
];

function externalLink(url, text) {
  return el("a", { href: url, target: "_blank", rel: "noopener noreferrer" },
    text ?? url);
}

function recipientLine(record) {
  const recipient = record.recipient || {};
  return el("div", { class: "receiver" },
    el("span", {}, recipient.name || "(unnamed)"),
    el("span", { class: "dim" },
      ` · ${recipient.email || recipient.submission_url || "no direct route"}`),
    recipient.organization
      ? el("span", { class: "dim" }, ` · ${recipient.organization}`)
      : null);
}

function eventControl(record, reload) {
  const select = el("select", { name: "event" },
    EVENT_CHOICES.map(([value, label]) => el("option", { value }, label)));
  const button = el("button", { class: "subtle" }, "Log event");
  button.addEventListener("click", async () => {
    if (button.disabled) return;
    button.disabled = true;
    button.textContent = "logging…";
    try {
      await api.outreachEvent(record.outreach_id, select.value,
        { applied_by: "console" });
      reload();
    } catch (error) {
      button.disabled = false;
      button.textContent = "Log event";
      /* surface the failure next to the control */
      const msg = el("span", { class: "dim", role: "alert" },
        `Could not log: ${
          error instanceof ApiError ? error.message : String(error)}`);
      button.after(msg);
    }
  });
  return el("div", { class: "event-control" },
    el("span", { class: "dim" }, "Record event: "),
    select, " ", button);
}

function recordCard(record, reload) {
  const recipient = record.recipient || {};
  const details = detailToggle(() => api.getOutreach(record.outreach_id));

  const openStation = el("a", {
    class: "linkish",
    href: stationHref(recipient.identity_key || ""),
  }, "Open station");

  let primaryAction;
  const email = String(recipient.email || "").trim();
  if (email) {
    primaryAction = el("button", {
      class: "buttonish",
      onClick: () => openOutreachModal({ recipient, record }),
    }, "Compose & hand off");
  } else {
    primaryAction = el("a", {
      class: "linkish",
      href: recipient.submission_url || "#",
      target: "_blank",
      rel: "noopener noreferrer",
    }, "Open submission page");
    if (!recipient.submission_url) primaryAction.classList.add("dim");
  }

  const body = [
    (record.subject ? el("strong", {}, record.subject) : null),
    recipientLine(record),
    trackLine(record),
    record.updated_at
      ? el("div", { class: "dim" },
        `updated ${new Date(record.updated_at).toLocaleString()}`)
      : null,
  ];
  const attempts = (record.attempts || []).map(attemptRow);

  return el("article", { class: "table-card card outreach-record" },
    el("div", { class: "table-card-header" },
      el("div", { class: "table-card-title" },
        statusChip(record.status),
        el("span", { class: "dim record-id" }, record.outreach_id)),
      el("div", { class: "table-card-actions actions-row" },
        openStation, primaryAction, details.button,
        removeOutreachButton(record, reload))),
    body.length ? el("div", { class: "table-card-body" }, ...body) : null,
    el("div", { class: "table-card-actions actions-row" },
      eventControl(record, reload)),
    attempts.length
      ? el("ol", { class: "attempt-list" }, ...attempts)
      : null,
    details.slot);
}

export function renderOutreachView(root) {
  root.append(
    el("h1", {}, "Outreach"),
    el("p", { class: "dim" },
      "Active outreach records you prepared from station pages. Open a ",
      "record, hand it to your email client, and log what happens — the ",
      "record answers who you reached, for which release, and where it stands."));

  const listSlot = el("div", { class: "outreach-list" },
    el("p", { class: "dim" }, "Loading outreach records…"));
  const refresh = el("button", { class: "subtle" }, "Refresh");
  root.append(
    el("div", { class: "actions-row" },
      el("a", { class: "linkish", href: stationsHref },
        "Prepare records from a station →"),
      el("a", { class: "linkish", href: outreachHistoryHref() },
        "Full activity log →"),
      refresh),
    listSlot);

  function renderRecords() {
    listSlot.replaceChildren(el("p", { class: "dim" },
      "Loading outreach records…"));
    api.listOutreach({ limit: 200 })
      .then((data) => {
        const active = (data.outreach || []).filter((r) =>
          ACTIVE_STATUSES.includes(r.status));
        if (active.length === 0) {
          listSlot.replaceChildren(
            el("section", { class: "card" },
              el("h2", {}, "No active outreach records"),
              el("p", { class: "dim" },
                "Open a station, select the people who decide about music, ",
                "pick a release, and click \"Start outreach\" to prepare ",
                "records here."),
              el("div", { class: "actions-row" },
                el("a", { class: "primary", href: stationsHref },
                  "Browse stations"))));
          return;
        }
        listSlot.replaceChildren(...active.map((r) => recordCard(r, renderRecords)));
      })
      .catch((error) => {
        const detail = error instanceof ApiError ? error.message : String(error);
        listSlot.replaceChildren(
          el("p", { class: "banner-error", role: "alert" },
            `Could not load outreach records: ${detail}`));
      });
  }

  refresh.addEventListener("click", renderRecords);
  renderRecords();
}