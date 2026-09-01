/* Personal outreach composer modal.
 *
 * Reach out to ONE verified individual recipient. Hand-held, hand-off work:
 *
 *  - Recipient and email come ONLY from verified backend evidence already in
 *    the recipient basket. The email shown and mailed is the exact stored
 *    value — this module never constructs or infers an address.
 *  - If the recipient has no verified email, no composer is offered here;
 *    the caller is responsible for showing "No verified outreach route found".
 *  - Handoff only via mailto (Open in My Email), clipboard Copy Email / Copy
 *    Message. Nothing is ever sent by the application.
 *
 * New rendering is appended/removed through overlay helpers (a layered
 * <div> view); it re-uses el() and addEventListener, never innerHTML.
 */

import { el } from "../dom.js";

const DRAFT_PREFIX = "mie.outreach.draft.";
const SENDER_PREF_KEY = "mie.outreach.from.address";

export function senderPref() {
  try {
    return String(window.localStorage.getItem(SENDER_PREF_KEY) || "").trim();
  } catch (error) {
    return "";
  }
}

function setSenderPref(value) {
  try {
    if (value && value.trim()) {
      window.localStorage.setItem(SENDER_PREF_KEY, value.trim());
    } else {
      window.localStorage.removeItem(SENDER_PREF_KEY);
    }
  } catch (error) {
    /* storage unavailable: skip persisting */
  }
}

function draftKey(uid) {
  return DRAFT_PREFIX + String(uid);
}

function loadDraft(uid) {
  try {
    const raw =
      JSON.parse(window.sessionStorage.getItem(draftKey(uid)) || "null");
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

function saveDraft(uid, draft) {
  try {
    window.sessionStorage.setItem(draftKey(uid), JSON.stringify({
      subject: draft.subject || "",
      body: draft.body || "",
      from: draft.from || "",
    }));
  } catch (error) {
    /* storage unavailable: keep in-memory */
  }
}

/* mailto handoff built only from the exact verified email. */
function openInMyEmail(email, subject, body) {
  const params = new URLSearchParams({ subject, body });
  window.location.href = `mailto:${email}?${decodeURIComponent(params.toString())}`;
}

async function copyText(text, statusElement, okMessage) {
  try {
    await navigator.clipboard.writeText(text);
    if (statusElement) statusElement.textContent = okMessage;
    return true;
  } catch (error) {
    if (statusElement) {
      statusElement.textContent =
        "Copy blocked by the browser — use Open in My Email instead.";
    }
    return false;
  }
}

/* Modal overlay helper: a full-screen dim layer containing a centered card. */
function openOverlay(...children) {
  const backdrop = el("div", { class: "outreach-backdrop", role: "dialog" });
  const panel = el("div", { class: "outreach-modal", role: "document" },
    ...children);
  backdrop.append(panel);
  backdrop.addEventListener("click", (event) => {
    if (event.target === backdrop) closeOverlay(backdrop);
  });
  document.body.append(backdrop);
  return backdrop;
}

function closeOverlay(backdrop) {
  if (backdrop && backdrop.parentNode) backdrop.parentNode.removeChild(backdrop);
}

/* ---------------- shared message building (no constructed addresses) ------- */

function copyEmailText(recipient) {
  return recipient.email || "";
}

function copyMessageText(recipient, draft) {
  const lines = [];
  lines.push("To: " + (recipient.email || ""));
  if (draft.from && draft.from.trim()) lines.push("From: " + draft.from.trim());
  if (draft.subject && draft.subject.trim()) lines.push("Subject: " + draft.subject.trim());
  lines.push("");
  lines.push(draft.body || "");
  return lines.join("\n");
}

/* ---- verified recipient read-only context panel (facts only, never
 * invented — name/role/station/email/evidence come straight from evidence) */

function recipientContext(recipient, email) {
  const name = recipient.name || "unnamed";
  const role = recipient.role || "—";
  const station = recipient.station_name || "—";
  const evidence = recipient.source_url || null;

  const emailLine = el("div", { class: "oc-line" },
    el("span", { class: "oc-label" }, "Email"),
    el("span", { class: "email-readonly" },
      el("span", { class: "verified" }, email),
      el("span", { class: "verified-badge", title: "Exact address stored in verified backend evidence" },
        "✓ verified"),
      el("span", { class: "dim" }, "read-only")));

  const lines = [
    el("div", { class: "oc-line" },
      el("span", { class: "oc-label" }, "Recipient"),
      el("strong", {}, name)),
    el("div", { class: "oc-line" },
      el("span", { class: "oc-label" }, "Role"),
      el("span", {}, role)),
    el("div", { class: "oc-line" },
      el("span", { class: "oc-label" }, "Organization"),
      el("span", {}, station)),
    emailLine,
    el("div", { class: "oc-line" },
      el("span", { class: "oc-label" }, "Evidence"),
      evidence
        ? el("a", { href: evidence, target: "_blank",
          rel: "noopener noreferrer" }, evidence)
        : el("span", { class: "dim" }, "no source on record")),
  ];

  return el("section", { class: "oc-panel" },
    el("div", { class: "oc-panel-title" },
      el("span", {}, "Verified recipient"),
      el("span", { class: "dim" }, "evidence-backed, read-only")),
    el("div", { class: "outreach-context" }, ...lines));
}

/* Factual, edit-friendly opening for the message, assembled ONLY from
 * verified fields present. If the name/role/station pieces are missing it
 * degrades gracefully — it never invents a name, role, or organization. */
function suggestedOpening(recipient) {
  const name = (recipient.name || "").trim();
  const role = (recipient.role || "").trim();
  const station = (recipient.station_name || "").trim();
  const parts = [];
  if (name) parts.push(name);
  const roleLabel =
    role && role !== "unknown"
      ? role.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())
      : "";
  if (roleLabel) parts.push(roleLabel);
  if (station) parts.push("at " + station);
  if (parts.length === 0) return "Hello,";
  return "Hi " + parts.join(", ") + ",";
}

/* ---- modal for a verified single recipient (email route) ---------------- */

export function openOutreachModal(recipient) {
  const email = String(recipient.email || "").trim();
  if (!email) return null; // no verified email -> caller shows the no-route state

  const uid = String(recipient.contact_uid);
  const draft = loadDraft(uid);
  const fromPref = senderPref();

  const status = el("div", { class: "dim dialect-status" });
  const subjectInput = el("input", {
    type: "text", placeholder: "Subject", autocomplete: "off",
    value: draft.subject,
  });
  const bodyInput = el("textarea", {
    rows: "12", placeholder: "Write your message, then hand it to your email client",
    autocomplete: "off",
  });
  bodyInput.value = draft.body;
  const addGreeting = () => {
    const body = bodyInput.value;
    const trimmedLead = body.replace(/^\s+/, "");
    if (/^(hi|hello|dear)\b/i.test(trimmedLead)) {
      status.textContent = "A greeting is already at the top of your message.";
      bodyInput.focus();
      return;
    }
    bodyInput.value = suggestedOpening(recipient) + "\n\n" + trimmedLead;
    save("Personalized greeting added from verified evidence.");
    bodyInput.focus();
  };
  const fromInput = el("input", {
    type: "text", placeholder: "your address (optional)", autocomplete: "off",
    spellcheck: "false",
    value: draft.from || fromPref,
  });

  const markDirty = () => { setSenderPref(fromInput.value); };
  subjectInput.addEventListener("input", markDirty);
  bodyInput.addEventListener("input", markDirty);
  fromInput.addEventListener("input", markDirty);

  const save = (message) => {
    saveDraft(uid, { subject: subjectInput.value, body: bodyInput.value,
      from: fromInput.value });
    setSenderPref(fromInput.value);
    status.textContent = message || "Draft saved in this browser session.";
  };

  const mailButton = el("button", { class: "primary" }, "Open in My Email");
  mailButton.addEventListener("click", () => {
    openInMyEmail(email, subjectInput.value, bodyInput.value);
  });

  const copyEmailButton = el("button", {}, "Copy Email");
  copyEmailButton.addEventListener("click", () =>
    copyText(copyEmailText(recipient), status, "Recipient email copied."));

  const copyMessageButton = el("button", {}, "Copy Message");
  copyMessageButton.addEventListener("click", () => {
    const text = copyMessageText(recipient, {
      from: fromInput.value, subject: subjectInput.value,
      body: bodyInput.value,
    });
    copyText(text, status, "Full message copied.");
  });

  const closeButton = el("button", { class: "linkish" }, "Close");
  closeButton.addEventListener("click", () => {
    save("Draft saved in this browser session.");
    closeOverlay(backdrop);
  });

  const backdrop = openOverlay(
    el("header", { class: "outreach-modal-header" },
      el("h2", {}, "Reach out"),
      el("p", { class: "dim" },
        "Compose an individualized message, then open in your email client ",
        "for review and sending. This console never sends anything.")),
    recipientContext(recipient, email),
    el("section", { class: "oc-panel oc-draft" },
      el("div", { class: "oc-panel-title" },
        el("span", {}, "Your message"),
        el("span", { class: "dim" }, "edit freely before handing off")),
      el("button", { class: "oc-greeting-btn", onClick: addGreeting },
        "Add personalized greeting from verified evidence"),
      el("label", { class: "field" },
        el("span", {}, "From (your email)"),
        fromInput,
        el("span", { class: "dim hint" },
          "Your own address, saved only in this browser — not verified by ",
          "this console.")),
      el("label", { class: "field" }, el("span", {}, "Subject"), subjectInput),
      el("label", { class: "field" }, el("span", {}, "Message"), bodyInput)),
    el("div", { class: "actions-row outreach-actions" },
      mailButton, copyEmailButton, copyMessageButton,
      el("button", { class: "subtle", onClick: () => save(
        "Draft saved in this browser session.") }, "Save draft"),
      closeButton),
    status);

  return backdrop;
}
