# discovery/

Reusable discovery subsystem (Phase 2+). The generic machinery lives here;
per-target-type logic is isolated in subpackages (`discovery/radio/` today).

**Generic (org-type agnostic):**

| Module          | Responsibility                                            |
|-----------------|-----------------------------------------------------------|
| `models.py`     | DiscoveryRequest, Candidate, Fact (provenance), Failure, DiscoveryResult, EnrichmentResult |
| `queries.py`    | Deterministic structured-request → search-query generation |
| `providers.py`  | Provider Protocol + SeedListProvider (credential-free)     |
| `events.py`     | Structured JSON event logging                              |

**Target types:** `discovery/radio/` — discovery pipeline (`pipeline.py`),
enrichment engine + CLI (`enrich_pipeline.py`, offline by default, bounded
opt-in fetching), pure assembly layer (`intelligence.py`) and normalized
records/schema extensions (`schema.py`). Future types (blogs, labels,
curators, ...) add sibling packages reusing everything above and all of
`crawler/` + `enrichment/`.

Details: [`docs/radio-discovery.md`](../docs/radio-discovery.md),
[`docs/radio-enrichment.md`](../docs/radio-enrichment.md).
