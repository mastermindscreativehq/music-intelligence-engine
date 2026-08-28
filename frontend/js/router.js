/* Hash router: "#/" -> list view, "#/station/<key>" -> station view,
 * "#/tracks" -> submission assets view (Phase 8).
 * Identity keys contain ":" so they are percent-encoded in the hash and
 * decoded here before reaching the API client. */

export function startRouter(root, routes) {
  function render() {
    const hash = location.hash || "#/";
    const match = hash.match(/^#\/station\/(.+)$/);
    window.scrollTo(0, 0);
    root.replaceChildren();
    if (match) {
      routes.station(root, decodeURIComponent(match[1]));
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
