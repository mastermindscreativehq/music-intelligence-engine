/* Minimal outreach composer (action-first workflow).
 *
 * Reaches NO backend: there is no send endpoint, and "nothing here ever
 * sends anything". This page helps an operator turn a selected recipient
 * (or several) into an outreach draft. It loads recipients from the
 * recipient basket by contact_uid, lets the operator review/deselect them,
 * compose a subject + body draft that persists locally (sessionStorage),
 * and hands off to their own email client via a mailto: link or by copying
 * the finished message. Nothing leaves this browser except what the
 * operator explicitly chooses to send from their own mail client.
 */

import { el } from "../dom.js";
import { stationHref } from "../router.js";

const DRAFT_PREFIX = "mie.outreach.draft.";

function draftKey(uids) {
  return DRAFT_PREFIX + [...uids].sort().join(":");
}

function loadDraft(uids) {
  try {
    const raw = JSON.parse(
      window.sessionStorage.getItem(draftKey(uids)) || "null");
    if (raw && typeof raw === "object") {
      return {
        subject: typeof raw.subject === "string" ? raw.subject : "",
        body: typeof raw.body === "string" ? raw.body : "",
        from: typeof raw.from === "string" ? raw.from : "",
      };
    }
  } catch (error) {
    /* fall through to empty draft */
  }
  return { subject: "", body: "", from: "" };
}

function saveDraft(uids, draft) {
  try {
    window.sessionStorage.setItem(draftKey(uids),
      JSON.stringify({
        subject: draft.subject || "",
        body: draft.body || "",
        from: draft.from || "",
      }));
  } catch (error) {
    /* storage unavailable: keep the in-memory draft */
  }
}

function errorBanner(message) {
  return el("div", { class: "banner-error", role: "alert" },
    el("strong", {}, message));
}

function recipientLabel(item) {
  const parts = [
    item.name || "unnamed",
    item.station_name,
    item.email,
  ].filter((value) => value && value.trim());
  return parts.join(" · ");
}

export function renderOutreachView(root, uids, basket) {
  // Resolve each requested uid; gracefully skip ones no longer in the basket.
  const recipients = uids
    .map((uid) => basket.get(uid))
    .filter(Boolean);

  if (recipients.length === 0) {
    root.append(
      errorBanner("No recipients selected for outreach yet."),
      el("section", { class: "card" },
        el("p", { class: "dim" },
          "Select a recipient on a station page (Add Recipient), then come ",
          "back here to compose. ",),
        el("div", { class: "actions-row" },
          el("a", { class: "primary", href: "#/" },
            "Find stations"))));
    return;
  }

  const selected = recipients.map((item) => String(item.contact_uid));
  const draft = loadDraft(selected);

  const subjectInput = el("input", {
    type: "text", placeholder: "Subject", autocomplete: "off",
    value: draft.subject,
  });
  const bodyInput = el("textarea", {
    rows: "12", placeholder: "Message body",
    autocomplete: "off",
  });
  bodyInput.value = draft.body;
  const fromInput = el("input", {
    type: "email", placeholder: "you@youremail.com", autocomplete: "off",
    value: draft.from,
    spellcheck: "false",
  });

  const statusLine = el("div", { class: "dim draft-status" },
    "Draft saved locally in this browser session.");

  let saved = true;
  const markDirty = () => { saved = false; };
  const save = () => {
    saveDraft(selected, {
      subject: subjectInput.value,
      body: bodyInput.value,
      from: fromInput.value,
    });
    saved = true;
    statusLine.textContent = "Draft saved locally in this browser session.";
  };
  subjectInput.addEventListener("input", markDirty);
  bodyInput.addEventListener("input", markDirty);
  fromInput.addEventListener("input", markDirty);

  /* One traceable recipient block: who they are + the exact evidence. */
  const recipientBlocks = recipients.map((item) => {
    const email = (item.email && item.email.trim()) || null;
    const source = (item.source_url && item.source_url.trim()) || null;
    return el("div", { class: "recipient-evidence" },
      el("div", { class: "recipient-tags" },
        el("span", { class: "chip recipient-tag" }, recipientLabel(item)),
        el("button", {
          class: "linkish tag-remove",
          onClick: () => deselect(String(item.contact_uid)),
        }, "remove")),
      el("div", { class: "dim context-line" },
        item.station_name ? `${item.station_name} · ` : "",
        email ? `email verified: ${email}` : "no verified email on record"),
      source
        ? el("div", { class: "dim context-line" },
          "Evidence: ", el("a", {
            href: source, target: "_blank", rel: "noopener noreferrer",
          }, source))
        : el("div", { class: "dim context-line" },
          "Evidence: no source on record"));
  });

  const copyButton = el("button", { class: "primary" }, "Copy message");
  copyButton.addEventListener("click", async () => {
    if (!bodyInput.value.trim()) {
      statusLine.textContent = "Nothing to copy yet — write a message first.";
      return;
    }
    const head = fromInput.value.trim()
      ? [`From: ${fromInput.value.trim()}`, "To: " + recipients
        .map((item) => item.email).filter(Boolean).join(", ")]
      : ["To: " + recipients
        .map((item) => item.email).filter(Boolean).join(", ")];
    const text = [head.join("\n"), bodyInput.value.trim()].join("\n\n");
    try {
      await navigator.clipboard.writeText(text + "\n");
      statusLine.textContent = "Message copied to clipboard.";
    } catch (error) {
      statusLine.textContent = "Could not copy automatically; use Open in email.";
    }
  });

  const mailButton = el("button", {}, "Open in email");
  mailButton.addEventListener("click", () => {
    const emails = recipients
      .map((item) => item.email)
      .filter((value) => value && value.trim());
    if (emails.length === 0) {
      statusLine.textContent =
        "No verified email on this recipient — draft it yourself.";
      return;
    }
    const to = emails.join(",");
    const params = new URLSearchParams({
      subject: subjectInput.value,
      body: bodyInput.value,
    });
    window.location.href = `mailto:${to}?${decodeURIComponent(params.toString())}`;
  });

  const deselect = (uid) => {
    const remaining = recipients.filter((item) =>
      String(item.contact_uid) !== uid);
    if (remaining.length === 0) {
      window.location.hash = stationHref(recipients[0].identity_key);
      return;
    }
    // re-render with the remaining set
    window.location.hash = "#/outreach?recipient=" +
      remaining.map((item) => encodeURIComponent(item.contact_uid)).join(",");
  };

  root.append(
    el("section", { class: "card" },
      el("h2", {}, "Start outreach"),
      el("p", { class: "dim" },
        "Compose a message to the selected recipient(s), then hand it off ",
        "to your own email — this console never sends anything."),
      el("div", {},
        el("div", { class: "field-label" }, "Recipients"),
        recipientBlocks),
      el("label", { class: "field" },
        el("span", {}, "From (your email)"),
        fromInput,
        el("span", { class: "dim hint" },
          "Your own address — the message is handed to your mail client.")),
      el("label", { class: "field" }, el("span", {}, "Subject"), subjectInput),
      el("label", { class: "field" }, el("span", {}, "Message"), bodyInput),
      el("div", { class: "actions-row" },
        copyButton, mailButton,
        el("button", {
          class: "subtle",
          onClick: save,
        }, "Save draft")),
      statusLine));

  window.addEventListener("beforeunload", () => { if (!saved) saveDraft(
    selected, { subject: subjectInput.value, body: bodyInput.value,
      from: fromInput.value }); });
}
