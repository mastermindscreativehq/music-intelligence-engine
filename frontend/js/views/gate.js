/* Auth gate for operator-only views.
 *
 * Browsing the public directories (Stations, DJs and their pages) is open to
 * everyone. The personal workspace — My music, Opportunities, Outreach and
 * Profile — belongs to the signed-in operator, so every gated route renders
 * a clear "Sign in required" card instead of an empty or erroring page. The
 * gate is presentation only: the backend independently authorises every
 * personal request, so hiding the UI is never the security boundary.
 *
 * Nothing is invented: the sign-in button starts the real Google identity hop
 * (openid / email / profile only) and surfaces a typed error on failure.
 * Dynamic values become text, never markup (el() only; no innerHTML). */

import { api, ApiError } from "../api.js";
import { el } from "../dom.js";

// Returns true when the operator must sign in first (and renders the card).
export function requiresAuth(root, label) {
  if (api.isLoggedIn()) return false;
  root.append(signInCard(label));
  return true;
}

function signInCard(label) {
  const status = el("span", { class: "account-status" });
  const signIn = el("button", {
    class: "primary",
    onClick: async () => {
      signIn.disabled = true;
      status.replaceChildren("Opening Google…");
      try {
        const res = await api.googleStart();
        if (res && res.auth_url) {
          window.location.assign(res.auth_url);
          return;
        }
        throw new Error("no authorization URL returned");
      } catch (error) {
        const detail = error instanceof ApiError ? error.message : String(error);
        status.replaceChildren(
          el("span", { class: "banner-error", role: "alert" }, detail));
        signIn.disabled = false;
      }
    },
  }, "Sign in with Google");
  const what = label ? `${label} is` : "This part of the app is";
  return el("section", { class: "card" },
    el("h2", {}, "Sign in required"),
    el("p", {},
      `${what} part of your personal workspace. Sign in with Google to `
      + "continue."),
    el("div", { class: "actions-row" }, signIn, status),
    el("p", { class: "dim" },
      "Browsing stations and DJs stays open to everyone — no account needed."));
}
