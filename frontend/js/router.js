/* Hash router: "#/" -> list view, "#/station/<key>" -> station view,
 * "#/tracks" -> submission assets view (Phase 8), "#/outreach" -> outreach
 * composer (recipient selection passed as ?recipient=<uid>,<uid>).
 * Identity keys contain ":" so they are percent-encoded in the hash and
 * decoded here before reaching the API client. */

export function startRouter(root, routes) {
  function render() {
    const hash = location.hash || "#/";
    const match = hash.match(/^#\/station\/(.+)$/);
    const outreach = hash.match(/^#\/outreach\?(.*)$/);
    window.scrollTo(0, 0);
    root.replaceChildren();
    if (match) {
      routes.station(root, decodeURIComponent(match[1]));
    } else if (outreach) {
      const params = new URLSearchParams(outreach[1]);
      const uids = (params.get("recipient") || "").split(",")
        .map((u) => decodeURIComponent(u)).filter(Boolean);
      routes.outreach(root, uids);
    } else if (hash === "#/tracks") {
      routes.tracks(root);
    } else {
      routes.list(root);
    }
  }
  window.addEventListener("hashchange", render);
  render();
}

export const stationHref = (identityKey) =>
  `#/station/${encodeURIComponent(identityKey)}`;

export const tracksHref = "#/tracks";

export const outreachHref = (contactUids) =>
  `#/outreach?recipient=${(contactUids || []).map(encodeURIComponent).join(",")}`;
