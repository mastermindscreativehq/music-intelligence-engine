/* Profile (#/profile) — account, connected services, and plan.
 *
 * Three honest sections, all backed by the real API:
 *   Account             — the signed-in email (read-only identity) and an
 *                         editable display name (PATCH /auth/profile).
 *   Connected services  — whether Gmail is connected, with the same
 *                         connect/disconnect hops as the header account area.
 *   Plan                — the deployment's actual entitlements from
 *                         GET /plan. This build has NO billing and NO paid
 *                         tiers, so the section states that plainly rather
 *                         than showing a fake upgrade button.
 *
 * Nothing is invented: an unreachable or failing endpoint surfaces as a
 * typed error, never a placeholder value. Dynamic data becomes text, never
 * markup (el() only; no innerHTML, no inline handlers). */

import { api, ApiError } from "../api.js";
import { el } from "../dom.js";
import { refreshAccountArea } from "./account.js";

function errorText(error) {
  if (error instanceof ApiError || (error && error.message)) {
    return String(error.message);
  }
  return String(error);
}

export function renderProfileView(root) {
  root.append(
    el("h1", {}, "Profile"),
    el("p", { class: "dim" },
      "Your account, connected services, and what this deployment includes."));

  if (!api.isLoggedIn()) {
    root.append(el("section", { class: "card" },
      el("h2", {}, "Sign in required"),
      el("p", { class: "dim" },
        "Sign in with Google from the header to view and manage your profile.")));
    return;
  }

  const account = el("section", { class: "card" },
    el("h2", {}, "Account"), el("p", { class: "dim" }, "Loading…"));
  const services = el("section", { class: "card" },
    el("h2", {}, "Connected services"), el("p", { class: "dim" }, "Loading…"));
  const plan = el("section", { class: "card" },
    el("h2", {}, "Plan"), el("p", { class: "dim" }, "Loading…"));
  root.append(account, services, plan);

  const reload = () => {
    root.replaceChildren();
    renderProfileView(root);
  };

  api.me().then((me) => {
    renderAccountSection(account, me);
    renderServicesSection(services, me, reload);
  }).catch((error) => {
    account.replaceChildren(
      el("h2", {}, "Account"),
      el("p", { class: "banner-error", role: "alert" },
        `Could not load your account: ${errorText(error)}`));
    services.replaceChildren(
      el("h2", {}, "Connected services"),
      el("p", { class: "banner-error", role: "alert" },
        `Could not load connected services: ${errorText(error)}`));
  });

  api.plan().then((data) => {
    renderPlanSection(plan, data);
  }).catch((error) => {
    plan.replaceChildren(
      el("h2", {}, "Plan"),
      el("p", { class: "banner-error", role: "alert" },
        `Could not load plan information: ${errorText(error)}`));
  });
}

function renderAccountSection(host, me) {
  const user = (me && me.user) || {};
  const nameInput = el("input", {
    type: "text",
    value: user.name || "",
    placeholder: "Add a display name",
    maxlength: "120",
    autocomplete: "name",
  });
  const status = el("span", { class: "dim" });
  const save = el("button", {
    class: "primary",
    onClick: async () => {
      const name = nameInput.value.trim();
      save.disabled = true;
      status.replaceChildren("Saving…");
      try {
        const res = await api.updateProfile(name);
        const saved = (res && res.user) || {};
        if (typeof saved.name === "string") nameInput.value = saved.name;
        else if (saved.name === null) nameInput.value = "";
        status.replaceChildren(
          el("span", { class: "banner-info" }, "Saved."));
        refreshAccountArea();
      } catch (error) {
        status.replaceChildren(el("span", { class: "banner-error", role: "alert" },
          `Could not save: ${errorText(error)}`));
      } finally {
        save.disabled = false;
      }
    },
  }, "Save");

  host.replaceChildren(
    el("h2", {}, "Account"),
    el("dl", { class: "kv" },
      el("dt", {}, "Email"), el("dd", {}, user.email || "—"),
      el("dt", {}, "Sign-in"), el("dd", {}, "Google")),
    el("div", { class: "field" },
      el("span", {}, "Display name"),
      el("div", { class: "actions-row" }, nameInput, save),
      el("span", { class: "hint" },
        "Shown on your outreach. Your email is your sign-in identity and "
        + "can't be changed here.")),
    el("p", {}, status));
}

function renderServicesSection(host, me, reload) {
  const gmailConnected = Boolean(me && me.gmail_connected);
  const gmailEmail = (me && me.gmail_email) || (me && me.user && me.user.email);
  const status = el("span", { class: "dim" });

  const button = el("button", {
    class: gmailConnected ? "subtle" : "primary",
    onClick: async () => {
      if (gmailConnected) {
        button.disabled = true;
        status.replaceChildren("Disconnecting…");
        try {
          await api.disconnectGmail();
          refreshAccountArea();
          reload();
        } catch (error) {
          status.replaceChildren(el("span", { class: "banner-error", role: "alert" },
            `Could not disconnect: ${errorText(error)}`));
          button.disabled = false;
        }
        return;
      }
      button.disabled = true;
      try {
        const res = await api.googleConnect();
        if (res && res.auth_url) {
          window.location.assign(res.auth_url);
          return;
        }
        throw new Error("no authorization URL returned");
      } catch (error) {
        status.replaceChildren(el("span", { class: "banner-error", role: "alert" },
          `Could not start Gmail connect: ${errorText(error)}`));
        button.disabled = false;
      }
    },
  }, gmailConnected ? "Disconnect Gmail" : "Connect Gmail");

  host.replaceChildren(
    el("h2", {}, "Connected services"),
    el("div", { class: "kv" },
      el("dt", {}, "Gmail"),
      el("dd", {},
        gmailConnected
          ? el("span", {},
            el("span", { class: "badge badge-accent" }, "Connected"),
            " ",
            gmailEmail || "")
          : el("span", { class: "badge badge-muted" }, "Not connected"))),
    el("p", { class: "dim" },
      gmailConnected
        ? "Outreach can be sent from this mailbox."
        : "Connect Gmail to send outreach from your own mailbox. Connecting "
          + "grants the Gmail send scope only — never your sign-in."
    ),
    el("div", { class: "actions-row" }, button, status));
}

function renderPlanSection(host, data) {
  const plan = (data && data.plan) || {};
  host.replaceChildren(
    el("h2", {}, "Plan"),
    el("dl", { class: "kv" },
      el("dt", {}, "Plan"), el("dd", {}, plan.name || "—"),
      el("dt", {}, "Billing"),
      el("dd", {}, plan.billing_configured ? "Configured" : "Not configured")),
    el("p", { class: "note-honest" },
      plan.note
      || "Plan information is unavailable for this deployment."));
}
