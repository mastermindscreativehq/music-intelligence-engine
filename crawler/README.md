# crawler/

Deterministic crawling module. **No AI in this module** — the LLM never
performs web operations.

**Owns:** source-driven discovery queue, website retrieval, page fetching, contact-page
and submission-page discovery, rate limiting, retry behavior, robots compliance, crawl
state tracking.

**Produces:** raw page records and structured extraction inputs for `enrichment/`.

**Never does:** interpretation/classification of content (that is enrichment's job),
email sending, or trust decisions.

Design notes: deterministic code only; politeness (rate limits, robots.txt) is a core
requirement, not an afterthought; every fetch is attributable to a source record.

## Phase 2 status: implemented (bounded retrieval)

| Module          | Responsibility                                                    |
|-----------------|-------------------------------------------------------------------|
| `http.py`       | `StdlibHttpFetcher` + `FetchResult`: timeouts, size cap, content-type gate, robots.txt (`urllib.robotparser`), per-host rate limiting, classified failure kinds |
| `urls.py`       | URL normalization (tracking params dropped, meaningful params kept/sorted), canonical hosts & registrable-domain approximation |
| `pages.py`      | stdlib `HTMLParser`-based link/text/mailto extraction with malformed-markup recovery |
| `page_finder.py`| Focused priority-page selection (keyword-weighted, per-site budget, no unrestricted spider) |

Retrieval policy: http/https only · robots respected · ≤ `max_pages_per_site`
pages/domain · depth ≤ 1 beyond homepage · failures recorded and skipped,
never fatal · **no** auth/CAPTCHA/anti-bot bypass, ever.

Extension points: swap `StdlibHttpFetcher` for another implementation exposing
`fetch(url) -> FetchResult` without touching pipeline code.
