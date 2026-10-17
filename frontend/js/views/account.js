/* Account area in the header: sign in with Google, show the connected
 * Google account, and disconnect Gmail.
 *
 * There are TWO separate Google hops, deliberately never bundled:
 *   1. Sign in  - identity only (openid/email/profile). Mints the session.
 *   2. Connect Gmail - a signed-in operator grants the Gmail SEND scope.
 * This module never stores credentials itself: it reads /api/v1/auth/me
 * (the bearer token lives in localStorage, attached by api.js) and renders
 * one of three honest states:
 *   signed out        -> "Sign in with Google"
 *   signed in, no Gmail-> account email + "Connect Gmail" + "Sign out"
 *   Gmail connected    -> account email + "Disconnect Gmail" + "Sign out"
 *
 * Disconnecting removes only the stored Google credentials — the operator
 * stays signed in.
 *
 * Every node is built with el(); dynamic values become text, never markup
 * (CSP-friendly, no inline handlers, no innerHTML). */

import { api } from "../api.js";
import { el } from "../dom.js";

let host = null;
let lastError = null;

export function renderAccountArea(target, options = {}) {
  host = target;
  // An OAuth return may carry a failure reason (see api.adoptAuthFromUrl);
  // render it as the account-area notice so a cancelled/failed sign-in shows
  // a clear message instead of an empty ("stuck") header.
  lastError = options.notice ? String(options.notice) : null;
  return load();
}

export async function refreshAccountArea() {
  lastError = null;
  await load();
}

async function load() {
  if (!host) return;
  host.replaceChildren(el("span", { class: "account-email" }, "…"));
  // No stored session means /auth/me would 401 by design. Skip the call and
  // render the signed-out state directly so the console stays clean.
  if (!api.isLoggedIn()) {
    host.replaceChildren(...accountNodes(null));
    return;
  }
  let me = null;
  try {
    me = await api.me();
  } catch (error) {
    if (!isSignedOut(error)) lastError = errorText(error);
    me = null;
  }
  host.replaceChildren(...accountNodes(me));
}

function isSignedOut(error) {
  return Boolean(error) && (error.status === 401
    || error.code === "unauthorized");
}

function accountNodes(me) {
  const nodes = [];
  if (lastError) {
    nodes.push(el("span", { class: "account-status" }, lastError));
  }
  if (!me || !me.user) {
    nodes.push(el("button", { class: "primary", onClick: signIn },
      "Sign in with Google"));
    return nodes;
  }
  const email = me.gmail_connected && me.gmail_email
    ? me.gmail_email
    : me.user.email;
  nodes.push(el("span", {
    class: "account-email",
    title: me.gmail_connected ? "Gmail connected" : "signed in",
  }, email));
  if (me.gmail_connected) {
    nodes.push(el("span", { class: "badge badge-accent" },
      "Gmail connected"));
    nodes.push(actionButton("Disconnect Gmail", disconnectGmail));
  } else {
    nodes.push(actionButton("Connect Gmail", connectGmail));
  }
  nodes.push(actionButton("Sign out", signOut, "subtle"));
  return nodes;
}

function actionButton(label, onClick, extraClass) {
  return el("button", {
    class: extraClass ? `account-btn ${extraClass}` : "account-btn",
    onClick,
  }, label);
}

async function startGoogleFlow(fetcher) {
  try {
    const res = await fetcher();
    if (res && res.auth_url) {
      window.location.assign(res.auth_url);
      return;
    }
    throw new Error("no authorization URL returned");
  } catch (error) {
    showError(error);
  }
}

async function signIn() {
  return startGoogleFlow(() => api.googleStart());
}

async function connectGmail() {
  return startGoogleFlow(() => api.googleConnect());
}

async function disconnectGmail() {
  try {
    await api.disconnectGmail();
  } catch (error) {
    showError(error);
    return;
  }
  await refreshAccountArea();
}

async function signOut() {
  try {
    await api.logout();
  } catch (error) {
    /* the local token is cleared even when the revoke call fails */
  }
  await refreshAccountArea();
}

function showError(error) {
  lastError = errorText(error);
  load();
}

function errorText(error) {
  const message = error && error.message ? String(error.message) : "";
  return message || "account action failed";
}
