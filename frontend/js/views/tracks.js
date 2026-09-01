/* Submission assets view (Phase 8): MP3 uploads and stored asset records.
 *
 * Isolated from the station views: this route owns uploads and the asset
 * listing, nothing else. All data comes verbatim from the real submission
 * endpoints through api.js — there are no mocks and no invented metadata.
 * Uploads post raw file bytes with the chosen filename as a query
 * parameter, exactly as the backend contract defines; backend errors
 * (payload_too_large, track_rejected, ...) are surfaced to the operator
 * unchanged. Per-track links are taken from each projection's links.self
 * value supplied by the backend; this module never constructs asset URLs.
 */

import { api, ApiError } from "../api.js";
import { el } from "../dom.js";

/* Mirrors submissions.service.TRACK_STATUSES on the backend. */
const TRACK_STATUSES = ["ready", "quarantined", "archived"];
const LIMIT_CHOICES = [25, 50, 100, 200];

function errorBanner(error) {
  const detail = error instanceof ApiError
    ? `${error.code}: ${error.message}`
    : String(error);
  return el("div", { class: "banner-error", role: "alert" },
    "Upload failed. ", el("strong", {}, detail));
}

function statusChip(status) {
  const known = TRACK_STATUSES.includes(status);
  return el("span",
    { class: `chip${known ? ` ${status}` : ""}` },
    String(status ?? "unknown"));
}

function uploadCard(onUploaded) {
  const fileInput = el("input", {
    type: "file",
    accept: ".mp3,audio/mpeg",
    autocomplete: "off",
  });
  const nameInput = el("input", {
    type: "text",
    placeholder: "override filename (optional)",
    autocomplete: "off",
  });
  const submit = el("button", { class: "primary", type: "submit" },
    "upload");
  const resultLine = el("div", { class: "upload-result" });

  const form = el(
    "form",
    {
      class: "upload-form",
      onSubmit: async (event) => {
        event.preventDefault();
        resultLine.replaceChildren();
        const file = fileInput.files && fileInput.files[0];
        if (!file) {
          resultLine.append(el("span", { class: "dim" },
            "choose an MP3 file first"));
          return;
        }
        submit.disabled = true;
        submit.textContent = "uploading…";
        try {
          const bytes = new Uint8Array(await file.arrayBuffer());
          const filename = nameInput.value.trim() || file.name;
          const data = await api.uploadTrack(bytes, filename);
          resultLine.append(
            el("strong", {}, `stored ${data.track_id} · status `
              + `${data.status} `),
            el("span", { class: "dim" },
              `${data.original_filename} · ${data.size_bytes} bytes`));
          fileInput.value = "";
          nameInput.value = "";
          onUploaded();
        } catch (error) {
          resultLine.append(errorBanner(error));
        } finally {
          submit.disabled = false;
          submit.textContent = "upload";
        }
      },
    },
    el("label", { class: "field" }, el("span", {}, "MP3 file"), fileInput),
    el("label", { class: "field" }, el("span", {}, "filename"),
      nameInput),
    submit,
  );

  return el("section", { class: "card" },
    el("h2", {}, "Submit a track"),
    el("p", { class: "dim" },
      "Files are validated and stored by the backend; identical bytes "
      + "deduplicate to one asset. Rejected content is recorded server-side "
      + "with its reason."),
    form,
    resultLine);
}

function trackDetailPanel(data) {
  const record = data && typeof data === "object" ? data : {};
  const title = record.original_filename || "(untitled upload)";
  const uploadedAt = record.created_at || null;

  function kvRows(pairs) {
    const rows = pairs
      .filter(([, value]) => value !== null && value !== undefined
        && value !== "" && value !== "—" && value !== "–")
      .map(([key, value]) =>
        [el("dt", {}, key),
          el("dd", { class: key === "track_id" || key === "sha256" ? "track-id" : "" },
            String(value))]);
    return Array.isArray(rows) ? rows.flat() : [];
  }

  const main = el("div", { class: "track-overview" },
    el("h3", {}, title),
    el("p", { class: "dim" }, "Asset status · ",
      statusChip(record.status),
      uploadedAt ? el("span", { class: "dim" }, ` · uploaded ${uploadedAt}`) : null));

  const techPairs = [
    ["asset id", record.track_id],
    ["sha256", record.sha256],
    ["file size (bytes)", record.size_bytes],
    ["content type", record.content_type],
    ["status", record.status],
    ["reject reason", record.reject_reason],
    ["notes", record.notes],
    ["changed", record.updated_at],
    ["asset link", record.links && record.links.self],
  ];

  const tech = el("details", { class: "track-tech" },
    el("summary", {}, "Technical Details"),
    el("dl", { class: "kv" }, kvRows(techPairs)));

  return el("div", { class: "track-detail" },
    main,
    tech);
}

function trackRow(track, detailBox) {
  let detailsShown = false;
  const detailsButton = el("button", { class: "linkish" }, "details");
  detailsButton.addEventListener("click", async () => {
    if (detailsShown) {
      detailsShown = false;
      detailsButton.textContent = "details";
      detailBox.replaceChildren();
      return;
    }
    detailsButton.disabled = true;
    detailBox.replaceChildren(el("p", { class: "dim" }, "Loading…"));
    try {
      const data = await api.trackDetail(track.links.self);
      detailsShown = true;
      detailsButton.textContent = "hide details";
      detailBox.replaceChildren(trackDetailPanel(data));
    } catch (error) {
      detailBox.replaceChildren(error instanceof ApiError
        ? errorBanner(error)
        : el("p", { class: "banner-error" }, String(error)));
    } finally {
      detailsButton.disabled = false;
    }
  });

  return el("tr", { class: "station-row" },
    el("td", {},
      el("div", {}, track.original_filename || "(unnamed upload)"),
      el("div", { class: "track-id" }, track.track_id)),
    el("td", {},
      statusChip(track.status),
      track.reject_reason
        ? el("div", { class: "dim" }, track.reject_reason)
        : null),
    el("td", {}, String(track.size_bytes ?? "—")),
    el("td", { class: "dim" }, String(track.created_at ?? "—")),
    el("td", {}, detailsButton));
}

function summaryLine(total, limit, offset) {
  const from = total === 0 ? 0 : offset + 1;
  const to = Math.min(offset + limit, total);
  return `showing ${from}–${to} of ${total} stored assets`;
}

export function renderTracksView(root) {
  let state = { limit: 50, offset: 0 };
  const resultsCard = el("section", { class: "card" });
  const detailBox = el("div", {});

  const statusSelect = el(
    "select", { name: "status" },
    el("option", { value: "" }, "any status"),
    TRACK_STATUSES.map((value) =>
      el("option", { value, selected: state.status === value }, value)));
  const limitSelect = el(
    "select", { name: "limit" },
    LIMIT_CHOICES.map((value) =>
      el("option", { value, selected: Number(state.limit) === value },
        `${value} / page`)));

  const filterForm = el(
    "form",
    { class: "filter-form", onSubmit: (event) => {
      event.preventDefault();
      const applied = Object.fromEntries(
        new FormData(filterForm).entries());
      state.offset = 0;
      if (applied.status) state.status = applied.status;
      else delete state.status;
      if (applied.limit) state.limit = Number(applied.limit);
      load();
    } },
    el("label", { class: "field" }, el("span", {}, "status"), statusSelect),
    el("label", { class: "field" }, el("span", {}, "page size"),
      limitSelect),
    el("button", { class: "primary", type: "submit" }, "Apply filters"),
    el("button", {
      class: "subtle", type: "button",
      onClick: () => {
        state = { limit: 50, offset: 0 };
        load();
      },
    }, "Reset"),
  );

  function pagination(data) {
    const back = el("button", {
      disabled: data.offset === 0,
      onClick: () => {
        state.offset = Math.max(0, state.offset - data.limit);
        load();
      },
    }, "← previous");
    const next = el("button", {
      disabled: data.offset + data.limit >= data.total,
      onClick: () => { state.offset += data.limit; load(); },
    }, "next →");
    return el("div", { class: "actions-row" }, back, next);
  }

  async function load() {
    detailBox.replaceChildren();
    resultsCard.replaceChildren(el("p", { class: "dim" }, "Loading…"));
    try {
      const data = await api.tracks(state);
      resultsCard.replaceChildren(
        el("h2", {}, "Submission assets"),
        el("p", { class: "dim" },
          summaryLine(data.total, data.limit, data.offset)),
        data.total === 0
          ? el("p", { class: "dim" },
            "No submission assets match the current filters. Storage is "
            + "populated by operator uploads; this console never invents "
            + "rows.")
          : el("table", { class: "results" },
            el("thead", {}, el("tr", {},
              el("th", {}, "file"), el("th", {}, "status"),
              el("th", {}, "size (bytes)"), el("th", {}, "stored"),
              el("th", {}, "actions"))),
            el("tbody", {},
              (data.tracks || []).map((track) =>
                trackRow(track, detailBox)))),
        pagination(data));
    } catch (error) {
      resultsCard.replaceChildren(
        el("h2", {}, "Submission assets"),
        errorBanner(error));
    }
  }

  root.append(
    uploadCard(load),
    el("section", { class: "card" },
      el("h2", {}, "Filter assets"),
      filterForm),
    resultsCard,
    detailBox);
  load();
}
