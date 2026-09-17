/* Opportunities view (#/opportunities) — Phase 3 opportunity intelligence.
 *
 * Deterministic, explainable, read-only: pick a release from My Music and
 * see every station ranked by a transparent 0..100 score with reasons,
 * recommended contact + route, and honest reachability.
 *
 * The engine never sends and never auto-creates outreach records; the
 * primary action funnels into the existing single composer
 * (openOutreachModal) exactly like a station page does. History is read
 * from the ledger and never rewritten. */

import { api, ApiError } from "../api.js";
import { el } from "../dom.js";
import {
  opportunitiesTrackHref,
  outreachHistoryHref,
  stationHref,
  tracksHref,
} from "../router.js";
import { openOutreachModal } from "./outreachModal.js";

function errorBanner(error) {
  const detail = error instanceof ApiError
    ? `${error.code}: ${error.message}`
    : String(error);
  return el("p", { class: "banner-error", role: "alert" },
    "Error loading opportunities: ", detail);
}

function tierChip(tier) {
  const cls = tier === "HIGH" ? "tier-high" : tier === "MEDIUM"
    ? "tier-medium" : "tier-low";
  return el("span", { class: `chip ${cls}` }, tier);
}

function reasonItem(r) {
  const sign = r.sign === "-" ? "\u2212" : r.sign;
  return el("li", {}, el("span", { class: "sign" }, sign), " ", r.text);
}

function contactLine(opp) {
  const c = opp.recommended_contact;
  if (!c) return el("div", { class: "opp-contact dim" },
    "No music decision-maker contact on record.");
  const rel = c.relevance || {};
  return el("div", { class: "opp-contact" },
    el("strong", {}, c.name || "(unnamed contact)"),
    el("span", { class: "dim" },
      ` \u00b7 role: ${c.role || "unknown"} \u00b7 relevance: ${rel.label || "unknown"}`));
}

function routeLine(opp) {
  const route = opp.recommended_route;
  if (!route) return el("div", { class: "opp-route dim" },
    "No outreach route recorded for this station.");
  const badge = opp.route_verified
    ? el("span", { class: "chip check-ok" }, "verified")
    : el("span", { class: "chip inference" },
        route.evidence_state || "discovered");
  return el("div", { class: "opp-route" },
    el("strong", {}, route.title || "route"),
    " \u2014 ", badge, " ", route.type || "",
    route.value ? ` \u00b7 ${route.value}` : "",
    route.source_url
      ? el("span", { class: "dim" }, ` source: ${route.source_url}`)
      : null);
}

function recipientLine(opp) {
  if (!opp.recipient) return null;
  const route = opp.recipient.email || opp.recipient.submission_url;
  return el("div", { class: "opp-recipient" },
    el("span", { class: "dim" }, "Recipient: "), route);
}

function historyLines(opp) {
  if (!opp.already_contacted || !(opp.history || []).length) return null;
  return el("div", { class: "opp-history-line" },
    el("span", { class: "chip" }, "already contacted"),
    " ",
    ...opp.history.map((h) =>
      el("span", { class: "dim" },
        ` ${h.outreach_id} (${h.status}${h.created_at ? `, ${h.created_at}` : ""})`)));
}

function buildFilters(items) {
  const genres = [...new Set(
    items.flatMap((o) => (o.station.genres || []).map((g) =>
      g.trim().toLowerCase())))].sort();
  const countries = [...new Set(
    items.map((o) => (o.station.country || "").trim())
      .filter(Boolean))].sort();
  const types = [...new Set(
    items.map((o) => (o.station.station_type || "").trim())
      .filter(Boolean))].sort();

  const tierSel = el("select", { name: "tier" },
    ["", "HIGH", "MEDIUM", "LOW"].map((v) =>
      el("option", { value: v }, v || "All tiers")));
  const genreSel = el("select", { name: "genre" },
    [el("option", { value: "" }, "All genres"),
     ...genres.map((g) => el("option", { value: g }, g))]);
  const countrySel = el("select", { name: "country" },
    [el("option", { value: "" }, "All countries"),
     ...countries.map((c) => el("option", { value: c }, c))]);
  const typeSel = el("select", { name: "station_type" },
    [el("option", { value: "" }, "All types"),
     ...types.map((t) => el("option", { value: t }, t))]);
  const contactSel = el("select", { name: "has_contact" },
    [el("option", { value: "" }, "Any contacts"),
     el("option", { value: "true" }, "With contact"),
     el("option", { value: "false" }, "No contact")]);
  const routeSel = el("select", { name: "has_route" },
    [el("option", { value: "" }, "Any route"),
     el("option", { value: "true" }, "Verified route"),
     el("option", { value: "false" }, "Unverified route")]);
  const outreachSel = el("select", { name: "outreach_status" },
    [el("option", { value: "" }, "Any outreach"),
     el("option", { value: "new" }, "New"),
     el("option", { value: "contacted" }, "Already contacted")]);
  const sortSel = el("select", { name: "sort" },
    ["score", "name", "relevance"].map((v) =>
      el("option", { value: v }, v)));
  const orderSel = el("select", { name: "order" },
    [["desc", "descending"], ["asc", "ascending"]].map(([v, l]) =>
      el("option", { value: v }, l)));

  const bar = el("div", { class: "opp-toolbar" },
    el("label", {}, "Tier", tierSel),
    el("label", {}, "Genre", genreSel),
    el("label", {}, "Country", countrySel),
    el("label", {}, "Type", typeSel),
    el("label", {}, "Contacts", contactSel),
    el("label", {}, "Route", routeSel),
    el("label", {}, "Outreach", outreachSel),
    el("label", {}, "Sort", sortSel),
    el("label", {}, "Order", orderSel));
  bar._refs = { tierSel, genreSel, countrySel, typeSel, contactSel,
                routeSel, outreachSel, sortSel, orderSel };
  return bar;
}

function renderResults(state, resultsSlot, filters) {
  const refs = (filters && filters._refs) || {};
  const tier = refs.tierSel ? refs.tierSel.value : "";
  const genre = refs.genreSel ? refs.genreSel.value : "";
  const country = refs.countrySel ? refs.countrySel.value : "";
  const stationType = refs.typeSel ? refs.typeSel.value : "";
  const hasContact = refs.contactSel ? refs.contactSel.value : "";
  const hasRoute = refs.routeSel ? refs.routeSel.value : "";
  const outreachStatus = refs.outreachSel ? refs.outreachSel.value : "";
  const sort = refs.sortSel ? refs.sortSel.value : "score";
  const order = refs.orderSel ? refs.orderSel.value : "desc";

  let items = [...state.items];
  if (tier) items = items.filter((o) => o.tier === tier);
  if (genre) items = items.filter((o) =>
    (o.station.genres || []).some((g) =>
      g.trim().toLowerCase() === genre.toLowerCase()));
  if (country) items = items.filter((o) =>
    (o.station.country || "").toLowerCase() === country.toLowerCase());
  if (stationType) items = items.filter((o) =>
    (o.station.station_type || "").toLowerCase() === stationType.toLowerCase());
  if (hasContact === "true") items = items.filter((o) => o.recommended_contact);
  if (hasContact === "false") items = items.filter((o) => !o.recommended_contact);
  if (hasRoute === "true") items = items.filter((o) => o.route_verified);
  if (hasRoute === "false") items = items.filter((o) => !o.route_verified);
  if (outreachStatus) items = items.filter((o) =>
    o.outreach_status === outreachStatus);

  if (sort === "name") {
    items.sort((a, b) =>
      ((a.station.name || "").localeCompare(b.station.name || ""))
        * (order === "desc" ? -1 : 1));
  } else if (sort === "relevance") {
    items.sort((a, b) =>
      (((a.recommended_route || {}).priority || 99)
       - ((b.recommended_route || {}).priority || 99))
        * (order === "desc" ? 1 : -1));
  } else {
    items.sort((a, b) => (b.score - a.score) * (order === "asc" ? -1 : 1));
  }

  if (!items.length) {
    const msg = state.items.length
      ? "No opportunities match the current filters."
      : "No strong opportunities found for this release.";
    resultsSlot.replaceChildren(el("p", { class: "dim" }, msg));
    return;
  }

  const cards = items.map((opp) => {
    const station = opp.station || {};
    const stnName = el("a", {
      class: "station-name",
      href: stationHref(station.identity_key || ""),
    }, station.name || "(unnamed station)");
    const meta = el("div", { class: "opp-meta" },
      (station.genres || []).map((g) => el("span", { class: "chip" }, g)),
      station.station_type
        ? el("span", { class: "chip" }, station.station_type) : null,
      station.location_status
        ? el("span", {
            class: `chip location-${station.location_status === "available"
                    ? "available" : "unavailable"}`,
          }, station.location_status) : null,
      station.country
        ? el("span", { class: "dim" }, station.country) : null);
    const reachability = opp.reachable
      ? el("div", { class: "opp-reachability" },
          "Reachable via ",
          opp.recipient && opp.recipient.email ? "email" : "submission page")
      : el("div", { class: "opp-reachability dim" },
          opp.already_contacted
            ? "Already contacted \u2014 review outreach history "
              + "before sending again."
            : "Not directly reachable via a verified route at this time.");
    const reasons = (opp.reasoning && opp.reasoning.reasons || [])
      .map(reasonItem);

    const actions = el("div", { class: "opp-row" });
    if (opp.reachable && !opp.already_contacted && opp.recipient) {
      const btn = el("button", { class: "primary" }, "Prepare outreach");
      btn.addEventListener("click", () => {
        openOutreachModal({
          recipient: opp.recipient,
          stationName: station.name || "",
          preselectedTrackId: (state.track && state.track.track_id) || "",
        });
      });
      actions.append(btn);
    } else if (opp.already_contacted) {
      actions.append(el("a", {
        class: "buttonish",
        href: outreachHistoryHref(),
      }, "Already contacted \u2014 view history"));
    }
    actions.append(el("a", {
      class: "linkish",
      href: stationHref(station.identity_key || ""),
    }, "Open station"));

    return el("article", { class: "card opp-card" },
      el("div", { class: "opp-header" },
        stnName, tierChip(opp.tier),
        el("span", { class: "opp-score" }, String(opp.score))),
      meta,
      contactLine(opp),
      routeLine(opp),
      recipientLine(opp),
      reachability,
      historyLines(opp),
      reasons.length
        ? el("ul", { class: "opp-reason-list" }, ...reasons)
        : null,
      actions);
  });

  resultsSlot.replaceChildren(
    el("p", { class: "dim" },
      `${items.length} opportunit${items.length === 1 ? "y" : "ies"}`),
    ...cards);
}

export function renderOpportunitiesView(root, trackId) {
  const state = { items: [], total: 0, track: null, error: null };
  let currentTrackId = trackId || null;
  let filtersEl = null;
  const resultsSlot = el("div", { class: "opp-list" },
    el("p", { class: "dim" }, "Select a release to calculate opportunities."));

  root.append(
    el("h1", {}, "Opportunities"),
    el("p", { class: "dim" },
      "Select a release from My Music to rank every station by a transparent, "
      + "explainable 0\u2026100 score with reasons, recommended contact, and "
      + "honest reachability \u2014 without sending or auto-creating outreach "
      + "records."),
    resultsSlot,
  );

  const trackSelect = el("select", { name: "track" },
    el("option", { value: "" }, "Select a release\u2026"));
  const calculateBtn = el("button", { class: "primary", disabled: true },
    "Calculate opportunities");
  filtersEl = el("div", { class: "opp-toolbar" },
    el("label", {}, "Release", trackSelect), calculateBtn);
  root.insertBefore(filtersEl, resultsSlot);

  trackSelect.addEventListener("change", () => {
    calculateBtn.disabled = !trackSelect.value;
  });
  calculateBtn.addEventListener("click", () => {
    if (!trackSelect.value) return;
    currentTrackId = trackSelect.value;
    const next = opportunitiesTrackHref(currentTrackId);
    if (decodeURIComponent(location.hash || "#/")
        === decodeURIComponent(next)) {
      runCompute();
    } else {
      window.location.hash = next;
    }
  });

  function setError(error) {
    state.error = error;
    resultsSlot.replaceChildren(errorBanner(error));
  }

  function renderFromState() {
    if (filtersEl && filtersEl._refs) {
      renderResults(state, resultsSlot,
        { _refs: filtersEl._refs });
    }
  }

  async function loadTracks() {
    try {
      const data = await api.tracks({ status: "ready", limit: 200 });
      const tracks = data.tracks || [];
      if (!tracks.length) {
        resultsSlot.replaceChildren(
          el("section", { class: "card" },
            el("h2", {}, "No releases in My Music"),
            el("p", { class: "dim" },
              "Upload an MP3 release in My Music before "
              + "calculating opportunities."),
            el("div", { class: "actions-row" },
              el("a", { class: "primary", href: tracksHref },
                "Go to My Music"))));
        filtersEl.style.display = "none";
        return;
      }
      tracks.forEach((t) => {
        trackSelect.append(el("option", { value: t.track_id },
          t.original_filename || t.track_id));
      });
      if (currentTrackId) {
        trackSelect.value = currentTrackId;
        calculateBtn.disabled = false;
        runCompute();
      }
    } catch (error) {
      setError(error);
    }
  }

  async function runCompute() {
    if (!currentTrackId) return;
    resultsSlot.replaceChildren(
      el("p", { class: "dim" }, "Calculating opportunities\u2026"));
    try {
      const data = await api.opportunities({
        track_id: currentTrackId, limit: 200,
      });
      state.items = data.opportunities || [];
      state.track = data.track || null;
      state.total = data.total || state.items.length;
      while (filtersEl.childNodes.length > 3) {
        filtersEl.removeChild(filtersEl.lastChild);
      }
      if (state.items.length) {
        const filters = buildFilters(state.items);
        filtersEl._refs = filters._refs;
        filtersEl.append(filters);
        filters.addEventListener("change", () =>
            renderResults(state, resultsSlot, { _refs: filtersEl._refs }));
      }
      renderResults(state, resultsSlot,
        state.items.length ? { _refs: filtersEl._refs } : {});
    } catch (error) {
      setError(error);
    }
  }

  loadTracks();
}
