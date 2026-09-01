/* Operator console bootstrap.
 *
 * Wires the hash router, the recipient basket panel, and the schema badge
 * from the live /api/v1/health endpoint. No configuration, no secrets, no
 * mock data: every number and label on screen comes from the backend. */

import { api, ApiError } from "./api.js";
import { Basket } from "./basket.js";
import { el } from "./dom.js";
import { outreachHref, startRouter } from "./router.js";
import { renderListView } from "./views/list.js";
import { renderOutreachView } from "./views/outreach.js";
import { renderStationView, teardownStationView } from "./views/station.js";
import { renderTracksView } from "./views/tracks.js";

const viewRoot = document.getElementById("view");
const basketPanel = document.getElementById("basket-panel");
const basketCount = document.getElementById("basket-count");
const schemaBadge = document.getElementById("schema-badge");

const basket = new Basket(window.sessionStorage);

function renderHeader(items) {
  basketCount.textContent = `recipients: ${items.length}`;
}

function renderBasketPanel(items) {
  if (items.length === 0) {
    basketPanel.replaceChildren(
      el("section", { class: "card" },
        el("h2", {}, "Selected recipients"),
        el("p", { class: "dim" },
          "Open a station to add contacts. Selection is local to this ",
          "browser session; nothing is sent by this application.")));
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
      el("h2", {}, `Recipients (${items.length})`),
      items.map((item) =>
        el("div", { class: "recipient-item" },
          el("span", {},
            item.name || "(unnamed contact)",
            el("div", { class: "recipient-meta" },
              `${item.station_name ?? ""}`
              + `${item.role ? " · " + item.role : ""}`,
              item.email ? item.email : "")),
          el("span", {},
            el("button", {
              class: "linkish",
              onClick: () => basket.remove(item.contact_uid),
            }, "remove")))),
      el("div", { class: "actions-row" }, outreachButton, clearButton)));
}

basket.subscribe(renderBasketPanel);
basket.subscribe(renderHeader);

async function refreshSchemaBadge() {
  try {
    const data = await api.health();
    schemaBadge.textContent =
      `API online · storage schema v${data.schema_version}`;
    schemaBadge.className = "badge badge-accent";
  } catch (error) {
    const detail = error instanceof ApiError ? error.code : "error";
    schemaBadge.textContent = `API unreachable (${detail})`;
    schemaBadge.className = "badge";
  }
}
refreshSchemaBadge();

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
});
