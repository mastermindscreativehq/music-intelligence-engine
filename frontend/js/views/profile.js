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
  }).catch((error) => {
    account.replaceChildren(
      el("h2", {}, "Account"),
      el("p", { class: "banner-error", role: "alert" },
        `Could not load your account: ${errorText(error)}`));
  });

  // The provider list is whatever the server says it can really send
  // through — never a hardcoded roster, so an unsupported provider is
  // simply not rendered (and can never be shown as connected).
  api.emailProviders().then((data) => {
    renderServicesSection(services, (data && data.providers) || [], reload);
  }).catch((error) => {
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

function capabilityText(provider) {
  // Plain-language summary of the provider's TRUE capability flags as
  // reported by the backend — nothing is inferred or promised here.
  const caps = provider.capabilities || {};
  const bits = [];
  if (caps.send) {
    bits.push(provider.id === "mailto"
      ? "opens your own mail app"
      : "can send outreach from this service");
  }
  if (caps.attach) bits.push("supports attachments");
  if (caps.status) bits.push("reports delivery status");
  if (caps.errors) bits.push("reports send failures");
  if (!bits.length) bits.push("no send capability");
  return bits.join("; ") + ".";
}

function providerRow(provider, reload) {
  const row = el("div", { class: "card" });
  const status = el("span", { class: "dim" });
  const connected = Boolean(provider.connected);
  const email = provider.email || "";

  const badge = connected
    ? el("span", { class: "badge badge-accent" }, "Connected")
    : (provider.connected === null
      ? el("span", { class: "badge badge-muted" }, "Always available")
      : el("span", { class: "badge badge-muted" }, "Not connected"));

  const actions = el("div", { class: "actions-row" });
  if (provider.connect_path || provider.disconnect_path) {
    const button = el("button", {
      class: connected ? "subtle" : "primary",
      onClick: async () => {
        button.disabled = true;
        if (connected) {
          status.replaceChildren("Disconnecting…");
          try {
            await api.providerDisconnect(provider.disconnect_path);
            refreshAccountArea();
            reload();
          } catch (error) {
            status.replaceChildren(el("span", {
              class: "banner-error", role: "alert",
            }, `Could not disconnect: ${errorText(error)}`));
            button.disabled = false;
          }
          return;
        }
        status.replaceChildren("Connecting…");
        try {
          const res = await api.providerConnect(provider.connect_path);
          if (res && res.auth_url) {
            window.location.assign(res.auth_url);
            return;
          }
          throw new Error("no authorization URL returned");
        } catch (error) {
          status.replaceChildren(el("span", {
            class: "banner-error", role: "alert",
          }, `Could not start connect: ${errorText(error)}`));
          button.disabled = false;
        }
      },
    }, connected ? `Disconnect ${provider.label}` : `Connect ${provider.label}`);
    actions.append(button);
  } else if (provider.auth_required === false) {
    actions.append(el("span", { class: "dim" },
      "No connection step: use this option from the outreach composer."));
  }
  actions.append(status);

  row.append(
    el("div", { class: "kv" },
      el("dt", {}, provider.label),
      el("dd", {},
        connected
          ? el("span", {},
            badge, " ", el("span", {}, email))
          : badge)),
    el("p", { class: "dim" }, capabilityText(provider)),
    provider.id === "gmail"
      ? el("p", { class: "dim" },
        connected
          ? "Outreach can be sent from this mailbox."
          : "Connecting grants the Gmail send scope only — never your "
            + "sign-in.")
      : null,
    actions);
  return row;
}

function renderServicesSection(host, providers, reload) {
  // Driven entirely by GET /email-providers: a provider the server does
  // not offer is never rendered, so nothing unsupported can appear as
  // connected or available.
  host.replaceChildren(
    el("h2", {}, "Connected services"),
    el("p", { class: "dim" },
      "Outreach can be sent through the services listed here. Each one "
      + "shows only the capabilities it really supports."),
    providers.length
      ? el("div", {},
        ...providers.map((provider) => providerRow(provider, reload)))
      : el("p", { class: "banner-error", role: "alert" },
        "No email providers are configured for this deployment."));
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
