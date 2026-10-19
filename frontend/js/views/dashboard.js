/* Dashboard (#/) — lightweight overview, never operational.
 *
 * Two audiences, one route:
 *   signed-out  -> the public landing page (what the app does + sign in)
 *   signed-in   -> a read-only overview of the operator's own workspace
 *
 * Every signed-in number comes from the real backend endpoints; nothing here
 * stages or changes anything. Operations happen on their dedicated pages
 * (Stations / My music / Outreach). */

import { api, ApiError } from "../api.js";
import { el } from "../dom.js";
import {
  djsHref,
  outreachHistoryHref,
  stationHref,
  stationsHref,
  tracksHref,
} from "../router.js";
import { statusChip } from "./outreachRecords.js";

function statTile(label, value, href) {
  const body = el("div", { class: "stat-body" },
    el("div", { class: "stat-value" }, String(value ?? "—")),
    el("div", { class: "dim" }, label));
  return href
    ? el("a", { class: "card stat-tile", href }, body)
    : el("div", { class: "card stat-tile" }, body);
}

function alertLine(text, href, actionLabel) {
  return el("div", { class: "alert-row" },
    el("span", {}, text),
    el("a", { class: "linkish", href }, actionLabel || "Go →"));
}

// Public landing page (signed out). Plain, honest description of what this
// deployment does, the two open directories, and the single real next step
// (sign in with Google). No marketing claims that aren't true of the product.
function signInButton(label) {
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
  }, label);
  return { signIn, status };
}

function featureCard(title, body) {
  return el("article", { class: "feature-card" },
    el("h3", {}, title),
    el("p", {}, body));
}

function landingPage() {
  const { signIn, status } = signInButton("Sign in with Google");
  return el("div", { class: "landing" },
    el("section", { class: "landing-hero" },
      el("p", { class: "landing-kicker" }, "Music promotion workspace"),
      el("h1", { class: "landing-title" },
        "Find the right radio stations and DJs for your music."),
      el("p", { class: "landing-lead" },
        "Music Intelligence Engine discovers radio stations and independent "
        + "DJs from the public web, shows the contact and submission routes it "
        + "can verify, and helps you prepare and track outreach that references "
        + "your own releases."),
      el("div", { class: "landing-cta" },
        signIn,
        el("a", { class: "buttonish", href: stationsHref }, "Browse stations"),
        el("a", { class: "buttonish", href: djsHref }, "Browse DJs")),
      status,
      el("p", { class: "landing-fine" },
        "Browsing stations and DJs is open to everyone. Signing in with "
        + "Google unlocks your releases, your outreach records, and sending "
        + "from your own connected mailbox.")),
    el("section", { class: "landing-features" },
      featureCard("Discover",
        "Search radio stations and independent DJs, with the contact and "
        + "submission routes each one actually publishes."),
      featureCard("Prepare",
        "Write outreach that references your release, its listening link and "
        + "its story. Nothing about your music is invented."),
      featureCard("Send & track", 
        "Send from your own connected mailbox and keep a record of every "
        + "attempt, response and follow-up.")),
    el("section", { class: "card landing-start" },
      el("h2", {}, "How it works"),
      el("ol", { class: "landing-steps" },
        el("li", {}, "Browse the public directories to find a fit."),
        el("li", {}, "Sign in and add your releases in My music."),
        el("li", {}, "Prepare outreach, then connect your mailbox to send.")),
      el("div", { class: "actions-row" },
        el("a", { class: "buttonish subtle", href: stationsHref },
          "Browse stations"),
        el("a", { class: "buttonish subtle", href: djsHref },
          "Browse DJs"))));
}

export function renderDashboardView(root) {
  if (!api.isLoggedIn()) {
    root.append(landingPage());
    return;
  }

  root.append(
    el("h1", {}, "Dashboard"),
    el("p", { class: "dim" },
      "What's happening across the engine. This is an overview only — ",
      "prepare and track outreach from its own pages."));

  const slots = new Map([
    ["reads", el("div", { class: "stat-grid" },
      el("p", { class: "dim" }, "Loading…"))],
    ["outlook", el("section", { class: "card" },
      el("h2", {}, "Outreach summary"),
      el("p", { class: "dim" }, "Loading…"))],
    ["recent", el("section", { class: "card" },
      el("h2", {}, "Recent activity"),
      el("p", { class: "dim" }, "Loading…"))],
    ["alerts", el("section", { class: "card" },
      el("h2", {}, "Needs attention"),
      el("p", { class: "dim" }, "Loading…"))],
  ]);

  root.append(
    slots.get("reads"),
    slots.get("outlook"),
    slots.get("recent"),
    slots.get("alerts"),
    el("section", { class: "card" },
      el("h2", {}, "Quick paths"),
      el("div", { class: "actions-row" },
        el("a", { class: "buttonish subtle", href: stationsHref },
          "Browse stations"),
        el("a", { class: "buttonish subtle", href: tracksHref },
          "My music"),
        el("a", { class: "buttonish subtle", href: outreachHistoryHref() },
          "Outreach activity log"))));

  Promise.all([
    api.stations({ limit: 1 }).catch(() => null),
    api.tracks({ limit: 1 }).catch(() => null),
    api.listOutreach({ limit: 100 }).catch(() => null),
  ]).then(([stationsData, tracksData, outreachData]) => {
    const stationTotal = stationsData && stationsData.total;
    const trackTotal = tracksData ? (tracksData.total ?? tracksData.tracks.length) : null;
    const records = outreachData && outreachData.outreach || [];

    slots.get("reads").replaceChildren(
      statTile("stations discovered", stationTotal, stationsHref),
      statTile("releases in my music", trackTotal, tracksHref),
      statTile("outreach records", records.length,
        outreachHistoryHref()));

    const counts = {};
    for (const status of ["ready", "sent", "responded", "follow_up", "failed", "closed"]) {
      counts[status] = 0;
    }
    for (const r of records) {
      if (r && r.status in counts) counts[r.status] += 1;
    }
    slots.get("outlook").replaceChildren(
      el("h2", {}, "Outreach summary"),
      el("div", { class: "chips" },
        [["ready", "prepared"], ["sent", "sent"], ["responded", "responded"],
         ["follow_up", "follow up"], ["failed", "failed"],
         ["closed", "closed"]]
          .map(([status, label]) =>
            el("span", { class: "chip" },
              el("strong", {}, String(counts[status])), " ", label))));

    slots.get("recent").replaceChildren(
      el("h2", {}, "Recent outreach"),
      records.slice(0, 6).length
        ? el("div", { class: "recent-list" },
          records.slice(0, 6).map((r) =>
            el("div", { class: "recent-row" },
              statusChip(r.status),
              el("span", {}, (r.recipient && r.recipient.organization)
                || (r.recipient && r.recipient.name) || r.outreach_id),
              r.track && r.track.original_filename
                ? el("span", { class: "dim" },
                  `· ${r.track.original_filename}`) : null,
              el("span", { class: "dim" },
                `updated ${new Date(r.updated_at || r.created_at).toLocaleString()}`))))
        : el("p", { class: "dim" }, "No outreach records yet."));

    const alerts = [];
    if (records.length && counts.ready > 0) {
      alerts.push(alertLine(
        `${counts.ready} prepared record${counts.ready === 1 ? "" : "s"} ` +
        "await hand-off.", outreachHistoryHref(), "Review →"));
    }
    if (records.length && counts.failed > 0) {
      alerts.push(alertLine(
        `${counts.failed} record${counts.failed === 1 ? "" : "s"} marked failed.`,
        outreachHistoryHref(), "Review →"));
    }
    if (stationTotal === 0 || stationTotal === null) {
      alerts.push(alertLine(
        "No stations discovered yet — run the discovery pipeline.",
        stationsHref, "Stations →"));
    }
    if (trackTotal === 0 || trackTotal === null) {
      alerts.push(alertLine(
        "No releases in My music yet — upload an MP3 to attach to outreach.",
        tracksHref, "My music →"));
    }
    slots.get("alerts").replaceChildren(
      el("h2", {}, "Needs attention"),
      alerts.length
        ? el("div", { class: "alert-list" }, alerts)
        : el("p", { class: "dim" }, "Nothing needs attention right now."));
  }).catch((error) => {
    const detail = error instanceof ApiError ? error.message : String(error);
    slots.get("reads").replaceChildren(
      el("p", { class: "banner-error", role: "alert" },
        `Could not load dashboard data: ${detail}`));
  });
}