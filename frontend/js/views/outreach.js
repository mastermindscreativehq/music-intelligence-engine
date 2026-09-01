/* Outreach composer — the action-first pitch workflow (Parts 2-7, 9).
 *
 * Order (per spec):
 *   START OUTREACH   -> the verified recipient(s)
 *   TRACK TO PITCH   -> one existing uploaded asset (status=ready) chosen by
 *                       reference; NO re-upload. Persists a track asset id.
 *   OUTREACH CONTENT -> subject + message, editable; Generate Draft /
 *                       Regenerate builds from verified context only.
 *   DELIVERY METHOD  -> Open in email / Copy message (direct send is later).
 *   ACTIONS          -> Open in Email, Copy Message, Save Draft.
 *
 * Nothing here transmits email by itself. "Open in email" hands a mailto:
 * to the operator's own mail client — it can attach nothing, so attachments
 * are never implied. Saving a draft records it (status=draft) via the backend
 * outreach records; opening in email records opened_in_email. A record is
 * NEVER marked sent merely because the client opened.
 */

import { api, ApiError } from "../api.js";
import { el } from "../dom.js";
import { stationHref, outreachHistoryHref } from "../router.js";
import { generateDraft } from "../draftGenerator.js";

const DRAFT_KEY = "mie.outreach.pitch.v1";

function recallDraft() {
  try {
    const raw = JSON.parse(window.sessionStorage.getItem(DRAFT_KEY) || "null");
    return raw && typeof raw === "object" ? raw : {};
  } catch (error) {
    return {};
  }
}

function storeDraft(patch) {
  const next = { ...recallDraft(), ...patch };
  for (const key of Object.keys(next)) {
    if (next[key] === undefined || next[key] === null) delete next[key];
  }
  try {
    window.sessionStorage.setItem(DRAFT_KEY, JSON.stringify(next));
  } catch (error) {
    /* keep in-memory */
  }
  return next;
}

function errorBanner(message) {
  return el("div", { class: "banner-error", role: "alert" },
    el("strong", {}, message));
}

function statusChip(status) {
  const known = ["ready", "quarantined", "archived"].includes(status);
  return el("span", { class: `chip${known ? ` ${status}` : ""}` },
    String(status ?? "unknown"));
}

export function renderOutreachView(root, uids, basket) {
  const recipients = uids.map((uid) => basket.get(uid)).filter(Boolean);

  if (recipients.length === 0) {
    root.append(
      errorBanner("No recipients selected for outreach yet."),
      el("section", { class: "card" },
        el("p", { class: "dim" },
          "Select a verified recipient on a station page (Reach Out), then ",
          "come back here to pitch."),
        el("div", { class: "actions-row" },
          el("a", { class: "primary", href: "#/" }, "Find stations"))));
    return;
  }

  const persisted = recallDraft();

  // --- recipient blocks ----------------------------------------------------
  const deselect = (uid) => {
    const remaining = recipients.filter((r) => String(r.contact_uid) !== uid);
    if (remaining.length === 0) {
      window.location.hash = stationHref(recipients[0].identity_key);
      return;
    }
    window.location.hash = "#/outreach?recipient=" +
      remaining.map((r) => encodeURIComponent(r.contact_uid)).join(",");
  };
  const recipientCards = recipients.map((item) => {
    const email = (item.email && item.email.trim()) || null;
    return el("div", { class: "recipient-evidence" },
      el("div", { class: "recipient-tags" },
        el("span", { class: "chip recipient-tag" },
          [item.name || "unnamed", item.station_name].filter(Boolean)
            .join(" · ")),
        el("button", {
          class: "linkish tag-remove",
          onClick: () => deselect(String(item.contact_uid)),
        }, "Remove recipient")),
      el("div", { class: "dim context-line" },
        item.role ? `${item.role} · ` : "",
        email ? `Verified email · ${email}` : "no verified email"));
  });

  // --- track-to-pitch state ---------------------------------------------------
  let trackRecord = null;        // the selected ready asset (detail projection)
  let trackList = [];            // ready assets available to pick
  let trackErr = null;

  // context fields (Part 3) — operator-supplied, never invented
  const artistCtx = {
    name: el("input", { type: "text", placeholder: "Artist name", autocomplete: "off" }),
    bio: el("textarea", { rows: "2", placeholder: "Short artist bio (optional)", autocomplete: "off" }),
    genre: el("input", { type: "text", placeholder: "Genre (optional)", autocomplete: "off" }),
    location: el("input", { type: "text", placeholder: "Location (optional)", autocomplete: "off" }),
    socials: el("input", { type: "text", placeholder: "Social links, comma-separated (optional)", autocomplete: "off" }),
    epk: el("input", { type: "text", placeholder: "Press/EPK link (optional)", autocomplete: "off" }),
  };
  const trackCtx = {
    title: el("input", { type: "text", placeholder: "Track title (optional)", autocomplete: "off" }),
    genre: el("input", { type: "text", placeholder: "Genre (optional)", autocomplete: "off" }),
    release_date: el("input", { type: "text", placeholder: "Release date (optional)", autocomplete: "off" }),
    description: el("textarea", { rows: "2", placeholder: "Short description (optional)", autocomplete: "off" }),
    tags: el("input", { type: "text", placeholder: "Mood/style tags (optional)", autocomplete: "off" }),
    listen_url: el("input", { type: "text", placeholder: "Private/streaming listening URL (optional)", autocomplete: "off" }),
  };

  // subject/message editable fields
  const subjectInput = el("input", { type: "text", placeholder: "Subject", autocomplete: "off", value: persisted.subject || "" });
  const messageInput = el("textarea", { rows: "14", placeholder: "Message", autocomplete: "off" });
  messageInput.value = persisted.message || "";

  // sharing mode (Part 5): private link default
  const sharingMode = el("select", { name: "sharing" },
    el("option", { value: "private_link", selected: true }, "Private track link (recommended)"),
    el("option", { value: "download" }, "Download link"),
    el("option", { value: "attachment" }, "Email attachment"));
  sharingMode.value = persisted.sharingMode || "private_link";

  const statusLine = el("div", { class: "dim draft-status" },
    "Select a ready track, then Generate a draft.");

  // --- load ready tracks --------------------------------------------------------
  function determineListenUrl() {
    const manual = trackCtx.listen_url.value.trim();
    if (manual) return manual;
    return null;
  }

  function sharingUrl() {
    if (sharingMode.value === "private_link") return determineListenUrl();
    return null;
  }

  function generate() {
    const recipient = recipients[0] || {};
    const artist = {
      name: artistCtx.name.value.trim() || undefined,
      bio: artistCtx.bio.value.trim() || undefined,
      genre: artistCtx.genre.value.trim() || undefined,
      location: artistCtx.location.value.trim() || undefined,
      socials: artistCtx.socials.value.trim() || undefined,
      epk: artistCtx.epk.value.trim() || undefined,
    };
    const trackInfo = {
      title: clean(trackCtx.title.value) || clean(trackRecord && trackRecord.original_filename),
      genre: clean(trackCtx.genre.value),
      release_date: clean(trackCtx.release_date.value),
      description: clean(trackCtx.description.value),
      tags: clean(trackCtx.tags.value),
      listen_url: sharingUrl(),
    };
    const draft = generateDraft(recipient, trackInfo, artist);
    subjectInput.value = draft.subject;
    messageInput.value = draft.message;
    statusLine.textContent = "Draft generated from verified context. Edit freely.";

    // persist context + selection shape
    storeDraft({
      subject: draft.subject,
      message: draft.message,
      track_id: trackRecord ? trackRecord.track_id : undefined,
      track: trackInfo,
      artist,
      sharingMode: sharingMode.value,
    });
  }

  function clean(value) {
    return typeof value === "string" ? value.trim() : "";
  }

  // --- Change Track picker ------------------------------------------------------
  const trackPickerList = el("div", { class: "track-picker-list" });
  function openTrackPicker() {
    trackPickerList.replaceChildren(el("p", { class: "dim" }, "Loading ready tracks…"));
    api.tracks({ status: "ready", limit: 100 })
      .then((data) => {
        trackList = data.tracks || [];
        if (trackList.length === 0) {
          trackPickerList.replaceChildren(el("p", { class: "dim" },
            "No ready tracks uploaded yet. Upload one on the Tracks page."));
          return;
        }
        trackPickerList.replaceChildren(
          trackList.map((t) => {
            const choose = el("button", { class: "subtle" }, "Select");
            choose.addEventListener("click", () => {
              trackRecord = t;
              renderTrackSummary();
              trackPickerList.replaceChildren();
            });
            return el("div", { class: "track-picker-row" },
              el("span", {}, t.original_filename || "(unnamed track)"),
              el("span", { class: "dim" },
                `${t.size_bytes ?? "?"} bytes · ready`),
              choose);
          }));
      })
      .catch((error) => {
        trackErr = error instanceof ApiError ? error.message : String(error);
        trackPickerList.replaceChildren(errorBanner(
          `Could not load ready tracks: ${trackErr}`));
      });
  }

  const trackCard = el("section", { class: "card" });
  function renderTrackSummary() {
    if (!trackRecord) {
      trackCard.replaceChildren(
        el("h2", {}, "Track to pitch"),
        el("p", { class: "dim" },
          "Choose one of your already-uploaded tracks — no re-upload."),
        el("div", { class: "actions-row" },
          el("button", { class: "primary", onClick: openTrackPicker },
            "Select a track")),
        trackPickerList);
      return;
    }
    const t = trackRecord;
    const focalName = clean(trackCtx.title.value) || t.original_filename || "(untitled)";
    trackCard.replaceChildren(
      el("h2", {}, "Track to pitch"),
      el("div", { class: "selected-track" },
        el("div", { class: "selected-track-title" },
          el("strong", {}, focalName),
          " · ",
          statusChip(t.status),
          t.size_bytes ? el("span", { class: "dim" },
            ` · ${t.size_bytes} bytes`) : null),
        el("div", { class: "dim select-track-meta" },
          `asset ${t.track_id}`),
        el("div", { class: "actions-row" },
          el("button", { class: "subtle", onClick: openTrackPicker },
            "Change Track"))),
      trackPickerList);
  }

  // --- outreach content / delivery / actions -------------------------------------
  const emailButton = el("button", { class: "primary", onClick: doOpenEmail }, "Open in Email");
  const copyButton = el("button", { onClick: doCopy }, "Copy Message");
  const saveButton = el("button", { class: "subtle", onClick: doSave }, "Save Draft");
  const generateButton = el("button", { class: "primary", onClick: generate }, "Generate Draft");

  async function doOpenEmail() {
    const to = recipients.map((r) => r.email).filter(Boolean);
    if (to.length === 0) {
      statusLine.textContent = "No verified email on the recipient — draft it yourself.";
      return;
    }
    if (!messageInput.value.trim()) {
      statusLine.textContent = "Nothing to send — generate or write a message first.";
      return;
    }
    const params = new URLSearchParams({
      subject: subjectInput.value,
      body: messageInput.value,
    });
    window.location.href = `mailto:${to.join(",")}?${decodeURIComponent(params.toString())}`;
    // Record the handoff (status: opened_in_email) — NOT sent.
    for (const r of recipients) {
      const id = await saveRecord(r, "opened_in_email");
      if (id) statusLine.textContent =
        "Opened in your mail client. This is recorded as 'opened in email' — not sent.";
    }
  }

  async function saveRecord(recipient, event) {
    try {
      let record = {
        recipient: {
          contact_uid: String(recipient.contact_uid || ""),
          identity_key: recipient.identity_key,
          name: recipient.name,
          role: recipient.role,
          organization: recipient.station_name,
          email: recipient.email,
          source_url: recipient.source_url,
        },
        track: trackRecord
          ? { track_id: trackRecord.track_id,
              original_filename: trackRecord.original_filename,
              status: trackRecord.status,
              size_bytes: trackRecord.size_bytes }
          : null,
        context: buildContext(),
        subject: subjectInput.value,
        message: messageInput.value,
        sharing: { mode: sharingMode.value, url: sharingUrl() },
      };
      const created = await api.createOutreach(record);
      if (event && event !== "draft") {
        await api.outreachEvent(created.outreach_id, event,
          { channel: "mailto" });
      }
      return created.outreach_id;
    } catch (error) {
      statusLine.textContent = "Could not save outreach: " +
        (error instanceof ApiError ? error.message : String(error));
      return null;
    }
  }

  function buildContext() {
    return {
      artist: {
        name: clean(artistCtx.name.value) || undefined,
        bio: clean(artistCtx.bio.value) || undefined,
        genre: clean(artistCtx.genre.value) || undefined,
        location: clean(artistCtx.location.value) || undefined,
        socials: clean(artistCtx.socials.value) || undefined,
        epk: clean(artistCtx.epk.value) || undefined,
      },
      track: {
        title: clean(trackCtx.title.value) || undefined,
        genre: clean(trackCtx.genre.value) || undefined,
        release_date: clean(trackCtx.release_date.value) || undefined,
        description: clean(trackCtx.description.value) || undefined,
        tags: clean(trackCtx.tags.value) || undefined,
        listen_url: clean(trackCtx.listen_url.value) || undefined,
      },
    };
  }

  async function doSave() {
    let saved = 0;
    for (const r of recipients) {
      const id = await saveRecord(r, null);
      if (id) saved += 1;
    }
    if (saved > 0) {
      storeDraft({ subject: subjectInput.value, message: messageInput.value });
      statusLine.textContent =
        `Draft saved (${saved} record(s)). See Outreach History.`;
    }
  }

  async function doCopy() {
    if (!messageInput.value.trim()) {
      statusLine.textContent = "Nothing to copy — generate or write a message first.";
      return;
    }
    const head = ["To: " + recipients.map((r) => r.email).filter(Boolean).join(", "),
      "Subject: " + subjectInput.value];
    const text = [head.join("\n"), messageInput.value.trim()].join("\n\n");
    try {
      await navigator.clipboard.writeText(text + "\n");
      statusLine.textContent = "Message copied to clipboard.";
    } catch (error) {
      statusLine.textContent = "Could not copy automatically; use Open in Email.";
    }
  }

  // restore any selected track from persisted draft
  if (persisted.track_id) {
    api.tracks({ status: "ready", limit: 100 }).then((data) => {
      const found = (data.tracks || []).find((t) => t.track_id === persisted.track_id);
      if (found) { trackRecord = found; renderTrackSummary(); }
      else renderTrackSummary();
    }).catch(() => renderTrackSummary());
    // prefill context from persistent draft
    if (persisted.track) {
      trackCtx.title.value = clean(persisted.track.title) || "";
      trackCtx.genre.value = clean(persisted.track.genre) || "";
      trackCtx.release_date.value = clean(persisted.track.release_date) || "";
      trackCtx.description.value = clean(persisted.track.description) || "";
      trackCtx.tags.value = clean(persisted.track.tags) || "";
      trackCtx.listen_url.value = clean(persisted.track.listen_url) || "";
    }
    if (persisted.artist) {
      artistCtx.name.value = clean(persisted.artist.name) || "";
      artistCtx.bio.value = clean(persisted.artist.bio) || "";
      artistCtx.genre.value = clean(persisted.artist.genre) || "";
      artistCtx.location.value = clean(persisted.artist.location) || "";
      artistCtx.socials.value = clean(persisted.artist.socials) || "";
      artistCtx.epk.value = clean(persisted.artist.epk) || "";
    }
  } else {
    renderTrackSummary();
  }

  root.append(
    el("section", { class: "card" },
      el("h2", {}, "Start outreach"),
      el("p", { class: "dim" },
        "A verified recipient, a ready track, a personalized message — ",
        "then hand off to your own email."),
      recipientCards),

    trackCard,

    el("section", { class: "card" },
      el("h2", {}, "Outreach content"),
      el("p", { class: "dim" },
        "Generated from verified context — the draft never invents a ",
        "relationship or a fact. Edit freely."),

      el("section", { class: "context-block" },
        el("h3", {}, "Artist"),
        el("div", { class: "context-grid" },
          field("Artist name", artistCtx.name),
          field("Genre", artistCtx.genre),
          field("Location", artistCtx.location),
          field("Social links", artistCtx.socials),
          field("Press/EPK", artistCtx.epk)),
        field("Short artist bio", artistCtx.bio)),

      el("section", { class: "context-block" },
        el("h3", {}, "Track"),
        el("div", { class: "context-grid" },
          field("Track title", trackCtx.title),
          field("Genre", trackCtx.genre),
          field("Release date", trackCtx.release_date),
          field("Mood/style tags", trackCtx.tags)),
        field("Short description", trackCtx.description),
        field("Private/streaming listen URL", trackCtx.listen_url)),

      el("h3", {}, "How to include your music"),
      el("p", { class: "dim" },
        "For now, only secure track links are supported. An email client ",
        "cannot attach your local MP3, so attachments are never implied."),
      el("label", { class: "field" }, el("span", {}, "Include as"),
        sharingMode),

      el("label", { class: "field" }, el("span", {}, "Subject"), subjectInput),
      el("label", { class: "field" }, el("span", {}, "Message"), messageInput),
      el("div", { class: "actions-row" },
        generateButton, el("button", { class: "subtle", onClick: generate },
          "Regenerate"))),

    el("section", { class: "card" },
      el("h2", {}, "Delivery method"),
      el("p", { class: "dim" },
        "Open the message in your own mail client, or copy it. Direct ",
        "in-app sending is a later phase.")),

    el("section", { class: "card" },
      el("h2", {}, "Actions"),
      el("div", { class: "actions-row" },
        emailButton, copyButton, saveButton),
      el("div", { class: "actions-row" },
        el("a", { href: outreachHistoryHref() },
          "View outreach history →")),
      statusLine));

  function field(labelText, inputEl) {
    return el("label", { class: "field" }, el("span", {}, labelText), inputEl);
  }
}
