/* Station list view — find a radio station.
 *
 * Provides search + optional filters and a clear, action-first list of the
 * stations the engine has discovered. Filters map onto the real backend
 * listing endpoint; the backend owns interpretation. */

import { api, ApiError } from "../api.js";
import { chips, el } from "../dom.js";
import { outreachHref, stationHref } from "../router.js";

const LIMIT_CHOICES = [25, 50, 100, 200];

/* Best available route for sending music to a station, in priority order
 * (submission page -> verified email -> contact page -> program/DJ page).
 * Only discovered URLs and verified emails are used; nothing is guessed. */
function submissionPages(usefulPages) {
  return (usefulPages || []).filter((p) => p
    && typeof p.url === "string" && /^https?:\/\//i.test(p.url)
    && (p.category === "send_music"
      || p.category === "submission_guidelines"));
}

function bestCategoryPage(usefulPages, category) {
  const pages = (usefulPages || []).filter((p) => p
    && p.category === category
    && typeof p.url === "string" && /^https?:\/\//i.test(p.url));
  const usable = pages.filter((p) => (p.label && p.label.trim().length > 2));
  return usable[0] || null;
}

function resolveBestRoute(intel, contactsPayload) {
  const canonical = intel && intel.submission && intel.submission.submission_url;
  if (canonical && canonical.value
    && /^https?:\/\//i.test(String(canonical.value))) {
    return {
      url: canonical.value,
      label: canonical.source_type === "official_website_page"
        ? "official submission page" : "verified submission page",
      role: "music_submission",
    };
  }
  const submissionPage = submissionPages(intel && intel.useful_pages)[0] || null;
  if (submissionPage) {
    return {
      url: submissionPage.url,
      label: submissionPage.label || "submission page",
      role: "music_submission",
    };
  }
  const withEmail = (contactsPayload && contactsPayload.contacts || [])
    .filter((c) => c && c.email && String(c.email).trim());
  const firstEmail = withEmail[0] || null;
  if (firstEmail) {
    return {
      contact: firstEmail,
      label: (firstEmail.role && firstEmail.role.replace(/_/g, " "))
        || "station contact",
      role: "email",
    };
  }
  const contactPage = bestCategoryPage(intel && intel.useful_pages, "contact");
  if (contactPage) {
    return {
      url: contactPage.url,
      label: contactPage.label || "contact page",
      role: "station_contact",
    };
  }
  const djPage = bestCategoryPage(intel && intel.useful_pages, "dj_directory")
    || bestCategoryPage(intel && intel.useful_pages, "programming");
  if (djPage) {
    return {
      url: djPage.url,
      label: djPage.label || "program page",
      role: "station_program_page",
    };
  }
  return null;
}

/* Add the station's best route to the outreach list and open it. */
async function sendMusic(identityKey, stationName, basket) {
  const [intel, contactsPayload] = await Promise.all([
    api.intelligence(identityKey),
    api.contacts(identityKey),
  ]);
  const route = resolveBestRoute(intel, contactsPayload);
  if (!route) return null;
  const uid = `st_${identityKey.replace(/[^a-z0-9]+/gi, "_")}`;
  const recipient = {
    contact_uid: uid,
    identity_key: identityKey,
    station_name: stationName,
    name: (typeof route.label === "string"
      ? route.label.replace(/^official /, "") : "route") || "route",
    role: route.role,
    email: route.contact ? String(route.contact.email).trim() : "",
    source_url: route.contact
      ? (route.contact.source_url || null) : null,
    outreach_class: route.role === "email" ? "email" : "webform",
    submission_url: route.url || null,
    route_kind: route.role === "email" ? "email"
      : route.role === "station_contact" ? "contact"
      : route.role === "station_program_page" ? "dj" : "webform",
    route_label: route.label,
  };
  basket.add(recipient);
  return recipient;
}

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

function resultRow(station, basket) {
  let sendButton;
  const onClick = async () => {
    if (sendButton.disabled) return;
    sendButton.disabled = true;
    sendButton.textContent = "adding…";
    try {
      const recipient = await sendMusic(
        station.identity_key, station.name, basket);
      if (recipient) {
        window.location.hash = outreachHref([recipient.contact_uid]);
        return;
      }
      /* No route: land on the station page where the honest state is shown. */
      window.location.hash = stationHref(station.identity_key);
    } catch (error) {
      window.location.hash = stationHref(station.identity_key);
    }
  };
  sendButton = el("button", { class: "subtle buttonish", onClick },
    "Send music");
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
      sendButton),
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
            (data.stations || []).map((station) => resultRow(station, basket)))),
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
