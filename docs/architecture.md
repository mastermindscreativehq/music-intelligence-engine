# System Architecture

> Phase 2 note: the radio discovery slice is now implemented. See
> [Radio Discovery Engine](radio-discovery.md) for the concrete module map,
> retrieval rules, and pipeline stages. This document remains the
> platform-level architecture of record.

## 1. Overview

The Music Intelligence Engine is a modular intelligence platform for music-industry
contacts. The pipeline is generic; each target type (`radio_station` today; curators,
DJs, blogs, labels, A&R, festivals, influencers later) plugs into the same stages.

```
DISCOVER → CRAWL → EXTRACT → NORMALIZE → ENRICH → VERIFY → SCORE
        → STORE → SEARCH → PERSONALIZE → APPROVE → OUTREACH → TRACK
```

## 2. System boundaries

```
+--------------------------------------------------------------------------+
|                            n8n ORCHESTRATION                              |
|   (coordinates workflows between modules; contains no business logic)     |
+-----+----------------+------------------+---------------+---------------+
      |                |                  |               |
      v                v                  v               v
+-----------+    +------------+    +-----------+   +-----------+
|  CRAWLER  |    | ENRICHMENT |   |  BACKEND  |   | OUTREACH  |
| HTTP only |    | code + AI  |    |  FastAPI  |   | approval- |
| no AI     |    |            |    |           |   | gated     |
+-----+-----+    +-----+------+    +-----+-----+   +-----+-----+
      |                |                 |               |
      |          +-----v------+          |               |
      |          |   OLLAMA   |          |               |
      |          | localhost  |          |               |
      |          | qwen2.5-   |          |               |
      |          | coder:7b   |          |               |
      |          +------------+          |               |
      v                                  v               v
+--------------------------------------------------------------------------+
|                    DATABASE (PostgreSQL / Supabase)                       |
+--------------------------------------------------------------------------+
                                   ^
                                   |
                          +--------+--------+
                          |     FRONTEND    |
                          | search / filter |
                          | inspect/approve |
                          +-----------------+
```

Hard boundaries:

| Module     | May do                                        | Must never do                          |
|------------|-----------------------------------------------|----------------------------------------|
| crawler    | fetch URLs, respect robots/rate limits        | call the LLM, send email, decide trust |
| enrichment | normalize, classify, score confidence         | crawl websites                         |
| Ollama     | semantic extraction/classification/generation | act as a web crawler or email sender   |
| n8n        | trigger, schedule, route between modules      | implement business logic in workflows  |
| backend    | persist/query/serve intelligence              | contain crawler-specific logic         |
| outreach   | prepare messages, deliver after approval      | send anything without human approval   |

## 3. Data flow

1. **Discover** — legitimate public sources produce candidate organizations.
2. **Crawl** — deterministic HTTP retrieval of candidate sites; contact/submission page
   discovery; rate-limited, retried, state-tracked.
3. **Extract** — deterministic parsers pull structured fields from pages.
4. **Normalize** — domains, names, emails, phones normalized to canonical form.
5. **Enrich** — classification (org type, station format, genre, contact role); simple
   cases by rules, ambiguous cases by local LLM.
6. **Verify** — cross-source comparison; verification status recorded separately from
   discovery.
7. **Score** — confidence per fact/contact/organization; relevance scored independently.
8. **Store** — PostgreSQL via backend persistence layer, full provenance retained.
9. **Search** — API + frontend filtering by location, genre, format, confidence.
10. **Personalize** — LLM-drafted outreach messages from approved templates.
11. **Approve** — human reviews every recipient and message before sending.
12. **Outreach** — provider delivery, throttled and capped.
13. **Track** — delivery events, replies, bounces, opt-outs feed suppression.

## 4. Module responsibilities

### crawler/
Deterministic HTTP layer: source-driven discovery queue, URL fetching, robots compliance,
rate limiting, retries, crawl-state tracking, contact-page and submission-page discovery.
Produces raw page records + extraction inputs. Contains **zero AI logic**.

### enrichment/
Contact/name/role extraction, normalization, organization classification, station-format
and genre classification, email classification, duplicate detection, confidence scoring,
source comparison. Deterministic rules first; Ollama consulted for genuinely ambiguous
semantic judgments (see [AI Architecture](ai-architecture.md)).

### outreach/
Campaign preparation, personalized message generation (drafts only), submission/file
references, recipient selection, **human approval gate**, delivery execution, bounce and
response tracking, suppression and opt-out handling. Never assumes "email found = send".

### backend/
FastAPI application serving organizations, contacts, sources, confidence data, campaigns,
submissions, outreach state. Owns persistence and business queries. No crawling logic.

### frontend/
Search/filter UI (location, genre, format), contact inspection with confidence and source
attribution visible, campaign building, message review/approval, upload flow, tracking.

### n8n/
Orchestration only: small logical workflows (e.g., `discovery → crawler`, `extraction →
enrichment → validation → database`, `select → generate → review → approve → send →
track`). Deliberately split so each unit is testable and replaceable.

### database/
PostgreSQL-compatible schema suitable for Supabase. Entity model documented in
[Data Model](data-model.md).

## 5. Why modular

- **Target-type extensibility.** Radio is one value of `organization_type`, not a bespoke
  schema. New target types reuse discovery/crawl/enrich/verify/score/outreach unchanged.
- **Independent testability.** Deterministic modules are unit-testable without AI;
  AI-assisted steps have contract boundaries (structured JSON in/out).
- **Failure isolation.** A crawler bug cannot corrupt enrichment; an LLM outage degrades
  to rules-based behavior instead of halting the platform.
- **Operational separation.** Long-running crawling, synchronous API traffic, and human
  approval flows scale and fail differently; separating them keeps each simple.
- **Swappable components.** Email providers, storage backends, or even the local model can
  change without touching the pipeline shape.

## 6. Intelligence principles

The system preserves, for every extracted fact:

- `source` (URL + source type)
- `extraction method` (rule/parser/LLM/manual)
- `confidence` (0–1)
- `verification status` (unverified / verified / failed / stale /
  conflicting / unsupported)
- `timestamps` (discovered, last verified)

An email found on a webpage is not assumed to belong to the right organization, the right
person, accept music, or still be active. Those are separate, tracked properties.
