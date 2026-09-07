/* Station list view — find a radio station.
 *
 * Provides search + optional filters and a clear, action-first list of the
 * stations the engine has discovered. Filters map onto the real backend
 * listing endpoint; the backend owns interpretation. */

import { api, ApiError } from "../api.js";
import { chips, el } from "../dom.js";
import { stationHref } from "../router.js";

const LIMIT_CHOICES = [25, 50, 100, 200];

function errorBanner(error) {
  const detail = error instanceof ApiError
    ? `${error.code}: ${error.message}`
    : String(error);
  return el("div", { class: "banner-error", role: "alert" },
    "Could not reach station data. ", el("strong", {}, detail),
    " — check that the engine server is running.");
}

function emptyState() {
  return el("div", { class: "banner-info" },
    "No stations match the current search. Try clearing the search box.");
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
    field("search station", text("q", "e.g. KEXP", current.q)),
    field("genre", text("genre", "e.g. rock", current.genre)),
    field("country", text("country", "e.g. US", current.country)),
    field("page size", limitSelect),
    el("button", { class: "primary", type: "submit" }, "Search"),
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
  return `${total} station${total === 1 ? "" : "s"} found`;
}

function locationOf(station) {
  return [station.city, station.state_or_region, station.country]
    .filter(Boolean).join(", ") || "—";
}

function resultRow(station) {
  return el(
    "tr",
    { class: "station-row" },
    el("td", {},
      el("a", { class: "station-name", href: stationHref(station.identity_key) },
        station.name || "(unnamed station)"),
      el("div", { class: "dim" }, station.domain ?? "—")),
    el("td", {}, chips(station.genres)),
    el("td", {}, chips(station.formats)),
    el("td", { class: "dim" }, locationOf(station)),
    el("td", { class: "actions-cell" },
      el("a", { class: "subtle buttonish", href: stationHref(station.identity_key) },
        "Send music")),
  );
}

export function renderListView(root, basket) {
  let state = { limit: 50, offset: 0 };
  const resultsCard = el("section", { class: "card" });

  async function load() {
    resultsCard.replaceChildren(el("p", { class: "dim" }, "Loading…"));
    try {
      const data = await api.stations(state);
      if ((data.stations || []).length === 0) {
        resultsCard.replaceChildren(
          el("h2", {}, "Stations"), emptyState());
        return;
      }
      resultsCard.replaceChildren(
        el("h2", {}, "Stations"),
        el("p", { class: "dim" }, summaryLine(data.total, data.limit, data.offset)),
        el("table", { class: "results" },
          el("thead", {}, el("tr", {},
            el("th", {}, "station"), el("th", {}, "genres"),
            el("th", {}, "formats"), el("th", {}, "location"),
            el("th", {}, "submit"))),
          el("tbody", {},
            (data.stations || []).map((station) => resultRow(station)))),
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
    el("h2", {}, "Find a radio station"),
    el("p", { class: "dim" },
      "Search the stations we know about, open one, and find a real way ",
      "to send your music."),
    filterForm(state, (applied) => {
      state = { ...state, offset: 0 };
      for (const key of ["q", "genre", "country"]) {
        if (applied[key]) state[key] = applied[key];
        else delete state[key];
      }
      if (applied.limit) state.limit = Number(applied.limit);
      load();
    }));

  root.append(card, resultsCard);
  load();
}
