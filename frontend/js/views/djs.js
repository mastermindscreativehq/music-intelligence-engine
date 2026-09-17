/* DJ list view (#/djs) — find and open a DJ profile (Phase 4).
 *
 * Search + optional filters and an action-first list of the DJs the
 * operator has recorded. Filters map onto the real backend listing
 * endpoint; the backend owns interpretation. Each row opens the DJ profile
 * (the DJ outreach intake). Everything here is presentation only: names and
 * values come verbatim from the backend. */

import { api, ApiError } from "../api.js";
import { chips, el } from "../dom.js";
import { djHref } from "../router.js";
import { stationLocation } from "./stationLocation.js";

const LIMIT_CHOICES = [25, 50, 100, 200];

function errorBanner(error) {
  const detail = error instanceof ApiError
    ? `${error.code}: ${error.message}`
    : String(error);
  return el("div", { class: "banner-error", role: "alert" },
    "Could not reach DJ data. ", el("strong", {}, detail),
    " — check that the engine server is running.");
}

function emptyState() {
  return el("div", { class: "banner-info" },
    "No DJs match the current search. Try clearing the search box.");
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

  const hasContactSelect = el(
    "select", { name: "has_contact" },
    el("option", { value: "", selected: !current.has_contact }, "any"),
    el("option", { value: "true", selected: current.has_contact === "true" },
      "with contact"),
    el("option", { value: "false", selected: current.has_contact === "false" },
      "no contact"),
  );

  const form = el(
    "form",
    { class: "filter-form", onSubmit: (event) => {
      event.preventDefault();
      const data = new FormData(form);
      onApply(Object.fromEntries(data.entries()));
    } },
    field("search DJ", text("q", "e.g. DJ Nobody", current.q)),
    field("genre", text("genre", "e.g. hip hop", current.genre)),
    field("country", text("country", "e.g. US", current.country)),
    field("location", text("location", "e.g. Seattle", current.location)),
    field("DJ type",
      text("dj_type", "e.g. wedding DJ", current.dj_type)),
    field("platform", text("platform", "e.g. Mixcloud", current.platform)),
    field("Station affiliation",
      text("station", "e.g. KEXP (optional)", current.station)),
    field("contact", hasContactSelect),
    field("page size", limitSelect),
    el("button", { class: "primary", type: "submit" }, "Search"),
    el("button", {
      class: "subtle", type: "button",
      onClick: () => onApply({}),
    }, "Reset"),
  );
  return form;
}

function summaryLine(total) {
  return `${total} DJ${total === 1 ? "" : "s"} found`;
}

function resultRow(dj) {
  const subtitle = [dj.station_name, dj.program]
    .filter((value) => value)
    .join(" · ") || dj.platform || "—";
  return el(
    "tr",
    { class: "dj-row" },
    el("td", {},
      el("a", { class: "station-name", href: djHref(dj.dj_id) },
        dj.stage_name || dj.name || "(unnamed DJ)"),
      el("div", { class: "dim" }, subtitle)),
    el("td", {}, chips(dj.genres)),
    el("td", {}, chips(dj.formats)),
    el("td", { class: "dim" }, stationLocation(dj) || "—"),
    el("td", { class: "actions-cell" },
      el("a", { class: "buttonish subtle", href: djHref(dj.dj_id) },
        "Open DJ")),
  );
}

export function renderDJsView(root) {
  let state = { limit: 50, offset: 0 };
  const resultsCard = el("section", { class: "card" });

  // -- Phase 4c: independent public-web DJ discovery ------------------------
  // A deliberately tiny, front-end-only control. It drives the honest
  // POST /api/v1/djs/discover endpoint: when the live provider is not
  // configured the backend answers 503 (dj_discovery_provider_not_configured)
  // and we show that plainly — we never fabricate DJs or pretend seeds are
  // live results.
  const discoverInput = el("input", { type: "search", placeholder: "query…" });
  const discoverCard = el("section", { class: "card" },
    el("h2", {}, "Discover independent DJs"),
    el("p", { class: "dim" }, "Search the public web (e.g. “Afrobeats DJs in "
      + "New York”). Independent professionals only — never radio stations."),
    discoverInput,
    el("button", { class: "primary" }, "Discover"),
    el("p", { class: "dj-discover-status" }));
  const discoverStatus = discoverCard.querySelector(".dj-discover-status");

  (async function () {
    if (!discoverInput) return;
    discoverStatus.replaceChildren("Live provider not configured — DJ "
      + "discovery is unavailable until DJS_SEARCH_* is set (see "
      + ".env.example).");
  })();

  async function load() {
    resultsCard.replaceChildren(el("p", { class: "dim" }, "Loading…"));
    try {
      const data = await api.djs(state);
      if ((data.djs || []).length === 0) {
        resultsCard.replaceChildren(
          el("h2", {}, "DJs"), emptyState());
        return;
      }
      resultsCard.replaceChildren(
        el("h2", {}, "DJs"),
        el("p", { class: "dim" }, summaryLine(data.total)),
        el("table", { class: "results" },
          el("thead", {}, el("tr", {},
            el("th", {}, "DJ"), el("th", {}, "genres"),
            el("th", {}, "formats"), el("th", {}, "location"),
            el("th", {}, "open"))),
          el("tbody", {},
            (data.djs || []).map((dj) => resultRow(dj)))),
        pagination(data));
    } catch (error) {
      resultsCard.replaceChildren(el("h2", {}, "DJs"), errorBanner(error));
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
    el("h2", {}, "Find a DJ"),
    el("p", { class: "dim" },
      "Search the DJ profiles on record, open one, and prepare outreach ",
      "with a release from My music."),
    filterForm(state, (applied) => {
      state = { ...state, offset: 0 };
      for (const key of ["q", "genre", "country", "location", "station"]) {
        if (applied[key]) state[key] = applied[key];
        else delete state[key];
      }
      if (applied.limit) state.limit = Number(applied.limit);
      load();
    }));

  root.append(card, resultsCard);
  load();
}