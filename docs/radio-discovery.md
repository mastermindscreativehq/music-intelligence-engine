# Radio Discovery Engine — Phase 2 Architecture

Phase 2 implements the first working slice of the DISCOVER pipeline for
`radio_station` target types, on reusable machinery.

## 1. What exists after Phase 2

```
DiscoveryRequest ──► query generation ──► DiscoveryProvider ──► Candidates
                                                                    │
        ┌───────────────────────────────────────────────────────────┘
        ▼
URL normalization ──► bounded retrieval (robots-aware, rate-limited)
        │
        ▼
focused contact/submission-page discovery ──► public contact extraction
        │
        ▼
contact normalization + role classification ──► station classification
        │
        ▼
deduplication ──► confidence scoring ──► normalized StationRecord(s)
        │
        ▼
DiscoveryResult (records + failures + provenance)   [JSON out]
```

No LLM is required anywhere in this pipeline. All logic is deterministic.

## 2. Module map (where things live)

| Concern | Module | Notes |
|---|---|---|
| Request/result/candidate models, source types, provenance facts | `discovery/models.py` | generic, org-type agnostic |
| Deterministic search-query generation | `discovery/queries.py` | structured request → query strings |
| Provider abstraction + seed-file provider | `discovery/providers.py` | no credentials required |
| Event logging constants | `discovery/events.py` | structured log events |
| HTTP retrieval abstraction, rate limiting, robots.txt | `crawler/http.py` | stdlib `urllib` only |
| URL normalization / canonical domains | `crawler/urls.py` | shared by crawl + dedupe |
| Page parsing (links, text, mailto) | `crawler/pages.py` | stdlib `html.parser` |
| Focused priority-page discovery | `crawler/page_finder.py` | bounded, keyword-driven |
| Email extraction/normalization/quality | `enrichment/emails.py` | no obfuscation defeating |
| Contact-role classification | `enrichment/roles.py` | transparent keyword rules |
| Contact assembly/normalization | `enrichment/contacts.py` | conservative name heuristics |
| Station-type classification + socials | `enrichment/stations.py` | evidence-based |
| Deduplication & merging | `enrichment/dedupe.py` | domain-keyed |
| Confidence scoring | `enrichment/confidence.py` | additive reasons, capped 0–1 |
| Radio pipeline orchestration | `discovery/radio/pipeline.py` | composes stages |
| Normalized radio records | `discovery/radio/schema.py` | JSON-ready dataclasses |

Reusability contract: `discovery/models|queries|providers`, all of `crawler/*`,
and most of `enrichment/*` are organization-type agnostic. Only
`discovery/radio/*` is radio-specific. Future org types add their own
`discovery/<type>/` pipeline reusing these stages.

## 3. Discovery input

`DiscoveryRequest` (validated): `query`, optional `country`,
`state_or_region`, `city`, `station_type`, `genre`, `language`, `limit`
(1–500, default 50). Unknown keys rejected at construction → malformed input
fails fast instead of producing garbage.

## 4. Query generation

Deterministic composition of intent parts:
`[station_type] [genre] radio stations [city] [state_or_region] [country]`
plus, when `station_type` or genre implies submissions interest, a second
query variant `... accepting music submissions`. Fully covered by unit tests;
no hard-coded cities.

## 5. Provider abstraction

```python
class DiscoveryProvider(Protocol):
    def search(self, request: DiscoveryRequest,
               queries: Sequence[str]) -> list[Candidate]: ...
```

Implemented now:

- **SeedListProvider** — reads candidate station URLs/names from a local JSON
  seed file produced from legitimate public sources. This is the honest
  credential-free provider: no invented API results.

Documented extension point (not implemented): an HTTP search-API provider
behind the same Protocol once credentials exist. Tests use fixture-backed
providers only; nothing claims live search results it does not have.

Candidate fields: `title`, `url`, `snippet`, `source`, `source_type`,
`discovered_at`.

Source types: `official_source`, `directory_source`, `social_source`,
`search_source`, `other`.

## 6. Retrieval rules (`crawler/http.py`)

- stdlib `urllib.request`; **http/https schemes only** (rejected otherwise)
- configurable timeout (default 15 s), max response size (default 2 MB)
- content-type gate: `text/html` (or `text/plain`) required
- redirect behavior: urllib's default safe following within scheme; final URL recorded
- failures classified: `timeout`, `dns_error`, `http_status` (403/404/500…),
  `invalid_url`, `content_type`, `too_large`, `connection_error`, `ssl_error`
- robots.txt honored via `urllib.robotparser` (per-host cache; disable flag
  exists strictly for offline tests against local fixtures)
- politeness rate limit: minimum delay between requests to the same host
- bounded scope: ≤ `max_pages_per_site` pages per domain (default 6),
  depth ≤ 1 beyond the homepage; **no unrestricted spider**
- retries: none automatic in Phase 2 (failures are recorded, not retried);
  retry policy belongs to Phase 3 hardening

The crawler never evaluates trust, extracts intelligence, or calls any model.

## 7. Focused page discovery (`crawler/page_finder.py`)

For each station domain: fetch homepage, then prioritize existing well-known
paths and homepage-discovered links matching keywords:
`contact, contact-us, submissions, submit-music, music-submissions,
programming, program-director, dj, about, staff, team, advertise, shows,
schedule`. Selection is ranked by keyword weight, capped at the per-site page
budget. Off-site links are ignored.

## 8. Public contact extraction

From prioritized pages only:

- **emails** — literal text emails + `mailto:` hrefs; whitespace/case/
  punctuation normalized; obfuscation mechanisms are *not* defeated; no
  address is ever invented; every address keeps `{value, source_url,
  source_type, discovered_at}` provenance
- **phones** — tel: hrefs and conservative text patterns
- **contact names** — conservative adjacency heuristic around role keywords
  ("Music Director: Jane Smith", "Jane Smith, Music Director"); low-weight,
  marked rule-derived
- **social URLs** — facebook/instagram/x-twitter/youtube/linkedin links found
  on official pages (recorded only; no scraping of those platforms)
- **page roles** — contact_page / submission_page / programming_page markers

## 9. Role classification (`enrichment/roles.py`)

Ordered longest-match keyword rules:
music_director · program_director · programming · music_submission ·
station_manager · dj · general · advertising · unknown. Deterministic,
unit-tested, transparent. An AI-enrichment hook may replace `unknown`
outcomes later without changing call sites.

## 10. Station classification (`enrichment/stations.py`)

Evidence keywords over combined page text map to candidate types:
college/university/campus, community, public (NPR/public radio),
independent, internet/online. Highest-evidence wins; ties and empty
evidence → `unknown`. Output: `station_type`,
`classification_confidence`, `classification_evidence[]`.

## 11. Deduplication (`enrichment/dedupe.py`)

Primary key: **canonical registrable-domain approximation** (lowercased host,
`www.` stripped, common two-part suffixes like co.uk/com.au collapsed).
Merge when canonical domains match; fallback key
`slug(name)+country+region` only when no domain exists — never name-only
(different cities share names). Merges union source_urls, emails, contacts,
socials; conflicting names retained as alternates; provenance preserved.
Fuzzy matching explicitly deferred (documented).

## 12. Confidence scoring (`enrichment/confidence.py`)

Additive explainable signals (each ±weight listed in code), total clamped to
[0, 1]: website reachable (+0.25) · official-looking domain (+0.10) ·
name/title match (+0.15) · contact page found (+0.15) · professional
own-domain email (+0.20) vs generic inbox (+0.10) vs free-mail (+0.03) ·
submission page found (+0.15) · station-type evidence (+0.05) · multiple
distinct source references (+0.10) · penalties: broken site (−0.30),
domain/name mismatch suspicion (−0.15). Result stored with
`confidence_reasons[]`; no opaque scores.

Email quality is reported separately (own-domain/generic/free/invalid,
inbox-role guess) and **never** implies deliverability. Phase 2 semantics:
"publicly discovered", not "verified".

## 13. Provenance

Every meaningful field carries `Fact` metadata:
`{value, source_url, source_type, method, discovered_at}`. Station records
additionally retain full `source_urls[]`, per-fact provenance maps, and raw
page references. This satisfies: where did the station/email/name come from,
when observed.

## 14. Structured output & minimal interface

`RadioDiscoveryEngine.run(request) -> DiscoveryResult` is the internal API
(documented; no production HTTP layer yet). A tiny CLI exists for manual runs:

```
python -m discovery.radio.pipeline --request request.json [--seed seeds.json]
```

printing one JSON document (records, failures, counts). Persistence is
intentionally absent until Phase 6.

## 15. n8n boundary (documentation only)

Future wiring: n8n schedule/manual trigger → HTTP node calls a thin job
endpoint (future backend, Phase 6) → engine run → records persisted → n8n
notified of completion. The engine itself stays callable without n8n; no
production workflow is built in Phase 2.

## 16. Database boundary

Records are plain JSON-mappable dataclasses designed to map onto the Phase 1
entity model: StationRecord→Organization(+radio enrichment), ContactRecord→
Contact+ContactMethod(email), Fact→Source/EnrichmentResult provenance rows.
See `docs/data-model.md` §7. No database is touched in Phase 2.

## 17. Dependencies decision

Zero third-party dependencies. `urllib.request`, `urllib.robotparser`,
`html.parser`, `re`, `json`, `dataclasses`, `logging`, `datetime`, `uuid`,
`pathlib` cover all Phase 2 needs. Rationale: dependency discipline (Phase 1
Rule 8); httpx/requests/beautifulsoup bring no capability we need at this
scale and can be adopted later behind `crawler/http.py` and `crawler/pages.py`
abstractions without touching pipeline code.

## 18. Limitations (honest list)

- Seed-file provider means discovery breadth depends on supplied seeds; no
  live web-search integration without credentials.
- Registrable-domain approximation uses a small suffix list, not the PSL.
- Name extraction heuristics are conservative; many contacts stay unnamed.
- No JS rendering — static HTML only.
- No retries, no proxy support (by design), no CAPTCHA/anti-bot handling
  ever.
- Classification is English-keyword based.
- Emails are unverified; deliverability unknown.

## 19. Legal / safety posture

Public pages only; robots.txt respected; rate-limited; bounded depth; no
authentication/paywall/CAPTCHA bypass; no private personal data targeted —
business/role contact info only; no stealth mechanisms; user-agent identifies
the tool honestly.
