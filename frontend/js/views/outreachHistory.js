/* Outreach history (Phase 9) — the persisted ledger of outreach records.
 *
 * Lists backend records with their status (draft | opened_in_email | sent |
 * failed) and the append-only attempt ledger. A record shown as
 * "opened in email" is never labeled "sent": only a provider-confirmed send
 * ever earns the `sent` status.
 */

import { api, ApiError } from "../api.js";
import { el } from "../dom.js";
import { outreachHref } from "../router.js";

function statusChip(status) {
  const known = ["draft", "opened_in_email", "sent", "failed"];
  const css = known.includes(status) ? ` ${status}` : "";
  return el("span", { class: `chip${css}` }, String(status ?? "unknown"));
}

function attemptRow(a) {
  const when = new Date(a.at).toLocaleString();
  return el("li", { class: "attempt-row" },
    statusChip(a.event),
    el("span", { class: "dim" }, `@ ${a.at} · ${a.provider || "local"}`),
    a.meta && a.meta.channel
      ? el("span", { class: "dim" }, ` · via ${a.meta.channel}`) : null);
}

export function renderOutreachHistoryView(root) {
  root.append(
    el("h1", {}, "Outreach history"),
    el("p", { class: "dim" },
      "Everything you drafted and opened — a traceable ledger. Nothing ",
      "here is marked sent unless a provider confirmed delivery."));

  const listSlot = el("div", { class: "card" },
    el("p", { class: "dim" }, "Loading outreach records…"));
  const refresh = el("button", { class: "subtle" }, "Refresh");
  const newOutreach = el("button", { class: "primary" }, "New outreach");
  newOutreach.addEventListener("click", () => {
    window.location.hash = outreachHref([]);
  });
  refresh.addEventListener("click", renderRecords);

  root.append(el("div", { class: "actions-row" }, newOutreach, refresh),
    listSlot);

  function renderRecords() {
    listSlot.replaceChildren(el("p", { class: "dim" },
      "Loading outreach records…"));
    api.listOutreach({ limit: 100 })
      .then((data) => {
        const records = data.outreach || [];
        if (records.length === 0) {
          listSlot.replaceChildren(
            el("p", { class: "dim" },
              "No outreach records yet. Draft one from the outreach composer.")
          );
          return;
        }
        listSlot.replaceChildren(...records.map((r) => {
          const recipient = r.recipient || {};
          const track = r.track;
          const body = [
            (r.subject ? el("strong", {}, r.subject) : null),
            el("div", { class: "receiver" },
              el("span", {}, recipient.name || "(unnamed)"),
              el("span", { class: "dim" },
                ` · ${recipient.email || "no email"}`),
              recipient.organization
                ? el("span", { class: "dim" }, ` · ${recipient.organization}`)
                : null),
            track
              ? el("div", { class: "dim" },
                  `Track: ${track.original_filename || "(unnamed)"}`)
              : null,
            r.updated_at
              ? el("div", { class: "dim" },
                  `updated ${new Date(r.updated_at).toLocaleString()}`)
              : null,
          ];
          const attempts = (r.attempts || []).map(attemptRow);
          return el("article", { class: "table-card card" },
            el("div", { class: "table-card-header" },
              el("div", { class: "table-card-title" },
                statusChip(r.status),
                el("span", { class: "dim record-id" }, r.outreach_id)),
              el("div", { class: "table-card-actions" },
                el("a", { href: r.links && r.links.self ? "#" + r.links.self : "#", class: "linkish" },
                  "details"))),
            body.length ? el("div", { class: "table-card-body" }, ...body) : null,
            attempts.length ? el("ol", { class: "attempt-list" }, ...attempts) : null);
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