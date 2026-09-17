/* Outreach activity log (#/outreach-history) — the FULL persisted ledger.
 *
 * Every outreach record ever prepared, including failed/closed ones and the
 * append-only attempt history. Nothing is "sent" here unless a
 * provider-confirmed send earned that status; 'opened in email' handoffs
 * stay attempts. Records open their station right from the row.
 */

import { api, ApiError } from "../api.js";
import { el } from "../dom.js";
import { stationsHref, stationHref } from "../router.js";
import {
  attemptRow,
  detailToggle,
  removeOutreachButton,
  statusChip,
  trackLine,
} from "./outreachRecords.js";

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

export function renderOutreachHistoryView(root) {
  root.append(
    el("h1", {}, "Outreach activity log"),
    el("p", { class: "dim" },
      "The traceable ledger of every outreach record, with all attempts. ",
      "Nothing here is marked sent unless a provider confirmed delivery."));

  const listSlot = el("div", { class: "outreach-history-list" });
  const refresh = el("button", { class: "subtle" }, "Refresh");
  const newOutreach = el("button", { class: "primary" }, "New outreach");
  newOutreach.addEventListener("click", () => {
    window.location.hash = stationsHref;
  });
  refresh.addEventListener("click", renderRecords);

  root.append(
    el("div", { class: "actions-row" }, newOutreach, refresh),
    listSlot);

  function renderRecords() {
    listSlot.replaceChildren(el("p", { class: "dim" },
      "Loading outreach records…"));
    api.listOutreach({ limit: 200 })
      .then((data) => {
        const records = data.outreach || [];
        if (records.length === 0) {
          listSlot.replaceChildren(
            el("p", { class: "dim" },
              "No outreach records yet. Open a station and prepare records ",
              "from its OUTREACH section."));
          return;
        }
        listSlot.replaceChildren(...records.map((record) => {
          const recipient = record.recipient || {};
          const details = detailToggle(() => api.getOutreach(record.outreach_id));
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
                el("a", {
                  class: "linkish",
                  href: stationHref(recipient.identity_key || ""),
                }, "Open station"),
                details.button,
                removeOutreachButton(record, renderRecords))),
            body.length ? el("div", { class: "table-card-body" }, ...body) : null,
            attempts.length
              ? el("ol", { class: "attempt-list" }, ...attempts)
              : null,
            details.slot);
        }));
      })
      .catch((error) => {
        const detail = error instanceof ApiError ? error.message : String(error);
        listSlot.replaceChildren(
          el("p", { class: "banner-error", role: "alert" },
            `Could not load outreach history: ${detail}`));
      });
  }

  renderRecords();
}