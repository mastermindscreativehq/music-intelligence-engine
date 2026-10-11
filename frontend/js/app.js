/* Music Intelligence Engine bootstrap.
 *
 * Wires the hash router and the staged-recipient counter. No configuration,
 * no secrets, no mock data: every station shown comes from the backend. */

import { Basket } from "./basket.js";
import { api } from "./api.js";
import { startRouter, outreachHref } from "./router.js";
import { renderAccountArea } from "./views/account.js";
import { renderDashboardView } from "./views/dashboard.js";
import { renderDJProfileView } from "./views/djProfile.js";
import { renderDJsView } from "./views/djs.js";
import { renderListView } from "./views/list.js";
import { renderOpportunitiesView } from "./views/opportunities.js";
import { renderOutreachView } from "./views/outreach.js";
import { renderOutreachHistoryView } from "./views/outreachHistory.js";
import { renderStationView, teardownStationView } from "./views/station.js";
import { renderTracksView } from "./views/tracks.js";

const viewRoot = document.getElementById("view");
const basketCount = document.getElementById("basket-count");
const accountArea = document.getElementById("account-area");

const basket = new Basket(window.sessionStorage);

function renderHeader(items) {
  basketCount.textContent = `selected: ${items.length}`;
}

basket.subscribe(renderHeader);

// Adopt the session from a returning Google OAuth redirect (the callback
// lands on "/?oauth=connected[&auth=<token>]"). This stores the token and
// strips it from the address bar before the router reads the hash, then
// sends the operator back to the outreach composer.
const oauthResult = api.adoptAuthFromUrl();
if (oauthResult.oauth && !window.location.hash) {
  window.location.hash = outreachHref;
}

// The header account area is always live: it reflects the session just
// adopted from an OAuth return (or the absence of one) via /auth/me.
renderAccountArea(accountArea);

startRouter(viewRoot, {
  dashboard(root) {
    teardownStationView();
    renderDashboardView(root);
  },
  list(root) {
    teardownStationView();
    renderListView(root);
  },
  station(root, identityKey) {
    renderStationView(root, identityKey, basket);
  },
  djs(root) {
    teardownStationView();
    renderDJsView(root);
  },
  dj(root, djId) {
    renderDJProfileView(root, djId, basket);
  },
  tracks(root) {
    teardownStationView();
    renderTracksView(root);
  },
  opportunities(root, trackId) {
    teardownStationView();
    renderOpportunitiesView(root, trackId);
  },
  outreach(root) {
    teardownStationView();
    renderOutreachView(root);
  },
  outreachHistory(root) {
    teardownStationView();
    renderOutreachHistoryView(root);
  },
});