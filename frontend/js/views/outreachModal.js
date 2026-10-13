/* Outreach composer modal — the single composer for the whole app.
 *
 * One canonical action, two shapes — both funnel into a real outreach
 * RECORD that lives in the ledger (no duplicate composers, no parallel
 * campaign system):
 *
 *  - compose mode (from a station/DJ page, the opportunities view, or the
 *    outreach page for a fresh handoff): pick a release from My music,
 *    compose a subject + message (+ the release's saved music link), then
 *    either CREATE the record or open the operator's own email app.
 *  - record mode (existing record from the outreach page): prefilled from
 *    the record and still editable. "Open in My Email" hands the message to
 *    the operator's email client (a real mailto:), recorded as an
 *    'opened_in_email' ATTEMPT. The record is marked 'sent' ONLY when the
 *    operator explicitly confirms they sent it (or a provider-confirmed
 *    send does so) — never because a mail client opened.
 *
 * Google sign-in is NOT required to prepare, edit, or hand off outreach.
 * The mailto handoff is the primary, account-free path; a connected Gmail
 * account adds an optional in-app send but never gates the workflow.
 *
 * Recipient & email come ONLY from verified backend evidence; this module
 * constructs no addresses, and when no music link is saved it emits none —
 * nothing is fabricated.
 *
 * Rendered through overlay helpers (a layered <div> view); re-uses el() and
 * addEventListener, never innerHTML.
 */

import { api } from "../api.js";
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
        link: typeof raw.link === "string" ? raw.link : "",
      };
    }
  } catch (error) {
    /* fall through to empty draft */
  }
  return { subject: "", body: "", from: "", link: "" };
}

function saveDraft(uid, draft) {
  try {
    window.sessionStorage.setItem(draftKey(uid), JSON.stringify({
      subject: draft.subject || "",
      body: draft.body || "",
      from: draft.from || "",
      link: draft.link || "",
    }));
  } catch (error) {
    /* storage unavailable: keep in-memory */
  }
}

/* Build the mailto handoff from the exact verified email only. Spaces are
 * percent-encoded as %20 (URLSearchParams emits '+') so every mail client
 * renders the body faithfully. No address is ever constructed here. */
export function mailtoHref(email, subject, body) {
  const params = new URLSearchParams();
  if (subject) params.set("subject", subject);
  if (body) params.set("body", body);
  const query = params.toString().replace(/\+/g, "%20");
  return `mailto:${email}${query ? `?${query}` : ""}`;
}

/* Fold the selected release's SAVED music link into the message body exactly
 * once, and only when a real link exists — never fabricated, never guessed. */
export function withMusicLink(body, link) {
  const text = String(body || "");
  const url = String(link || "").trim();
  if (!url || text.includes(url)) return text;
  const trimmed = text.replace(/\s+$/, "");
  return `${trimmed}${trimmed ? "\n\n" : ""}You can listen here:\n${url}\n`;
}

/* mailto handoff built only from the exact verified email. */
function openInMyEmail(email, subject, body) {
  window.location.href = mailtoHref(email, subject, body);
}

async function copyText(text, status, okMessage) {
  try {
    await navigator.clipboard.writeText(text);
    if (status) status.textContent = okMessage;
    return true;
  } catch (error) {
    if (status) {
      status.textContent =
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
  if (draft.subject && draft.subject.trim()) {
    lines.push("Subject: " + draft.subject.trim());
  }
  if (draft.link && draft.link.trim()) {
    lines.push("Music link: " + draft.link.trim());
  }
  lines.push("");
  lines.push(draft.body || "");
  return lines.join("\n");
}

function recipientContext(recipient) {
  const name = recipient.name || "unnamed";
  const role = recipient.role || "—";
  const station = recipient.station_name || recipient.organization || "—";
  const evidence = recipient.source_url || null;
  const email = String(recipient.email || "").trim();
  const submissionUrl = recipient.submission_url || null;

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
  ];

  if (email) {
    lines.push(el("div", { class: "oc-line" },
      el("span", { class: "oc-label" }, "Email"),
      el("span", { class: "email-readonly" },
        el("span", { class: "verified" }, email),
        el("span", { class: "verified-badge",
          title: "Exact address published by the station" },
          "✓ verified"),
        el("span", { class: "dim" }, "read-only"))));
  } else if (submissionUrl) {
    lines.push(el("div", { class: "oc-line" },
      el("span", { class: "oc-label" }, "Route"),
      el("span", { class: "email-readonly" },
        el("span", { class: "verified" }, submissionUrl),
        el("span", { class: "verified-badge",
          title: "Exact submission page published by the station" },
          "✓ verified submission page"))));
  }

  lines.push(el("div", { class: "oc-line" },
    el("span", { class: "oc-label" }, "Source"),
    evidence
      ? el("a", { href: evidence, target: "_blank",
        rel: "noopener noreferrer" }, evidence)
      : el("span", { class: "dim" }, "no source on record")));

  return el("section", { class: "oc-panel" },
    el("div", { class: "oc-panel-title" },
      el("span", {}, "Recipient"),
      el("span", { class: "dim" }, "read-only")),
    el("div", { class: "outreach-context" }, ...lines));
}

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

/* ---- release (music) selector, loaded from My music /api/v1/tracks ---- */

function loadReleaseOptions() {
  return api.tracks({ status: "ready", limit: 200 })
    .then((data) => (data.tracks || []).filter((t) => t && t.track_id))
    .catch(() => []);
}

function releaseSelector(preselectedId, currentId, onChange) {
  const select = el("select", { name: "track" });
  const index = new Map();
  const fixed = currentId ? [
    el("option", { value: currentId, selected: true },
      currentId),
  ] : [];
  const placeholder = fixed.length === 0
    ? el("option", { value: "" }, "no music attached")
    : el("option", { value: "" },
      "keep attached music");
  select.append(placeholder, el("option", { value: "", disabled: true }, "—"));
  const emit = () => {
    if (typeof onChange === "function") {
      onChange(index.get(select.value) || null);
    }
  };
  loadReleaseOptions().then((tracks) => {
    tracks.forEach((t) => index.set(t.track_id, t));
    const rest = tracks.map((t) =>
      el("option", {
        value: t.track_id,
        selected: t.track_id === preselectedId || t.track_id === currentId,
      }, t.original_filename || t.track_id));
    select.append(...rest);
    emit();
  }).catch(() => {});
  select.addEventListener("change", emit);
  return select;
}

/* ---- modal: compose (create a record) or record (hand off a record) ------- */

export function openOutreachModal(context) {
  const recipient = context.recipient || {};
  const stationName = context.stationName || recipient.station_name || "";
  const record = context.record || null;
  const email = String(recipient.email || "").trim();
  const isWebform = !email && Boolean(recipient.submission_url);
  const uid = String(recipient.contact_uid || "");

  const status = el("div", { class: "dim dialect-status" });

  const fromPref = senderPref();
  const savedMusicLink = record
    ? ((record.context && record.context.music_link)
      || (record.track && record.track.listen_url) || "")
    : "";
  let draftLink = "";
  let fromInput;
  let subjectInput;
  let bodyInput;
  let musicLinkInput;
  let musicSelect;

  if (record) {
    const recTrack = record.track && typeof record.track === "object"
      ? record.track : null;
    fromInput = el("input", {
      type: "text", placeholder: "your address (optional)",
      autocomplete: "off", spellcheck: "false",
      value: record.from_email || fromPref,
    });
    subjectInput = el("input", {
      type: "text", placeholder: "Subject", autocomplete: "off",
      value: record.subject || "",
    });
    bodyInput = el("textarea", {
      rows: "12",
      placeholder: "Write your message, then hand it to your email client",
      autocomplete: "off",
    });
    bodyInput.value = record.message || "";
    musicSelect = recTrack
      ? el("div", { class: "field" },
        el("span", {}, "Release"),
        el("span", {}, recTrack.original_filename || recTrack.track_id),
        el("span", { class: "dim hint" },
          "attached to this record"))
      : el("div", { class: "field" },
        el("span", {}, "Release"),
        el("span", { class: "dim" }, "no music attached"));
  } else {
    const draft = loadDraft(uid);
    draftLink = draft.link;
    fromInput = el("input", {
      type: "text", placeholder: "your address (optional)",
      autocomplete: "off", spellcheck: "false",
      value: draft.from || fromPref,
    });
    subjectInput = el("input", {
      type: "text", placeholder: "Subject", autocomplete: "off",
      value: draft.subject,
    });
    bodyInput = el("textarea", {
      rows: "12",
      placeholder: "Write your message, then hand it to your email client",
      autocomplete: "off",
    });
    bodyInput.value = draft.body;
    musicSelect = releaseSelector(
      context.preselectedTrackId || "", null, applyReleaseLink);
  }

  musicLinkInput = el("input", {
    type: "text",
    placeholder: "Music link (listen URL) — optional",
    autocomplete: "off", spellcheck: "false",
    value: savedMusicLink || draftLink,
  });

  /* Honest link state + a live preview of exactly what the email will carry.
   * Selecting a release auto-fills its SAVED listening URL; when the release
   * has none we say so rather than inventing a link, and a link the operator
   * typed by hand is never silently replaced by a later selection. */
  const linkHint = el("p", { class: "dim hint" });
  const linkPreview = el("p", { class: "dim music-link-preview" });
  let autoLinkValue = "";
  let linkEdited = Boolean(draftLink);

  function updateLinkPreview() {
    const url = musicLinkInput.value.trim();
    if (url) {
      linkPreview.replaceChildren(
        el("span", {}, "Email will end with: "),
        el("code", { class: "listen-inline" },
          `You can listen here: ${url}`));
    } else {
      linkPreview.replaceChildren(
        el("span", {}, "No listening link will be included."));
    }
  }

  function applyReleaseLink(track) {
    const saved = track ? String(track.listen_url || "").trim() : "";
    const current = musicLinkInput.value.trim();
    if (!track) {
      if (!linkEdited) {
        musicLinkInput.value = "";
        autoLinkValue = "";
      }
      linkHint.replaceChildren(
        "No release selected — a music link is optional.");
    } else if (saved) {
      if (linkEdited && current && current !== autoLinkValue) {
        // Never silently replace a link the operator typed by hand.
        linkHint.replaceChildren(
          "This release's saved link differs from the link you entered. ",
          "Clear the field to use the saved link.");
      } else {
        musicLinkInput.value = saved;
        autoLinkValue = saved;
        linkEdited = false;
        linkHint.replaceChildren(
          "Using the saved listening URL for this release.");
      }
    } else if (!linkEdited) {
      musicLinkInput.value = "";
      autoLinkValue = "";
      linkHint.replaceChildren(
        "No listening URL is saved for this release — add one in My music. ",
        "Nothing is invented here.");
    } else {
      linkHint.replaceChildren(
        "This release has no saved link; you'll send the link you entered.");
    }
    updateLinkPreview();
  }

  musicLinkInput.addEventListener("input", () => {
    linkEdited = true;
    linkHint.replaceChildren();
    updateLinkPreview();
  });
  updateLinkPreview();

  fromInput.addEventListener("input", () => setSenderPref(fromInput.value));

  let recordId = record && record.outreach_id ? record.outreach_id : null;

  const addGreeting = () => {
    const body = bodyInput.value;
    const trimmedLead = body.replace(/^\s+/, "");
    if (/^(hi|hello|dear)\b/i.test(trimmedLead)) {
      status.textContent = "A greeting is already at the top of your message.";
      bodyInput.focus();
      return;
    }
    bodyInput.value = suggestedOpening(recipient) + "\n\n" + trimmedLead;
    save();
    bodyInput.focus();
  };

  const save = () => {
    if (!uid) return;
    saveDraft(uid, { subject: subjectInput.value, body: bodyInput.value,
      from: fromInput.value, link: musicLinkInput.value });
    setSenderPref(fromInput.value);
    status.textContent = "Draft saved in this browser session.";
  };

  /* Appends an append-only ATTEMPT in the ledger; never changes the stored
   * status. Only a provider-confirmed send (or the operator's explicit
   * "I sent this email" confirmation below) earns 'sent'. */
  function recordHandoff(outreachId, link) {
    const meta = { channel: "mailto" };
    if (link) meta.music_link = link;
    return api.outreachEvent(outreachId, "opened_in_email", meta)
      .then((updated) => {
        status.textContent =
          `Handoff recorded (attempt: opened_in_email; record ${updated.status}).`;
      })
      .catch((error) => {
        status.textContent =
          `Handoff opened, but logging it failed: ${
            error && error.message ? error.message : String(error)}`;
      });
  }

  /* Optional Gmail send. Offered ONLY when an account is already connected:
   * preparing and handing off outreach never requires Google sign-in, so
   * there is deliberately no "connect/login" prompt here. */
  async function renderGmailState(container, outreachId) {
    container.hidden = true;
    container.replaceChildren();
    if (!email) return;
    let me = null;
    try {
      me = await api.me();
    } catch (error) {
      me = null;
    }
    if (!me || !me.gmail_connected) return;
    const gmailEmail = me.gmail_email ? me.gmail_email : "";

    const sendButton = el("button", { class: "primary" }, "Send via Gmail");
    const confirmButton = el("button", { class: "primary" }, "Confirm send");
    const cancelButton = el("button", { class: "subtle" }, "Cancel");
    const review = el("div", { class: "gmail-review", hidden: true },
      el("div", { class: "oc-line" },
        el("span", { class: "oc-label" }, "From"),
        el("span", { class: "verified" }, gmailEmail || "connected Gmail")),
      el("div", { class: "oc-line" },
        el("span", { class: "oc-label" }, "To"),
        el("span", {}, email)),
      el("div", { class: "oc-line" },
        el("span", { class: "oc-label" }, "Subject"),
        el("span", {}, subjectInput.value || "(none)")),
      el("div", { class: "dim hint" },
        "This sends the message above from your connected Gmail account ",
        "and marks the record sent. It cannot be undone here."),
      el("div", { class: "actions-row" }, confirmButton, cancelButton));

    sendButton.addEventListener("click", () => {
      review.hidden = false;
      sendButton.hidden = true;
    });
    cancelButton.addEventListener("click", () => {
      review.hidden = true;
      sendButton.hidden = false;
    });
    confirmButton.addEventListener("click", async () => {
      confirmButton.disabled = true;
      sendButton.disabled = true;
      status.textContent = "Sending via Gmail…";
      try {
        const res = await api.sendOutreach(outreachId);
        status.textContent =
          `Sent via Gmail (message ${(res && res.message_id) || "ok"}; ` +
          `record ${res && res.status}).`;
        container.replaceChildren(
          el("div", { class: "gmail-line" },
            el("span", { class: "verified" }, "✓ sent"),
            el("span", { class: "dim" },
              ` from ${gmailEmail || "your connected Gmail"}`)));
      } catch (error) {
        status.textContent = `Send failed: ${
          error && error.message ? error.message : String(error)}`;
        confirmButton.disabled = false;
        sendButton.disabled = false;
      }
    });

    container.hidden = false;
    container.replaceChildren(
      el("div", { class: "gmail-line" },
        el("span", { class: "dim" },
          `Sending as ${gmailEmail || "connected Gmail"}`),
        sendButton),
      review);
  }

  /* Build the create payload from what is on screen; nothing is invented. */
  function buildTrack() {
    const value = typeof musicSelect === "object" && musicSelect.value
      ? musicSelect.value : "";
    if (!value) return null;
    const t = { track_id: value };
    const opt = typeof musicSelect === "object"
      && musicSelect.selectedOptions && musicSelect.selectedOptions[0];
    if (opt) {
      const label = opt.textContent.trim();
      if (label && label !== value) t.original_filename = label;
    }
    const link = musicLinkInput.value.trim();
    if (link) t.listen_url = link;
    return t;
  }

  function buildPayload() {
    const link = musicLinkInput.value.trim();
    return {
      recipient: {
        contact_uid: String(recipient.contact_uid || ""),
        identity_key: String(recipient.identity_key || ""),
        target_type: String(recipient.target_type || "station"),
        name: recipient.name || null,
        role: recipient.role || null,
        organization: stationName || null,
        email,
        outreach_class: email ? "email" : "webform",
        submission_url: isWebform ? recipient.submission_url : null,
        source_url: recipient.source_url || null,
      },
      track: buildTrack(),
      context: link ? { music_link: link } : null,
      subject: subjectInput.value,
      message: bodyInput.value,
      from: fromInput.value,
    };
  }

  async function ensureRecord() {
    if (recordId) return recordId;
    const created = await api.createOutreach(buildPayload());
    recordId = created.outreach_id;
    status.textContent =
      `Record created (${created.outreach_id}, status ${created.status}).`;
    return recordId;
  }

  /* Enter record mode — idempotent and independent of DOM insertion order,
   * so it is safe to call BEFORE the overlay exists (existing-record open)
   * and after a fresh create. Inputs stay editable so the message can still
   * be edited before it is handed off. */
  function enterRecordMode(outreachId) {
    recordId = outreachId;
    createButton.hidden = true;
    if (handoffButton) handoffButton.hidden = false;
    if (submissionButton) submissionButton.hidden = false;
    sentConfirm.hidden = false;
    renderGmailState(gmailActions, outreachId);
  }

  const createButton = el("button", {}, "Create outreach record");
  createButton.addEventListener("click", async () => {
    if (createButton.disabled || recordId) return;
    createButton.disabled = true;
    createButton.textContent = "creating…";
    try {
      const created = await api.createOutreach(buildPayload());
      recordId = created.outreach_id;
      save();
      status.textContent =
        `Record created (${created.outreach_id}, status ${created.status}).`;
      enterRecordMode(created.outreach_id);
    } catch (error) {
      createButton.disabled = false;
      createButton.textContent = "Create outreach record";
      status.textContent = `Could not create the record: ${
        error && error.message ? error.message : String(error)}`;
    }
  });

  /* Never prepare an email for a release that has no saved music link: ask
   * the operator to add one in My music (or paste a link) instead of
   * inventing a link or sending without one. */
  function selectedReleaseId() {
    return (typeof musicSelect === "object" && musicSelect.value)
      ? musicSelect.value : "";
  }

  function askForLinkIfMissing() {
    if (selectedReleaseId() && !musicLinkInput.value.trim()) {
      linkHint.replaceChildren(
        "No listening URL is saved for this release — add one in My music. ",
        "Nothing is invented here.");
      status.textContent =
        "This release has no saved listening URL. Add one in My music, or " +
        "paste a link above, before handing off.";
      musicLinkInput.focus();
      return false;
    }
    return true;
  }

  /* Primary handoff: create the record if it does not exist yet, log the
   * opened_in_email attempt, then open the operator's own email client. No
   * Google account is involved anywhere in this path. */
  const handoffButton = email
    ? el("button", { class: "primary" }, "Open in My Email")
    : null;
  if (handoffButton) {
    handoffButton.addEventListener("click", async () => {
      if (handoffButton.disabled) return;
      if (!askForLinkIfMissing()) return;
      handoffButton.disabled = true;
      handoffButton.textContent = "opening…";
      try {
        const id = await ensureRecord();
        save();
        await recordHandoff(id, musicLinkInput.value.trim());
        openInMyEmail(email, subjectInput.value,
          withMusicLink(bodyInput.value, musicLinkInput.value));
        enterRecordMode(id);
        status.textContent =
          "Opened your email app with this message. Confirm below once you " +
          "have actually sent it.";
      } catch (error) {
        status.textContent = `Could not open your email app: ${
          error && error.message ? error.message : String(error)}`;
      } finally {
        handoffButton.disabled = false;
        handoffButton.textContent = "Open in My Email";
      }
    });
  }

  /* Explicit send confirmation — the ONLY way a mailto handoff becomes
   * 'sent'. The app never assumes a mail client delivered anything. */
  const sentConfirm = el("button", { class: "primary", hidden: true },
    "I sent this email");
  sentConfirm.addEventListener("click", async () => {
    if (sentConfirm.disabled || !recordId) return;
    sentConfirm.disabled = true;
    sentConfirm.textContent = "recording…";
    try {
      const updated = await api.outreachEvent(recordId, "sent",
        { applied_by: "operator", channel: "mailto" });
      status.textContent =
        `Marked sent (record ${updated.status}). Attempt recorded.`;
      sentConfirm.hidden = true;
    } catch (error) {
      sentConfirm.disabled = false;
      sentConfirm.textContent = "I sent this email";
      status.textContent = `Could not record it: ${
        error && error.message ? error.message : String(error)}`;
    }
  });

  /* Optional in-app Gmail send; hidden unless an account is connected. */
  const gmailActions = el("div", { class: "gmail-actions", hidden: true });

  const submissionButton = !email && isWebform
    ? el("a", {
      class: "linkish",
      href: recipient.submission_url || "#",
      target: "_blank",
      rel: "noopener noreferrer",
    }, "Open submission page")
    : null;

  const copyEmailButton = email
    ? el("button", {}, "Copy Email")
    : null;
  if (copyEmailButton) {
    copyEmailButton.addEventListener("click", () =>
      copyText(copyEmailText(recipient), status, "Recipient email copied."));
  }
  const copyMessageButton = el("button", {}, "Copy Message");
  copyMessageButton.addEventListener("click", () => {
    const text = copyMessageText(recipient, {
      from: fromInput.value, subject: subjectInput.value,
      body: bodyInput.value, link: musicLinkInput.value,
    });
    copyText(text, status, "Full message copied.");
  });

  const closeButton = el("button", { class: "linkish" }, "Close");
  closeButton.addEventListener("click", () => {
    save();
    closeOverlay(backdrop);
  });

  const modeTitle = record
    ? "Outreach record"
    : "Prepare outreach record";
  const modeSub = record
    ? "Edit freely, then open in your own email app. The record marks sent "
      + "only when you confirm you sent it."
    : "Pick a release (its saved listening link fills in automatically), "
      + "compose, then open it in your email app or create the record.";

  const backdrop = openOverlay(
    el("header", { class: "outreach-modal-header" },
      el("h2", {}, modeTitle),
      el("p", { class: "dim" }, modeSub)),
    recipientContext(recipient, stationName),
    el("section", { class: "oc-panel oc-draft" },
      el("div", { class: "oc-panel-title" },
        el("span", {}, record ? "Your message" : "Compose"),
        el("span", { class: "dim" }, "editable until you hand off")),
      el("label", { class: "field" },
        el("span", {}, "Release"),
        musicSelect,
        el("span", { class: "dim hint" },
          "Attached to this record; chosen from My music.")),
      el("label", { class: "field" },
        el("span", {}, "Music link (from the selected release)"),
        musicLinkInput,
        el("span", { class: "dim hint" },
          "Auto-filled from the selected release's saved URL; editable. ",
          "Left blank it is simply omitted.")),
      linkHint,
      linkPreview,
      el("button", { class: "oc-greeting-btn", onClick: addGreeting },
        "Add personalized greeting"),
      el("label", { class: "field" },
        el("span", {}, "From (your email)"),
        fromInput,
        el("span", { class: "dim hint" },
          "Optional. Shown in the copied text; the mailto handoff sends ",
          "from your own email app.")),
      el("label", { class: "field" }, el("span", {}, "Subject"), subjectInput),
      el("label", { class: "field" }, el("span", {}, "Message"), bodyInput)),
    el("div", { class: "actions-row outreach-actions" },
      handoffButton,
      createButton,
      submissionButton,
      sentConfirm,
      gmailActions,
      copyEmailButton,
      copyMessageButton,
      el("button", { class: "subtle", onClick: save }, "Save draft"),
      closeButton),
    status);

  /* Opening an EXISTING record: prefill it (still editable) and offer the
   * handoff / send actions — never a second "Create" competing action. */
  if (recordId) {
    enterRecordMode(recordId);
  }

  return backdrop;
}
