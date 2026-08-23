/* Station search / filter / selection view.
 *
 * Filters map 1:1 onto the real backend listing endpoint
 * GET /api/v1/stations?limit&offset&q&status&genre&format&country&
 * min_confidence — the backend owns interpretation; this view only
 * collects operator input and renders the response. */

import { api, ApiError } from "../api.js";
import { chips, confidenceBar, el, fmtPct } from "../dom.js";
import { stationHref } from "../router.js";

const LIMIT_CHOICES = [25, 50, 100, 200];

function errorBanner(error) {
  const detail = error instanceof ApiError
    ? `${error.code}: ${error.message}`
    : String(error);
  return el("div", { class: "banner-error", role: "alert" },
    "Could not reach station data. ", el("strong", {}, detail),
    " — adjust filters or verify the API server is running.");
}

function emptyState() {
  return el("div", { class: "banner-info" },
    "No stations match the current filters. Storage is populated by the ",
    "discovery/enrichment CLI pipeline; this console never invents rows.");
}

function filterForm(current, onApply) {
  const field = (labelText, input) =>
    el("label", { class: "field" }, el("span", {}, labelText), input);

  const text = (name, placeholder, value) => {
    const node = el("input", {
      type: "text", name, placeholder, value: value ?? "",
      autocomplete: "off",
    });
    return node;
  };

  const statusSelect = el(
    "select", { name: "status" },
    el("option", { value: "" }, "any status"),
    ["enriched", "new", "broken"].map((value) =>
      el("option", { value, selected: current.status === value }, value)),
  );

  const confidenceInput = el("input", {
    type: "number", name: "min_confidence", min: "0", max: "1",
    step: "0.05", placeholder: "0 – 1",
    value: current.min_confidence ?? "",
  });

  const limitSelect = el(
    "select", { name: "limit" },
    LIMIT_CHOICES.map((value) =>
      el("option", { value, selected: Number(current.limit || 50) === value },
        `${value} / page`)),
  );

  const form = el(
    "form",
    { class: "filter-form", onSubmit: (event) => {
      event.preventDefault();
      const data = new FormData(form);
      onApply(Object.fromEntries(data.entries()));
    } },
    field("search name / domain", text("q", "e.g. kzow", current.q)),
    field("status", statusSelect),
    field("genre", text("genre", "e.g. news", current.genre)),
    field("format", text("format", "e.g. talk", current.format)),
    field("country", text("country", "e.g. US", current.country)),
    field("min confidence", confidenceInput),
    field("page size", limitSelect),
    el("button", { class: "primary", type: "submit" }, "Apply filters"),
    el("button", {
      class: "subtle", type: "button",
      onClick: () => onApply({}),
    }, "Reset"),
  );
  return form;
}

function summaryLine(total, limit, offset) {
  const from = total === 0 ? 0 : offset + 1;
  const to = Math.min(offset + limit, total);
  return `showing ${from}–${to} of ${total} stations`;
}

function resultRow(station, basket) {
  const addPreferred = async (event) => {
    event.stopPropagation();
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const payload = await api.contacts(station.identity_key);
      const preferred = payload.preferred_submission_contacts || [];
      if (preferred.length === 0) {
        button.textContent = "no backend-preferred contacts";
        return;
      }
      for (const contact of preferred) {
        basket.add({
          contact_uid: contact.contact_uid,
          identity_key: station.identity_key,
          station_name: station.name,
          role: contact.role,
          email: contact.email,
          name: null,
        });
      }
      button.textContent = `added ${preferred.length}`;
    } catch (error) {
      button.textContent =
        error instanceof ApiError ? `error: ${error.code}` : "error";
    } finally {
      button.disabled = false;
    }
  };

  return el(
    "tr",
    { class: "station-row" },
    el("td", {},
      el("a", { class: "station-name", href: stationHref(station.identity_key) },
        station.name || "(unnamed)"),
      el("div", { class: "dim" }, station.domain ?? "—")),
    el("td", {}, chips(station.genres)),
    el("td", {}, chips(station.formats)),
    el("td", {},
      confidenceBar(station.confidence_score),
      el("div", { class: "dim" }, fmtPct(station.confidence_score))),
    el("td", {}, String(station.status ?? "—")),
    el("td", { class: "dim" },
      [station.city, station.state_or_region, station.country]
        .filter(Boolean).join(", ") || "—"),
    el("td", {},
      el("button", { class: "linkish", onClick: addPreferred },
        "+ add backend-preferred contacts"),
      " ",
      el("a", { href: stationHref(station.identity_key) }, "inspect")),
  );
}

export function renderListView(root, basket) {
  let state = { limit: 50, offset: 0 };
  const resultsCard = el("section", { class: "card" });

  async function load() {
    resultsCard.replaceChildren(el("p", { class: "dim" }, "Loading…"));
    try {
      const data = await api.stations(state);
      resultsCard.replaceChildren(
        el("h2", {}, "Stations"),
        el("p", { class: "dim" },
          summaryLine(data.total, data.limit, data.offset)),
        el("table", { class: "results" },
          el("thead", {}, el("tr", {},
            el("th", {}, "station"), el("th", {}, "genres"),
            el("th", {}, "formats"), el("th", {}, "confidence"),
            el("th", {}, "status"), el("th", {}, "location"),
            el("th", {}, "actions"))),
          el("tbody", {},
            (data.stations || []).map((station) =>
              resultRow(station, basket)))),
        pagination(data));
    } catch (error) {
      resultsCard.replaceChildren(el("h2", {}, "Stations"), errorBanner(error));
    }
  }

  function pagination(data) {
    const back = el("button", {
      disabled: data.offset === 0,
      onClick: () => { state.offset = Math.max(0, state.offset - data.limit); load(); },
    }, "← previous");
    const next = el("button", {
      disabled: data.offset + data.limit >= data.total,
      onClick: () => { state.offset += data.limit; load(); },
    }, "next →");
    return el("div", { class: "actions-row" }, back, next);
  }

  const card = el("section", { class: "card" },
    el("h2", {}, "Search & filter"),
    filterForm(state, (applied) => {
      state = { ...state, offset: 0 };
      for (const key of ["q", "status", "genre", "format", "country"]) {
        if (applied[key]) state[key] = applied[key];
        else delete state[key];
      }
      if (applied.min_confidence !== undefined && applied.min_confidence !== "") {
        state.min_confidence = applied.min_confidence;
      } else {
        delete state.min_confidence;
      }
      if (applied.limit) state.limit = Number(applied.limit);
      load();
    }));

  root.append(card, resultsCard);
  load();
}
