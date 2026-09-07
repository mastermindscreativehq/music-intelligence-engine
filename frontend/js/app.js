/* Music Intelligence Engine bootstrap.
 *
 * Wires the hash router and the outreach basket panel. No configuration,
 * no secrets, no mock data: every station shown comes from the backend. */

import { Basket } from "./basket.js";
import { el } from "./dom.js";
import { outreachHref, startRouter } from "./router.js";
import { renderListView } from "./views/list.js";
import { renderOutreachView } from "./views/outreach.js";
import { renderOutreachHistoryView } from "./views/outreachHistory.js";
import { renderStationView, teardownStationView } from "./views/station.js";
import { renderTracksView } from "./views/tracks.js";

const viewRoot = document.getElementById("view");
const basketPanel = document.getElementById("basket-panel");
const basketCount = document.getElementById("basket-count");

const basket = new Basket(window.sessionStorage);

function renderHeader(items) {
  basketCount.textContent = `outreach list: ${items.length}`;
}

function renderBasketPanel(items) {
  if (items.length === 0) {
    basketPanel.replaceChildren(
      el("section", { class: "card" },
        el("h2", {}, "Your outreach list"),
        el("p", { class: "dim" },
          "Open a station and pick a verified way to send your music. ",
          "Stations you add show up here.")));
    return;
  }

  const outreachButton = el("button", { class: "primary" }, "Start outreach");
  outreachButton.addEventListener("click", () => {
    window.location.hash = outreachHref(items.map((item) => item.contact_uid));
  });

  const clearButton = el("button", { class: "subtle" }, "clear");
  clearButton.addEventListener("click", () => basket.clear());

  basketPanel.replaceChildren(
    el("section", { class: "card" },
      el("h2", {}, `Outreach list (${items.length})`),
      items.map((item) =>
        el("div", { class: "recipient-item" },
          el("span", {},
            item.station_name || item.name || "(unnamed station)",
            el("div", { class: "recipient-meta" },
              item.email ? item.email
                : (item.submission_url ? "submission page" : ""))),
          el("span", {},
            el("button", {
              class: "linkish",
              onClick: () => basket.remove(item.contact_uid),
            }, "remove")))),
      el("div", { class: "actions-row" }, outreachButton, clearButton)));
}

basket.subscribe(renderBasketPanel);
basket.subscribe(renderHeader);

startRouter(viewRoot, {
  list(root) {
    teardownStationView();
    renderListView(root, basket);
  },
  station(root, identityKey) {
    renderStationView(root, identityKey, basket);
  },
  tracks(root) {
    teardownStationView();
    renderTracksView(root);
  },
  outreach(root, uids) {
    teardownStationView();
    renderOutreachView(root, uids, basket);
  },
  outreachHistory(root) {
    teardownStationView();
    renderOutreachHistoryView(root);
  },
});
