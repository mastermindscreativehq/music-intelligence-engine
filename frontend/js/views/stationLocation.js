/* Shared station-location derivation — the ONE source of truth for how a
 * station's location is composed in the UI.
 *
 * Both the station list and the station detail page read the SAME existing
 * station fields (city, state_or_region, country, then market_area as a
 * fallback when no city/state/country exists). Nothing here is guessed or
 * hardcoded: it only joins values that already live on the station record.
 * The result is "" (falsy) when no location field has data, so each view
 * keeps its own presentation fallback ("—" in the list, null on the detail
 * page). */

export function stationLocation(station) {
  if (!station) return "";
  const parts = [station.city, station.state_or_region, station.country]
    .filter((value) =>
      value !== null && value !== undefined && String(value).trim() !== "");
  if (parts.length) return parts.join(", ");
  const area = String(station.market_area ?? "").trim();
  return area || "";
}