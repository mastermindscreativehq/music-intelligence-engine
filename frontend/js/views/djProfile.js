/* DJ profile page (#/dj/<id>) — the DJ outreach intake (Phase 4).
 *
 * Canonical structure:
 *   1. DJ PROFILE          identity (name/stage, role, program, station)
 *   2. CHANNELS & ROUTES   source-backed contact / submission / social facts
 *   3. SUBMISSION          official submission routes, when recorded
 *   4. OUTREACH            staged DJ recipient + release selector +
 *                          "Start outreach" (creates one real outreach
 *                          RECORD, target_type 'dj') — reused from the
 *                          existing single outreach system
 *   5. OUTREACH HISTORY    records already created for this DJ
 *   6. Remove DJ           deletes the profile (records are preserved)
 *
 * Everything rendered comes verbatim from the backend; this view adds
 * presentation only. A DJ channel is shown ONLY when it was recorded with
 * provenance (source_url) — nothing is ever fabricated. */
 
import { api, ApiError } from "../api.js";
import { chips, el } from "../dom.js";
import { djsHref, outreachHref } from "../router.js";
import { stationLocation } from "./stationLocation.js";
import { djStatusChip } from "./status.js";

function errorBanner(error) {
  const detail = error instanceof ApiError
    ? `${error.code}: ${error.message}`
    : String(error);
  return el("div", { class: "banner-error", role: "alert" },
    "Could not load this DJ. ", el("strong", {}, detail));
}

function externalLink(url, text, cls) {
  return el("a", {
    class: cls || null,
    href: url,
    target: "_blank",
    rel: "noopener noreferrer",
  }, text ?? url);
}

function isHttp(value) {
  return typeof value === "string" && /^https?:/.test(value)
    && !/[\s]/.test(value);
}

function djEmail(detail) {
  return (detail.channels || []).find((c) =>
    (c.channel === "email" || c.channel === "submission_email")
    && typeof c.value === "string" && c.value.trim() !== "") || null;
}

function djWebform(detail) {
  return (detail.submission_routes || []).find((c) =>
    isHttp(c.value)) || null;
}

function djOrganization(detail) {
  return detail.station_name || detail.platform || detail.name || "DJ";
}

/* ---------------------------------------------------------------------------
 * Section 1 — DJ PROFILE
 * ------------------------------------------------------------------------- */

function overviewSection(detail) {
  const location = stationLocation(detail) || null;
  const subtitle = [detail.role, detail.program]
    .filter((value) => value)
    .join(" · ") || "DJ";
  const stationLine = detail.station_name || detail.platform || null;
  return el("section", { class: "card detail-head", id: "dj-profile" },
    el("p", { class: "dim section-label" }, "DJ profile"),
    el("h1", {}, detail.stage_name || detail.name || "(unnamed DJ)"),
    el("div", { class: "overview-row" },
      el("span", {}, subtitle),
      stationLine ? el("span", { class: "dim" }, " · " + stationLine) : null,
      location ? el("span", { class: "dim" }, " · " + location) : null),
    el("div", { class: "overview-row" },
      chips(detail.genres), " ", chips(detail.formats),
      " ", djStatusChip(detail)));
}

/* ---------------------------------------------------------------------------
 * Section 2 — CHANNELS & ROUTES (source-backed facts only)
 * ------------------------------------------------------------------------- */

function channelRow(channel) {
  const value = channel.value;
  return el("article", { class: "dj-channel" },
    el("div", { class: "head" },
      el("span", { class: "name" }, channel.channel.replace(/_/g, " ")),
      el("span", { class: "chip", title: channel.route_label },
        channel.route_class === "direct" ? "direct"
          : channel.route_class === "social" ? "social"
            : channel.route_class === "website" ? "website"
              : "submission")),
    el("div", { class: "route-status-line" },
      isHttp(value) || /^mailto:/.test(value)
        ? externalLink(value, value)
        : el("span", {}, value),
      el("span", { class: "dim" }, " · " + channel.route_label)),
    el("div", { class: "dim evidence-row" }, "Found on: ",
      externalLink(channel.source_url)));
}

function channelsSection(detail) {
  const channels = detail.channels || [];
  return el("section", { class: "card", id: "dj-channels" },
    el("p", { class: "dim section-label" }, "Channels & routes"),
    el("h2", {}, "How to reach this DJ"),
    el("p", { class: "dim" },
      "Source-backed contact, submission, and social facts recorded for ",
      "this DJ. A value is shown only when it was recorded with its source."),
    channels.length
      ? el("div", { class: "dj-channels-box" }, channels.map(channelRow))
      : el("p", { class: "dim" },
        "No source-backed channels recorded for this DJ yet."));
}

/* ---------------------------------------------------------------------------
 * Section 3 — SUBMISSION (official routes when recorded)
 * ------------------------------------------------------------------------- */

function submissionSection(detail) {
  const routes = detail.submission_routes || [];
  return el("section", { class: "card", id: "dj-submission" },
    el("p", { class: "dim section-label" }, "Submission"),
    el("h2", {}, "Send music to this DJ"),
    el("p", { class: "dim" },
      "The submission contacts and pages this DJ's profile actually ",
      "records. Use these when you prepare outreach below."),
    routes.length
      ? el("div", { class: "dj-channels-box" }, routes.map(channelRow))
      : el("p", { class: "dim" },
        detail.has_email
          ? "This DJ has a recorded email; use the outreach section below."
          : "No public submission route recorded for this DJ yet."));
}

/* ---------------------------------------------------------------------------
 * Section 4 — OUTREACH: one staged DJ recipient + release selector + Start
 * ------------------------------------------------------------------------- */

function releaseSelector() {
  const select = el("select", { name: "release", id: "dj-release" });
  select.append(el("option", { value: "" }, "no music attached"));
  api.tracks({ status: "ready", limit: 200 })
    .then((data) => {
      const list = data.tracks || [];
      if (list.length === 0) {
        select.append(el("option", { value: "", disabled: true },
          "(My music is empty — upload an MP3 first)"));
        return;
      }
      for (const track of list) {
        const option = el("option", { value: track.track_id },
          track.original_filename || track.track_id);
        select.append(option);
      }
    })
    .catch(() => { /* selector stays with the "no music attached" default */ });
  return select;
}

function stageRecipient(detail, basket) {
  const uid = "dj:" + detail.dj_id;
  const email = djEmail(detail);
  const webform = djWebform(detail);
  const sourceUrl = email ? email.source_url
    : (webform ? webform.source_url : (detail.source_urls || [])[0] || null);
  return basket.add({
    contact_uid: uid,
    identity_key: uid,
    target_type: "dj",
    station_name: djOrganization(detail),
    name: detail.stage_name || detail.name || "DJ",
    role: detail.role || null,
    email: email ? email.value : "",
    source_url: sourceUrl || null,
    outreach_class: email ? "email" : "webform",
    submission_url: webform ? webform.value : null,
    route_kind: email ? "email" : "webform",
    route_label: email ? "direct DJ contact" : "submission route",
  });
}

async function startOutreach(detail, basket, ui, status) {
  const uid = "dj:" + detail.dj_id;
  const staged = basket.items.filter((item) => item.identity_key === uid);
  if (staged.length === 0) {
    status.textContent = "Add this DJ to outreach first.";
    return;
  }
  ui.disabled = true;
  ui.textContent = "preparing record…";
  const release = document.getElementById("dj-release");
  const trackId = release ? release.value : "";
  const track = trackId ? { track_id: trackId } : null;
  if (track && release.selectedOptions && release.selectedOptions[0]) {
    const label = release.selectedOptions[0].textContent.trim();
    if (label && label !== trackId) track.original_filename = label;
  }
  const subject = (document.getElementById("dj-subject-template").value || "")
    .trim();
  const message = (document.getElementById("dj-message-template").value || "")
    .trim();
  try {
    const item = staged[0];
    await api.createOutreach({
      recipient: {
        contact_uid: item.contact_uid,
        identity_key: item.identity_key,
        target_type: "dj",
        name: item.name || null,
        role: item.role || null,
        organization: djOrganization(detail),
        email: String(item.email || "").trim(),
        outreach_class: item.outreach_class || "email",
        submission_url: item.submission_url || null,
        source_url: item.source_url || null,
      },
      track,
      subject,
      message,
    });
    for (const stagedItem of staged) basket.remove(stagedItem.contact_uid);
    status.textContent =
      "Created 1 outreach record for this DJ — opening Outreach.";
    window.location.hash = outreachHref;
  } catch (error) {
    ui.disabled = false;
    ui.textContent = "Start outreach";
    status.textContent = `Could not create the record: ${
      error instanceof ApiError ? error.message : String(error)}`;
  }
}

function outreachSection(detail, basket, onStagedChange) {
  const uid = "dj:" + detail.dj_id;
  const status = el("p", { class: "dim dj-outreach-status", role: "status" });
  const add = el("button", { class: "primary", id: "dj-add-outreach" },
    "Add to outreach");
  add.addEventListener("click", () => {
    if (!stageRecipient(detail, basket)) {
      status.textContent = "This DJ is already staged below.";
      return;
    }
    onStagedChange();
    status.textContent = "Staged this DJ for outreach — choose a release and start.";
  });

  const subjectInput = el("input", {
    type: "text", id: "dj-subject-template",
    placeholder: "Subject (optional, e.g. \"New release for consideration\")",
    autocomplete: "off",
  });
  const messageInput = el("textarea", {
    id: "dj-message-template", rows: "5",
    placeholder: "Message template (optional) — personalize it later on the "
      + "Outreach page.",
    autocomplete: "off",
  });

  const start = el("button", { class: "primary" }, "Start outreach");
  start.addEventListener("click", () =>
    startOutreach(detail, basket, start, status));

  const remove = el("button", { class: "subtle" }, "Remove DJ");
  remove.addEventListener("click", async () => {
    if (!window.confirm(
      "Remove this DJ profile? Outreach records for them are kept.")) {
      return;
    }
    remove.disabled = true;
    try {
      await api.deleteDj(detail.dj_id);
      window.location.hash = djsHref;
    } catch (error) {
      remove.disabled = false;
      status.textContent = `Could not remove the DJ: ${
        error instanceof ApiError ? error.message : String(error)}`;
    }
  });

  return el("section", { class: "card", id: "dj-outreach" },
    el("p", { class: "dim section-label" }, "Outreach"),
    el("h2", {}, "Prepare outreach for this DJ"),
    el("p", { class: "dim" },
      "Pick a release from My music, add this DJ, and click Start outreach ",
      "to create an outreach RECORD (target_type dj). Records land on the ",
      "Outreach page where you compose and hand them off."),
    el("div", { class: "staged-grid" },
      el("div", { class: "staged-col" },
        el("label", { class: "field" },
          el("span", {}, "Release (from My music)"),
          releaseSelector(),
          el("span", { class: "dim hint" },
            "Attached to the record created below."))),
      el("div", { class: "staged-col" },
        el("label", { class: "field" },
          el("span", {}, "Subject"),
          subjectInput),
        el("label", { class: "field" },
          el("span", {}, "Message template"),
          messageInput))),
    el("div", { class: "actions-row" },
      add,
      start,
      remove,
      el("a", { class: "linkish", href: outreachHref },
        "View prepared records →")),
    status);
}

/* ---------------------------------------------------------------------------
 * Section 5 — OUTREACH HISTORY (records for this DJ)
 * ------------------------------------------------------------------------- */

function historyRow(record) {
  return el("div", { class: "contact-route" },
    el("span", {
      class: `chip status-${String(record.status || "ready")}`,
    }, record.status || "ready"),
    el("span", {}, record.subject || "—"),
    el("span", { class: "dim" },
      " · " + (record.track && record.track.original_filename
        ? record.track.original_filename
        : record.track_id || "no music")),
    el("span", { class: "dim" }, " · " + (record.created_at || "")));
}

function historySection(detail) {
  const records = detail.outreach || [];
  return el("section", { class: "card", id: "dj-outreach-history" },
    el("p", { class: "dim section-label" }, "Outreach history"),
    el("h2", {}, "Records for this DJ"),
    records.length
      ? el("div", { class: "contact-routes" }, records.map(historyRow))
      : el("p", { class: "dim" },
        "No outreach records created for this DJ yet."));
}

/* ---------------------------------------------------------------------------
 * Intelligence Details (collapsed): classification evidence + source URLs.
 * ------------------------------------------------------------------------- */

function djKvCard(pairs) {
  const rows = pairs
    .filter(([, value]) => value !== null && value !== undefined)
    .map(([key, value]) => [el("dt", {}, key), el("dd", {}, value)]);
  if (rows.length === 0) return null;
  return el("dl", { class: "kv" }, rows.flat());
}

function classificationCard(cls) {
  if (!cls) return null;
  return el("div", {},
    el("h3", {}, "DJ classification"),
    djKvCard([
      ["classification", cls.verdict || null],
      ["kind", cls.kind || null],
      ["reason", cls.reason || null],
      ["evidence", cls.evidence_url
        ? externalLink(cls.evidence_url) : null],
      ["evaluated at", cls.evaluated_at || null],
    ]));
}

function sourceUrlsCard(detail) {
  const sources = Array.isArray(detail.source_urls)
    ? detail.source_urls.slice(0, 10) : [];
  if (sources.length === 0) return null;
  return el("div", {},
    el("h3", {}, "Source URLs"),
    el("ul", { class: "provenance-list" },
      sources.map((url) => el("li", {}, externalLink(url)))));
}

function intelligenceDetails(detail) {
  const cls = detail.classification
    && typeof detail.classification === "object"
    ? detail.classification : null;
  const parts = [classificationCard(cls), sourceUrlsCard(detail)]
    .filter(Boolean);
  if (parts.length === 0) {
    return el("details", { class: "card detail-collapse" },
      el("summary", {}, "Intelligence Details"),
      el("div", { class: "detail-body" },
        el("p", { class: "dim" },
          "No classification evidence recorded for this DJ yet.")));
  }
  return el("details", { class: "card detail-collapse" },
    el("summary", {}, "Intelligence Details"),
    el("div", { class: "detail-body" }, parts));
}

/* ---------------------------------------------------------------------------
 * View assembly
 * ------------------------------------------------------------------------- */

export function renderDJProfileView(root, djId, basket) {
  root.append(el("p", { class: "dim" }, "Loading…"));

  api.dj(djId).then((detail) => {
    const onStagedChange = () => { /* staged box is stateless: rerender only */ };
    root.replaceChildren(
      el("a", { class: "linkish back-link", href: djsHref }, "← All DJs"),
      overviewSection(detail),
      channelsSection(detail),
      submissionSection(detail),
      outreachSection(detail, basket, onStagedChange),
      historySection(detail),
      intelligenceDetails(detail));
  }).catch((error) => {
    root.replaceChildren(errorBanner(error));
  });
}