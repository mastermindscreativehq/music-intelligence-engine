# backend/

Application/API layer (Phase 6).

**Owns:** serving stored organizations, contacts, intelligence records,
search and filtering, verification exposure, ingestion API.

**Does not own:** crawler-specific logic (→ `crawler/`), classification
rules (→ `enrichment/`), message generation or delivery (→ `outreach/`),
orchestration (→ `n8n/`).

## Two servers, one contract

| Server | Module | Stack | Use |
| --- | --- | --- | --- |
| Stdlib reference server | `api.py` (`python -m backend.api`) | stdlib only | offline/tests, zero dependencies |
| FastAPI application | `app.py` (`python -m backend.app`) | FastAPI + Uvicorn over an injected storage backend (`--db` SQLite / `--dsn` PostgreSQL) | Phase 6 primary per `docs/architecture.md` |

Both serve the identical envelope and payload shapes; response builders
live in `contracts.py` and are shared verbatim.

## Envelope

```
success -> {"ok": true,  "data": <payload>, "error": null}
failure -> {"ok": false, "data": null,
            "error": {"code": <str>, "message": <str>}}
```

Error codes: `station_not_found`, `run_not_found`, `bad_request`,
`route_not_found`, `method_not_allowed`, `internal_error`. Validation
failures never leak internals; env values are never echoed into responses
(test-enforced).

## Routes

```
GET  /api/v1/health
GET  /api/v1/stations?limit&offset&q&status[&genre&format&country&min_confidence]
GET  /api/v1/stations/{identity_key}
GET  /api/v1/stations/{identity_key}/intelligence     # fact/inference/unknown map
GET  /api/v1/stations/{identity_key}/contacts         # + preferred submission contacts
GET  /api/v1/stations/{identity_key}/verification     # append-only history, six statuses
POST /api/v1/ingest                                   # {"records":[...], "source": str?}
GET  /api/v1/runs/{run_id}                            # ingestion run ledger + failures
```

`POST /api/v1/ingest` reuses the storage layer's validation gate and merge
policy: malformed records are isolated as per-record failures and never
abort the batch; re-posting the same records is idempotent (deterministic
identity upsert).

## Storage selection

`create_app(storage)` accepts any `database.repository.IntelligenceRepository`
implementation — SQLite `PersistenceService` by default in tests, or
`PostgresStorage(dsn=...)` for PostgreSQL/Supabase. The DSN comes from the
caller (`--dsn` flag or `MIE_PG_DSN`); no credentials are stored or logged.
