/* Shared outreach-record rendering (ledger cards, status chips, attempts,
 * expandable details). Used by the active-records view (#/outreach) and the
 * full-ledger view (#/outreach-history) so both surfaces always agree about
 * status vocabulary and what a record looks like — no duplicated variants.
 *
 * Status vocabulary (canonical): ready | sent | responded | follow_up |
 * failed | closed. 'opened_in_email' appears only in the attempts ledger (a
 * mail-client handoff, never a stored status). Everything renders via el()
 * text nodes only — nothing here is interpolated into markup. */

import { el } from "../dom.js";
import { api, ApiError } from "../api.js";

export const OUTREACH_STATUSES = [
  "ready", "sent", "responded", "follow_up", "failed", "closed",
];
export const OUTREACH_EVENTS = [
  "opened_in_email", "sent", "responded", "follow_up", "failed", "closed",
];

export const STATUS_LABELS = {
  ready: "ready",
  sent: "sent",
  responded: "responded",
  follow_up: "follow up",
  failed: "failed",
  closed: "closed",
  opened_in_email: "opened in email",
};

export function statusChip(status) {
  const value = String(status ?? "unknown");
  const known = OUTREACH_STATUSES.includes(value)
    || OUTREACH_EVENTS.includes(value);
  return el("span", { class: `chip${known ? ` status-${value}` : ""}` },
    STATUS_LABELS[value] || value);
}

export function attemptRow(attempt) {
  const when = new Date(attempt.at).toLocaleString();
  return el("li", { class: "attempt-row" },
    statusChip(attempt.event),
    el("span", { class: "dim" },
      `@ ${attempt.at} · ${attempt.provider || "local"}`),
    attempt.meta && attempt.meta.channel
      ? el("span", { class: "dim" }, ` · via ${attempt.meta.channel}`)
      : null);
}

export function detailPairs(record) {
  const recipient = record.recipient || {};
  const pairs = [
    ["status", record.status],
    ["outreach id", record.outreach_id],
    ["class", record.outreach_class],
    ["recipient", recipient.name || null],
    ["role", recipient.role || null],
    ["organization", recipient.organization || null],
    ["email", recipient.email || null],
    ["submission url", recipient.submission_url || null],
    ["source url", recipient.source_url || null],
    ["contact uid", recipient.contact_uid || null],
    ["station", recipient.identity_key || null],
    ["subject", record.subject || null],
    ["message", record.message || null],
    ["from", record.from_email || null],
    ["context", record.context ? JSON.stringify(record.context) : null],
    ["sharing", record.sharing ? JSON.stringify(record.sharing) : null],
    ["provider", record.provider],
    ["created", record.created_at],
    ["updated", record.updated_at],
  ];
  const rows = pairs
    .filter(([, value]) =>
      value !== null && value !== undefined && String(value).trim() !== "")
    .map(([key, value]) => [el("dt", {}, key), el("dd", {}, String(value))]);
  if (record.track && typeof record.track === "object") {
    const track = record.track;
    rows.push([el("dt", {}, "track"),
      el("dd", {},
        `${track.original_filename || "(unnamed)"} · ${track.track_id || ""}`)]);
  }
  return rows;
}

/* Expandable details block: loads the full record in place from the same
 * detail endpoint the backend serves (no secondary page). */
export function detailToggle(getFull) {
  const button = el("button", { class: "linkish" }, "details");
  const slot = el("div", { class: "outreach-detail-slot" });
  let open = false;
  button.addEventListener("click", async () => {
    if (open) {
      slot.replaceChildren();
      open = false;
      button.textContent = "details";
      return;
    }
    button.disabled = true;
    button.textContent = "loading…";
    try {
      const full = await getFull();
      const attempts = (full.attempts || []).map(attemptRow);
      slot.replaceChildren(
        el("dl", { class: "kv" }, detailPairs(full).flat()),
        attempts.length ? el("ol", { class: "attempt-list" }, ...attempts)
          : el("p", { class: "dim" }, "No attempts recorded yet."));
      open = true;
      button.textContent = "hide details";
    } catch (error) {
      slot.replaceChildren(
        el("p", { class: "dim", role: "alert" },
          `Could not load record details: ${
            error && error.message ? error.message : String(error)}`));
    } finally {
      button.disabled = false;
    }
  });
  return { button, slot };
}

export function trackLine(record) {
  const track = record.track;
  if (!track || typeof track !== "object") return null;
  return el("div", { class: "dim" },
    `Music: ${track.original_filename || "(unnamed)"}`);
}

/* Single shared Remove control for a persisted outreach record. Deleting an
 * outreach record removes the ledger row (and its attempt history) from the
 * console only — station data and the release track are never touched.
 * The two-step interaction is the minimal safe confirmation this app has
 * (no in-app confirm pattern exists); failures surface next to the button. */
export function removeOutreachButton(record, reload) {
  const button = el("button", { class: "subtle" }, "Remove");
  button.addEventListener("click", () => {
    if (button.disabled) return;
    if (!window.confirm(
      `Remove outreach record ${record.outreach_id}? This deletes the record `
      + "and its attempt history from the console. Station data is untouched.")) {
      return;
    }
    button.disabled = true;
    button.textContent = "removing…";
    api.deleteOutreach(record.outreach_id)
      .then(() => { if (typeof reload === "function") reload(); })
      .catch((error) => {
        button.disabled = false;
        button.textContent = "Remove";
        const message = error instanceof ApiError
          ? error.message
          : String(error);
        button.after(el("span", { class: "dim", role: "alert" },
          `Could not remove: ${message}`));
      });
  });
  return button;
}