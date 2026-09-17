/* Hash router (stabilized 2026-09).
 *
 * Canonical navigation — every route is a real, named destination; nothing
 * silently redirects and no two routes compete for the same operation:
 *   #/                 dashboard (overview only, never operational)
 *   #/stations         station list (search + filters + open station)
 *   #/station/<key>    station page (PROFILE / SUBMISSION INFORMATION /
 *                      CONTACTS / OUTREACH) — the primary outreach intake
 *   #/djs              DJ list (search + filters + open DJ profile)
 *   #/dj/<id>          DJ profile (identity + channels + outreach)
 *   #/tracks           my music (releases to attach to outreach)
 *   #/opportunities    opportunity intelligence for a release (Phase 3)
 *   #/outreach         active outreach records (ready|sent|responded|follow_up)
 *   #/outreach-history full outreach ledger (all records + attempts)
 *
 * Identity keys contain ":" so they are percent-encoded in the hash and
 * decoded here before reaching the API client. */

export function startRouter(root, routes) {
  function render() {
    const hash = location.hash || "#/";
    const stationMatch = hash.match(/^#\/station\/(.+)$/);
    const djMatch = hash.match(/^#\/dj\/(.+)$/);
    const opportunityMatch = hash.match(/^#\/opportunities\/(.+)$/);
    window.scrollTo(0, 0);
    root.replaceChildren();
    if (stationMatch) {
      routes.station(root, decodeURIComponent(stationMatch[1]));
    } else if (djMatch) {
      routes.dj(root, decodeURIComponent(djMatch[1]));
    } else if (hash === "#/opportunities") {
      routes.opportunities(root, null);
    } else if (opportunityMatch) {
      routes.opportunities(root, decodeURIComponent(opportunityMatch[1]));
    } else if (hash === "#/stations") {
      routes.list(root);
    } else if (hash === "#/djs") {
      routes.djs(root);
    } else if (hash === "#/tracks") {
      routes.tracks(root);
    } else if (hash === "#/outreach") {
      routes.outreach(root);
    } else if (hash === "#/outreach-history") {
      routes.outreachHistory(root);
    } else {
      routes.dashboard(root);
    }
  }
  window.addEventListener("hashchange", render);
  render();
}

export const stationHref = (identityKey) =>
  `#/station/${encodeURIComponent(identityKey)}`;

export const stationsHref = "#/stations";

export const djsHref = "#/djs";

export const djHref = (djId) => `#/dj/${encodeURIComponent(djId)}`;

export const tracksHref = "#/tracks";

export const outreachHref = "#/outreach";

export const outreachHistoryHref = () => "#/outreach-history";

export const opportunitiesHref = "#/opportunities";

export const opportunitiesTrackHref = (trackId) =>
  `#/opportunities/${encodeURIComponent(trackId)}`;