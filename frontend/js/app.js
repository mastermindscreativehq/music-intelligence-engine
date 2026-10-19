/* Music Intelligence Engine bootstrap.
 *
 * Wires the hash router and the staged-recipient counter. No configuration,
 * no secrets, no mock data: every station shown comes from the backend. */

import { Basket } from "./basket.js";
import { api } from "./api.js";
import { startRouter, dashboardHref } from "./router.js";
import { renderAccountArea } from "./views/account.js";
import { renderDashboardView } from "./views/dashboard.js";
import { renderDJProfileView } from "./views/djProfile.js";
import { renderDJsView } from "./views/djs.js";
import { renderListView } from "./views/list.js";
import { renderOpportunitiesView } from "./views/opportunities.js";
import { renderOutreachView } from "./views/outreach.js";
import { renderOutreachHistoryView } from "./views/outreachHistory.js";
import { renderProfileView } from "./views/profile.js";
import { requiresAuth } from "./views/gate.js";
import { renderStationView, teardownStationView } from "./views/station.js";
import { renderTracksView } from "./views/tracks.js";

const viewRoot = document.getElementById("view");
const basketCount = document.getElementById("basket-count");
const accountArea = document.getElementById("account-area");

// Personal-workspace chrome (My music / Opportunities / Outreach / Profile and
// the selected-recipient badge) is hidden for signed-out visitors and revealed
// once a session exists. The class is applied in index.html so anonymous
// visitors never see a flash of personal links before JS runs. This is
// presentation only — the backend authorises every personal request.
const authOnlyNodes = Array.from(document.querySelectorAll("[data-auth]"));

function syncChrome() {
  const authed = api.isLoggedIn();
  for (const node of authOnlyNodes) {
    node.classList.toggle("nav-hidden", !authed);
  }
}

const basket = new Basket(window.sessionStorage);

function renderHeader(items) {
  basketCount.textContent = `selected: ${items.length}`;
}

basket.subscribe(renderHeader);

// Adopt the session from a returning Google OAuth redirect (the callback
// lands on "/?oauth=connected[&auth=<token>]" on success, or
// "/?oauth=error&reason=<code>" on a cancelled/failed hop). This stores the
// token and strips the OAuth params from the address bar before the router
// reads the hash; a successful sign-in then lands on the dashboard, while a
// failure renders a clear message in the header instead of a stuck screen.
const oauthResult = api.adoptAuthFromUrl();
if (oauthResult.adopted && !window.location.hash) {
  window.location.hash = dashboardHref;
}
const oauthNotice = oauthResult.oauth === "error"
  ? oauthErrorMessage(oauthResult.reason)
  : "";

// The header account area is always live: it reflects the session just
// adopted from an OAuth return (or the absence of one) via /auth/me.
renderAccountArea(accountArea, { notice: oauthNotice });

// Keep the personal-workspace chrome in step with the session. Account actions
// (sign out, disconnect) announce "mie:auth-changed"; a hashchange also
// re-syncs in case the token was cleared elsewhere.
syncChrome();
window.addEventListener("hashchange", syncChrome);
window.addEventListener("mie:auth-changed", () => {
  syncChrome();
  window.dispatchEvent(new HashChangeEvent("hashchange"));
});

function oauthErrorMessage(reason) {
  switch (reason) {
    case "oauth_denied":
      return "Google sign-in was cancelled or denied. Please try again.";
    case "oauth_state_invalid":
      return "That sign-in link expired or was already used. Please try again.";
    case "oauth_exchange_failed":
      return "Google could not complete the sign-in. Please try again.";
    case "oauth_not_configured":
      return "Google sign-in is not configured on the server. Please contact support.";
    case "oauth_email_missing":
      return "Google did not share an email address for this account.";
    default:
      return "Google sign-in could not be completed. Please try again.";
  }
}

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
    if (requiresAuth(root, "My music")) return;
    renderTracksView(root);
  },
  opportunities(root, trackId) {
    teardownStationView();
    if (requiresAuth(root, "Opportunities")) return;
    renderOpportunitiesView(root, trackId);
  },
  outreach(root) {
    teardownStationView();
    if (requiresAuth(root, "Outreach")) return;
    renderOutreachView(root);
  },
  outreachHistory(root) {
    teardownStationView();
    if (requiresAuth(root, "Outreach activity")) return;
    renderOutreachHistoryView(root);
  },
  profile(root) {
    teardownStationView();
    renderProfileView(root);
  },
});