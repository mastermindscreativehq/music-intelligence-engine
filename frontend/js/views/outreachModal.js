/* Outreach composer modal — the single composer for the whole app.
 *
 * One canonical action, two shapes — both funnel into a real outreach
 * RECORD that lives in the ledger (no duplicate composers, no parallel
 * campaign system):
 *
 *  - compose mode (from the station OUTREACH section and the outreach
 *    page for a fresh handoff): pick a release from My music, compose a
 *    subject + message, then CREATE the outreach record (POST /outreach).
 *    For an email recipient the record then hands off via mailto and the
 *    handoff is recorded as an 'opened_in_email' ATTEMPT (never as a
 *    stored status).
 *  - record mode (existing record from the outreach page): prefilled from
 *    the record; "Open in My Email" appends the handoff attempt; webform
 *    records open the verified submission page instead of a composer.
 *
 * Recipient & email come ONLY from verified backend evidence. Handoff is
 * mailto / copy / opening the real submission page — this application never
 * sends anything. Nothing here constructs or infers an address.
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
  if (draft.subject && draft.subject.trim()) lines.push("Subject: " + draft.subject.trim());
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

function releaseSelector(preselectedId, currentId) {
  const select = el("select", { name: "track" });
  const fixed = currentId ? [
    el("option", { value: currentId, selected: true },
      currentId),
  ] : [];
  const placeholder = fixed.length === 0
    ? el("option", { value: "" }, "no music attached")
    : el("option", { value: "" },
      "keep attached music");
  select.append(placeholder, el("option", { value: "", disabled: true }, "—"));
  loadReleaseOptions().then((tracks) => {
    const rest = tracks.map((t) =>
      el("option", {
        value: t.track_id,
        selected: t.track_id === preselectedId || t.track_id === currentId,
      }, t.original_filename || t.track_id));
    select.append(...rest);
  });
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
  let fromInput;
  let subjectInput;
  let bodyInput;
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
    musicSelect = releaseSelector(context.preselectedTrackId || "", null);
  }

  subjectInput.addEventListener("input",
    () => setSenderPref(fromInput.value));
  bodyInput.addEventListener("input",
    () => setSenderPref(fromInput.value));
  fromInput.addEventListener("input",
    () => setSenderPref(fromInput.value));

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
      from: fromInput.value });
    setSenderPref(fromInput.value);
    status.textContent = "Draft saved in this browser session.";
  };

  /* primary hand-off action */
  let primary;

  /* Appends an append-only ATTEMPT in the ledger; never changes the stored
   * status. Only a provider-confirmed send earns 'sent'. */
  function recordHandoff(outreachId) {
    return api.outreachEvent(outreachId, "opened_in_email",
      { channel: "mailto" })
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

  /* The actions row may or may not be in the DOM yet: if it is, swap the
   * node in place; otherwise the row picks up the freshly assigned var. */
  function takePrimary(next) {
    if (primary.parentNode) primary.replaceWith(next);
    else primary = next;
  }

  function switchToRecordMode(outreachId) {
    subjectInput.disabled = true;
    bodyInput.disabled = true;
    fromInput.disabled = true;
    if (email) {
      const mail = el("button", { class: "primary" }, "Open in My Email");
      mail.addEventListener("click", () => {
        openInMyEmail(email, subjectInput.value, bodyInput.value);
        save();
        recordHandoff(outreachId);
      });
      takePrimary(mail);
    } else if (isWebform) {
      const open = el("a", {
        class: "primary",
        href: recipient.submission_url,
        target: "_blank",
        rel: "noopener noreferrer",
      }, "Open submission page →");
      takePrimary(open);
    }
  }

  const createButton = el("button", { class: "primary" },
    isWebform ? "Create outreach record" : "Create outreach record");
  createButton.addEventListener("click", async () => {
    if (createButton.disabled) return;
    createButton.disabled = true;
    createButton.textContent = "creating…";
    const trackValue = typeof musicSelect === "object" && musicSelect.value
      ? musicSelect.value : "";
    try {
      const created = await api.createOutreach({
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
        track: (() => {
          if (!trackValue) return null;
          const t = { track_id: trackValue };
          const opt = typeof musicSelect === "object"
            && musicSelect.selectedOptions && musicSelect.selectedOptions[0];
          if (opt) {
            const label = opt.textContent.trim();
            if (label && label !== trackValue) t.original_filename = label;
          }
          return t;
        })(),
        subject: subjectInput.value,
        message: bodyInput.value,
        from: fromInput.value,
      });
      save();
      status.textContent =
        `Record created (${created.outreach_id}, status ${created.status}).`;
      switchToRecordMode(created.outreach_id);
    } catch (error) {
      createButton.disabled = false;
      createButton.textContent = "Create outreach record";
      status.textContent = `Could not create the record: ${
        error && error.message ? error.message : String(error)}`;
    }
  });

  primary = createButton;

  /* Opening an EXISTING record: present it read-only and hand it off (or
   * open its submission page) — never a second "Create" competing action. */
  if (record && record.outreach_id) {
    switchToRecordMode(record.outreach_id);
  }

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
      body: bodyInput.value,
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
    ? "Compose and hand this record to your email client; nothing sends here."
    : "Pick a release, compose, then create a record for this recipient.";

  const backdrop = openOverlay(
    el("header", { class: "outreach-modal-header" },
      el("h2", {}, modeTitle),
      el("p", { class: "dim" }, modeSub)),
    recipientContext(recipient, stationName),
    el("section", { class: "oc-panel oc-draft" },
      el("div", { class: "oc-panel-title" },
        el("span", {}, record ? "Your message" : "Compose"),
        el("span", { class: "dim" },
          record ? "edits stay in this browser" : "edit freely before creating")),
      el("label", { class: "field" },
        el("span", {}, "Release"),
        musicSelect,
        el("span", { class: "dim hint" },
          "Attached to this record; chosen from My music.")),
      el("button", { class: "oc-greeting-btn", onClick: addGreeting },
        "Add personalized greeting"),
      el("label", { class: "field" },
        el("span", {}, "From (your email)"),
        fromInput,
        el("span", { class: "dim hint" },
          "Your own address, saved only in this browser — not verified by ",
          "this console.")),
      el("label", { class: "field" }, el("span", {}, "Subject"), subjectInput),
      el("label", { class: "field" }, el("span", {}, "Message"), bodyInput)),
    el("div", { class: "actions-row outreach-actions" },
      primary,
      copyEmailButton,
      copyMessageButton,
      el("button", { class: "subtle", onClick: save }, "Save draft"),
      closeButton),
    status);

  return backdrop;
}