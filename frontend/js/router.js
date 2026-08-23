/* Hash router: "#/" -> list view, "#/station/<key>" -> station view.
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
    } else {
      routes.list(root);
    }
  }
  window.addEventListener("hashchange", render);
  render();
}

export const stationHref = (identityKey) =>
  `#/station/${encodeURIComponent(identityKey)}`;
