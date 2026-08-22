"""Station characteristic enrichment: genres, formats, market area.

Deterministic keyword evidence over collected page text. Absence of
evidence yields empty lists / None — never guesses. Counts are returned as
evidence so downstream consumers can explain every classification.
"""

import re


def _normalize(texts: list[str]) -> str:
    """Collapse all whitespace so HTML line-wraps can't split key phrases."""
    return re.sub(r"\s+", " ",
                  "\n".join(t for t in texts if isinstance(t, str))).lower()


# Genre keyword → canonical genre token.
GENRE_KEYWORDS: dict[str, list[str]] = {
    "afrobeat": ["afrobeat", "afrobeats"],
    "amapiano": ["amapiano"],
    "hip_hop": ["hip-hop", "hip hop", "rap"],
    "r_and_b": ["r&b", "rhythm and blues", "rnb"],
    "jazz": ["jazz"],
    "blues": ["blues"],
    "country": ["country music", "country western"],
    "classical": ["classical music", "classical"],
    "rock": ["rock music", "classic rock", "indie rock"],
    "pop": ["pop music", "top 40", "chr"],
    "reggae": ["reggae"],
    "dancehall": ["dancehall"],
    "gospel": ["gospel"],
    "electronic": ["electronic music", "edm", "dance music", "house music",
                   "techno"],
    "latin": ["latin music", "salsa", "cumbia", "reggaeton"],
    "folk": ["folk music", "americana", "bluegrass"],
    "metal": ["metal"],
    "punk": ["punk"],
    "soul": ["soul music", "motown"],
    "funk": ["funk"],
    "world": ["world music", "global sounds"],
    "indie": ["indie music", "independent artists"],
}

# Format cues (what the station broadcasts, not which music).
FORMAT_KEYWORDS: dict[str, list[str]] = {
    "music": ["music radio", "music programming", "playing music",
              "music format"],
    "talk": ["talk radio", "talk show", "talk programming", "discussion"],
    "news": ["news radio", "news and information", "local news",
             "news talk"],
    "sports": ["sports radio", "sports talk", "live sports"],
    "variety": ["variety of music", "eclectic", "freeform"],
}

# Market/area claims are captured ONLY from explicit "serving ..." sentences.
_MARKET_RE = re.compile(
    r"serv(?:ing|es)\s+(?:the\s+)?([A-Za-z][A-Za-z ]{2,40}?)\s+"
    r"(area|region|market|community|metro)\b",
    re.I,
)


def detect_genres(texts: list[str], max_genres: int = 8) -> tuple[list[str], dict]:
    """Return (genres sorted by evidence count desc, {genre: [keywords hit]})."""
    combined = _normalize(texts)
    counts: dict[str, int] = {}
    evidence: dict[str, list[str]] = {}
    for genre, keywords in GENRE_KEYWORDS.items():
        hits: list[str] = []
        for kw in keywords:
            found = combined.count(kw)
            hits.extend([kw] * min(found, 3))
        if hits:
            counts[genre] = len(hits)
            evidence[genre] = sorted(set(hits))
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [genre for genre, _ in ranked[:max_genres]], evidence


def detect_formats(texts: list[str]) -> tuple[list[str], dict]:
    """Return (formats by evidence count desc, {format: keywords})."""
    combined = _normalize(texts)
    counts: dict[str, int] = {}
    evidence: dict[str, list[str]] = {}
    for fmt, keywords in FORMAT_KEYWORDS.items():
        hits: list[str] = []
        for kw in keywords:
            found = combined.count(kw)
            hits.extend([kw] * min(found, 3))
        if hits:
            counts[fmt] = len(hits)
            evidence[fmt] = sorted(set(hits))
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [fmt for fmt, _ in ranked[:4]], evidence


def extract_market(texts: list[str]) -> str | None:
    """Market/area string only when explicitly claimed ('serving the ... area').

    Returns None otherwise — a missing market stays unknown.
    """
    for text in texts or []:
        if not isinstance(text, str):
            continue
        match = _MARKET_RE.search(text)
        if match:
            area = " ".join(match.group(1).split())
            return area.title()
    return None
