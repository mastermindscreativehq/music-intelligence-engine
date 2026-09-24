"""Evidence-based geographic location extraction for stations.

Derives ``country`` / ``state_or_region`` / ``city`` ONLY from station
evidence: fetched page text, the homepage title, and the station's own
registrable domain. The discovery request / search query is NEVER a location
source — a station located in Nanaimo, BC will never be stored as "United
States" just because the search was scoped there.

Rules are deterministic and conservative; absence of evidence yields None:

- city + region are captured from explicit "City, REGION" tokens where
  REGION is a recognized US state or Canadian province abbreviation or full
  name, plus title-style "in CITY REGION" phrases;
- a region resolves to its country through the same map (Ohio -> United
  States, Nanaimo/BC -> Canada);
- the registrable-domain country-coded TLD (``.ca`` -> Canada) and the
  broadcast callsign prefix (``W``/``K`` -> United States, ``C`` -> Canada)
  corroborate country ONLY when no on-site text evidence contradicts them;
- every resolved field carries a provenance entry (source_url, source_type,
  method, matched text) for explainability.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from crawler.urls import canonical_domain

# ---------------------------------------------------------------------------
# Region maps (abbreviation -> full name), and region -> country.
# ---------------------------------------------------------------------------

_US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida",
    "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky",
    "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
    "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}

_CA_PROVINCES = {
    "BC": "British Columbia", "AB": "Alberta", "SK": "Saskatchewan",
    "MB": "Manitoba", "ON": "Ontario", "QC": "Quebec", "NS": "Nova Scotia",
    "NB": "New Brunswick", "PE": "Prince Edward Island",
    "NL": "Newfoundland and Labrador", "NT": "Northwest Territories",
    "YT": "Yukon", "NU": "Nunavut",
}

# abbreviation -> (full name, country). "CA" is California (US); the
# ambiguous province slot that would collide with it is deliberately absent.
_REGION_ABBR: dict[str, tuple[str, str]] = {
    abbr: (full, "United States") for abbr, full in _US_STATES.items()
}
_REGION_ABBR.update({
    abbr: (full, "Canada") for abbr, full in _CA_PROVINCES.items()
})

_FULL_TO_COUNTRY: dict[str, str] = {
    name: "United States" for name in _US_STATES.values()
}
_FULL_TO_COUNTRY.update({
    name: "Canada" for name in _CA_PROVINCES.values()
})

# ---------------------------------------------------------------------------
# Country evidence
# ---------------------------------------------------------------------------

# Country-name tokens that are unambiguous. Single-letter/word "us"/"uk"
# are deliberately absent: "send us your music" is everywhere on station
# sites and would fabricate a country.
_COUNTRY_TOKENS: dict[str, str] = {
    "united states of america": "United States",
    "united states": "United States",
    "u.s.a.": "United States",
    "u.s.a": "United States",
    "usa": "United States",
    "u.s.": "United States",
    "canada": "Canada",
    "united kingdom": "United Kingdom",
    "great britain": "United Kingdom",
    "england": "United Kingdom",
    "scotland": "United Kingdom",
    "wales": "United Kingdom",
    "australia": "Australia",
    "new zealand": "New Zealand",
    "ireland": "Ireland",
    "republic of ireland": "Ireland",
    "germany": "Germany",
    "france": "France",
    "spain": "Spain",
    "italy": "Italy",
    "netherlands": "Netherlands",
    "belgium": "Belgium",
    "switzerland": "Switzerland",
    "austria": "Austria",
    "portugal": "Portugal",
    "denmark": "Denmark",
    "sweden": "Sweden",
    "norway": "Norway",
    "finland": "Finland",
    "iceland": "Iceland",
    "japan": "Japan",
    "south korea": "South Korea",
    "china": "China",
    "india": "India",
    "brazil": "Brazil",
    "mexico": "Mexico",
    "argentina": "Argentina",
    "south africa": "South Africa",
    "nigeria": "Nigeria",
    "ghana": "Ghana",
    "philippines": "Philippines",
    "thailand": "Thailand",
    "singapore": "Singapore",
    "israel": "Israel",
}

# Country-coded domain suffixes that reliably indicate a station's physical
# country for media organizations. TLDs used generically by radio (.fm, .am,
# .tv, .io, .co, .gg, .dj, .me, .cc, .ly, .to) are deliberately excluded.
_DOMAIN_SUFFIX_COUNTRY: dict[str, str] = {
    "ca": "Canada",
    "co.uk": "United Kingdom", "org.uk": "United Kingdom",
    "uk": "United Kingdom",
    "com.au": "Australia", "net.au": "Australia", "org.au": "Australia",
    "au": "Australia",
    "co.nz": "New Zealand", "net.nz": "New Zealand", "org.nz": "New Zealand",
    "nz": "New Zealand",
    "ie": "Ireland",
    "de": "Germany", "fr": "France", "es": "Spain", "it": "Italy",
    "nl": "Netherlands", "be": "Belgium", "ch": "Switzerland",
    "at": "Austria", "pt": "Portugal", "dk": "Denmark", "se": "Sweden",
    "no": "Norway", "fi": "Finland", "is": "Iceland",
    "jp": "Japan", "kr": "South Korea", "cn": "China", "tw": "Taiwan",
    "hk": "Hong Kong", "in": "India", "sg": "Singapore",
    "my": "Malaysia", "th": "Thailand", "ph": "Philippines",
    "id": "Indonesia", "vn": "Vietnam",
    "br": "Brazil", "mx": "Mexico", "ar": "Argentina", "cl": "Chile",
    "co": "Colombia", "pe": "Peru", "uy": "Uruguay",
    "za": "South Africa", "ng": "Nigeria", "ke": "Kenya", "gh": "Ghana",
    "eg": "Egypt", "ma": "Morocco", "il": "Israel", "ae": "United Arab Emirates",
    "sa": "Saudi Arabia", "tr": "Turkey", "ru": "Russia",
    "pl": "Poland", "cz": "Czechia", "ro": "Romania", "hu": "Hungary",
    "gr": "Greece", "ua": "Ukraine", "pk": "Pakistan", "bd": "Bangladesh",
}

# Callsign prefixes (broadcast licensing): W/K -> US, C -> Canada. Only
# scanned in the homepage title where a callsign actually appears
# ("CHLY 101.7FM", "WYSO 91.3").
_CALLSIGN_WITH_DIGIT = re.compile(
    r"\b(?:(?:[WK][A-Z]{2,4})|(?:C[A-Z]{2,4}))\b"
    r"(?=[\s\-/]*(?:\d{1,3}(?:\.\d{1,2})?\s?(?:fm|am|khz|mhz)|"
    r"\d{2}\.\d{1,2}))",
    re.I,
)

# ---------------------------------------------------------------------------
# City / region token extraction
# ---------------------------------------------------------------------------

_ABBR_ALTERNATION = "|".join(
    sorted(_REGION_ABBR, key=len, reverse=True))
_FULL_ALTERNATION = "|".join(
    sorted(_FULL_TO_COUNTRY, key=len, reverse=True))

# "City, ABBR" / "City, Full Name" in prose. The leading capture holds the
# whole multi-word city caption; the region alternations are groups 2/3.
_CITY_REGION_PAGE = re.compile(
    r"\b([A-Z][A-Za-z.'\-]*(?:\s+[A-Z][A-Za-z.'\-]+){0,3})\s*,\s*"
    rf"(?:({_ABBR_ALTERNATION})|({_FULL_ALTERNATION}))\b",
)
# Title-style "in Nanaimo BC", "of Springfield IL" (no comma).
_CITY_REGION_TITLE = re.compile(
    rf"\b(?:in|of|from|near|based in|located in)\s+"
    rf"([A-Z][A-Za-z.'\-]*(?:\s+[A-Z][A-Za-z.'\-]+){{0,2}})\s+"
    rf"({_ABBR_ALTERNATION})\b",
)
# A bare full region name standing alone ("broadcasting across Ohio").
_REGION_FULL_TOKEN = re.compile(
    rf"\b(?:{_FULL_ALTERNATION})\b",
)

# Lead tokens that can never start a city caption in the page pattern (an
# unwelcome noun adjacent to "City, ST" list).
_CITY_LEAD_STOP = {
    "broadcasting", "based", "located", "situated", "serving", "welcome",
    "listen", "city", "counties", "st", "street", "pobox", "po",
}


# Street/suite address tokens that mark a city-caption as part of a mailing
# address. A non-first token ending in one of these (e.g. "Cedar St.") means
# the real CITY is what follows the address marker; "St." leading a caption is
# Saint (a city, e.g. "St. Paul"), never a street.
_STREET_SUFFIX = {
    "st", "street", "ave", "avenue", "blvd", "boulevard", "rd", "road",
    "dr", "drive", "ln", "lane", "ct", "court", "pkwy", "parkway", "way",
    "pl", "place", "ter", "terrace", "cir", "circle", "hwy", "highway",
    "ste", "suite", "fl", "floor", "box", "pob",
}


def _city_from_caption(caption: str) -> str | None:
    """Strip a leading street address from a city caption.

    "Cedar St. St. Paul" -> "St. Paul"; "Yellow Springs" -> "Yellow Springs";
    "St. Paul" (Saint) stays intact. Returns None when the caption names only
    an address, never a place.
    """
    words = re.findall(r"[A-Za-z0-9.'\-]+", caption)
    if not words:
        return None
    for i, word in enumerate(words[1:], start=1):
        core = re.sub(r"[^a-z]", "", word.lower())
        if core in _STREET_SUFFIX:
            tail = words[i + 1:]
            if not tail:
                return None   # caption ends at the address marker
            return " ".join(tail)
    return caption


def _looks_like_city_caption(candidate: str) -> bool:
    words = candidate.split()
    # 1..4 capitalized words; first token must not be a verb/stop caption.
    if not words or len(words) > 4:
        return False
    first = words[0].lower()
    if first in _CITY_LEAD_STOP:
        return False
    return all(word[:1].isupper() for word in words)


def _tld_country(url: str) -> str | None:
    """Country implied by a country-coded registrable-domain suffix."""
    if not url:
        return None
    try:
        domain = canonical_domain(url)
    except (ValueError, TypeError):
        return None
    host = urlsplit("https://" + domain).hostname or ""
    labels = [label for label in host.split(".") if label]
    suffix = ".".join(labels[-2:]).lower()
    if suffix in _DOMAIN_SUFFIX_COUNTRY:
        return _DOMAIN_SUFFIX_COUNTRY[suffix]
    if labels:
        return _DOMAIN_SUFFIX_COUNTRY.get(labels[-1].lower())
    return None


@dataclass
class LocationExtraction:
    """Evidence-based location for one station; None = not verifiable."""

    country: str | None = None
    state_or_region: str | None = None
    city: str | None = None
    evidence: list[dict] = field(default_factory=list)

    def has_location(self) -> bool:
        return bool(self.country or self.state_or_region or self.city)


def _evidence(field: str, value: str, source_url: str, method: str,
              matched_text: str, source_type: str = "official_website_page") -> dict:
    return {
        "value": value,
        "field": field,
        "source_url": source_url,
        "source_type": source_type,
        "method": method,
        "matched_text": matched_text,
        "discovered_at": "",
    }


def _scan_city_region(title: str, texts: list[tuple[str, str]]) -> list[dict]:
    """Find explicit City-Region tokens; return (field, value, source, matched).

    ``texts`` is a list of ``(source_url, page_text)`` pairs in fetch order.
    Returns a list of evidence entries for every distinct resolved location
    token (city, region, country) so provenance is fully preserved.
    """
    found: list[dict] = []

    def record_page(match: re.Match, source_url: str) -> None:
        caption = match.group(1).strip()
        region = match.group(2).strip() if match.group(2) else match.group(3).strip()
        if not _looks_like_city_caption(caption):
            return
        city = _city_from_caption(caption)
        if city is None:
            return
        full, country = _REGION_ABBR.get(region.upper(), (None, None))
        if full is None:
            full = next((name for name in _FULL_TO_COUNTRY
                         if name.lower() == region.lower()), None)
            country = _FULL_TO_COUNTRY.get(full or "")
            region_out = full or region
        else:
            region_out = full
        if country:
            found.append(_evidence(
                "country", country, source_url, "region_rule",
                f"{city}, {region}"))
        if region_out:
            found.append(_evidence(
                "state_or_region", region_out, source_url, "region_rule",
                f"{city}, {region}"))
        found.append(_evidence(
            "city", city, source_url, "region_rule", f"{city}, {region}"))

    def record_title(match: re.Match, source_url: str) -> None:
        city, region = match.group(1).strip(), match.group(2).strip()
        if not _looks_like_city_caption(city):
            return
        full, country = _REGION_ABBR.get(region.upper(), (None, None))
        if country is None:
            return
        found.append(_evidence(
            "country", country, source_url, "title_rule",
            f"in {city} {region}"))
        found.append(_evidence(
            "state_or_region", full, source_url, "title_rule",
            f"in {city} {region}"))
        found.append(_evidence(
            "city", city, source_url, "title_rule", f"in {city} {region}"))

    if title:
        for match in _CITY_REGION_TITLE.finditer(title):
            record_title(match, "")
    for source_url, text in texts:
        if not text:
            continue
        for match in _CITY_REGION_PAGE.finditer(text):
            record_page(match, source_url)
    return found


def _scan_region_tokens(title: str, texts: list[tuple[str, str]]) -> list[dict]:
    """Standalone full region names (region + country only, no city)."""
    found: list[dict] = []

    def record(text: str, source_url: str, method: str) -> None:
        for match in _REGION_FULL_TOKEN.finditer(text):
            name = match.group(0)
            country = _FULL_TO_COUNTRY.get(name)
            if not country:
                continue
            found.append(_evidence(
                "country", country, source_url, method, name))
            found.append(_evidence(
                "state_or_region", name, source_url, method, name))
            break   # first region token decides a single locality

    if title:
        record(title, "", "title_rule")
    for source_url, text in texts:
        record(text, source_url, "region_token_rule")
    return found


_COUNTRY_ALT = "|".join(sorted(_COUNTRY_TOKENS, key=len, reverse=True))

# "located in Canada", "in Australia", "from the United States" — anchored to
# governing prepositions so "Send us your music" never registers a country.
_FROM_COUNTRY = re.compile(
    rf"\b(?:located in|based in|serving|in|from)\s+(?:the\s+|a\s+)?"
    rf"({_COUNTRY_ALT})\b",
    re.I,
)
# "Tokyo, Japan" / "Vancouver, B.C., Canada" — comma-prefixed standalones.
_COMMA_COUNTRY = re.compile(rf",\s*(?:the\s+)?({_COUNTRY_ALT})\b", re.I)


def _scan_country_tokens(title: str, texts: list[tuple[str, str]]) -> list[dict]:
    """Country names in explicit prepositional or comma contexts."""
    found: list[dict] = []

    def record(text: str, source_url: str, method: str) -> None:
        for match in _FROM_COUNTRY.finditer(text):
            country = _COUNTRY_TOKENS.get(match.group(1).lower(), match.group(1))
            found.append(_evidence(
                "country", country, source_url, method, match.group(0)))
            break
        if not found or found[-1]["source_url"] != source_url:
            for match in _COMMA_COUNTRY.finditer(text):
                country = _COUNTRY_TOKENS.get(match.group(1).lower(), match.group(1))
                found.append(_evidence(
                    "country", country, source_url, method, match.group(0)))
                break

    if title:
        record(title, "", "country_token_rule")
    for source_url, text in texts:
        record(text, source_url, "country_token_rule")
    return found


def extract_location(
    *,
    url: str | None,
    title: str = "",
    pages: list | None = None,
) -> LocationExtraction:
    """Evidence-based location for a station's fetched pages.

    ``pages`` are parsed pages (``crawler.pages.ParsedPage``) in fetch order;
    each contributes ``.url``, ``.text`` and ``.title``. ``url`` is the
    station's homepage URL (drives the domain suffix evidence).
    """
    result = LocationExtraction()

    texts: list[tuple[str, str]] = []
    if pages:
        for page in pages:
            text = (getattr(page, "text", None) or "").strip()
            if text:
                texts.append((getattr(page, "url", "") or url or "", text))

    title = (title or "").strip()
    source_for_title = url or ""
    if title and texts:
        source_for_title = texts[0][0]

    candidates = _scan_city_region(title, texts)

    # Group by field in a stable order: city/region from structured tokens
    # dominate; later standalone region tokens only fill gaps.
    for field_name in ("city", "state_or_region", "country"):
        for entry in candidates:
            if entry["field"] != field_name:
                continue
            prior = getattr(result, field_name)
            if prior:
                continue
            # Title-derived evidence anchors to the homepage; the empty
            # source is resolved after texts are known.
            src = entry["source_url"] or source_for_title
            if src:
                entry["source_url"] = src
            setattr(result, field_name, entry["value"])
            result.evidence.append(entry)

    if result.state_or_region is None or result.country is None:
        for entry in _scan_region_tokens(title, texts):
            field_name = entry["field"]
            if getattr(result, field_name) is None and \
                    entry["value"] not in {
                        getattr(result, f) for f in ("city", "state_or_region")}:
                src = entry["source_url"] or source_for_title
                if src:
                    entry["source_url"] = src
                setattr(result, field_name, entry["value"])
                result.evidence.append(entry)

    # Country corroboration from explicit country-name tokens, domain TLD and
    # broadcast callsign (explicit text evidence wins over all of these).
    if result.country is None:
        for entry in _scan_country_tokens(title, texts):
            src = entry["source_url"] or source_for_title
            if src:
                entry["source_url"] = src
            result.country = entry["value"]
            result.evidence.append(entry)
            break

    text_country = result.country
    tld_country = _tld_country(url) if url else None
    if text_country is None and tld_country is not None:
        result.country = tld_country
        result.evidence.append(_evidence(
            "country", tld_country, url or "", "domain_tld_rule",
            f"domain suffix .{tld_country.lower()}"))

    if result.country is None and title:
        match = _CALLSIGN_WITH_DIGIT.search(title)
        if match:
            prefix = match.group(0)[0].upper()
            country = "United States" if prefix in ("W", "K") else "Canada"
            result.country = country
            result.evidence.append(_evidence(
                "country", country, url or "", "callsign_rule",
                match.group(0)))

    return result