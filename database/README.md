# database/

Dual-backend persistence layer (Phase 6): SQLite as the offline reference
implementation and PostgreSQL/Supabase for production, with a shared
contract.

## Modules

| Module | Purpose |
| --- | --- |
| `schema.py` | SQLite connection, `SCHEMA_VERSION`, forward-only migrations (v1: core tables; v2: verification history). |
| `service.py` | `PersistenceService` — validation gate (`validate_intelligence_record`), normalization, deterministic dedupe merge policy, listing queries, ingestion runs, **verification persistence**. The merge/validation helpers are imported by the PG backend so both backends behave identically by construction. |
| `repository.py` | `IntelligenceRepository` runtime-checkable Protocol — the contract every backend must satisfy. |
| `pg_store.py` | `PostgresStorage` — PostgreSQL twin of the service; lazy psycopg import; injectable connection for structural tests. |
| `schema_migrations.py` | Ordered SQL migration loader/runner for `migrations/postgres/NNNN_name.sql`. |

## Layout

- SQLite: schema is created/migrated in code by `schema.py`
  (`SCHEMA_VERSION = 2`). Migrations are additive and forward-only.
- PostgreSQL: DDL lives in `migrations/postgres/0001_init.sql`, applied by
  `apply_pg_migrations(conn)` and tracked in a `schema_migrations` table.
  Table names are generic (`organizations`, `organization_emails`,
  `organization_phones`, …); column names and payload shapes mirror the
  SQLite backend exactly.

## Contracts preserved across backends

- Same validation gate and normalization (shared functions).
- Same merge policy: null never erases a known value; lists/dicts union
  with provenance preserved; contacts upserted by stable `contact_uid`;
  identity keys (`domain:<host>` / `namegeo:<slug>`) decide station
  identity.
- Verification history (`persist_verification`) is **append-only** on both
  backends; results for stations unknown to storage are skipped, never
  created. All six statuses from `enrichment.verify` round-trip verbatim:
  `unverified | verified | failed | stale | conflicting | unsupported`.
- Listing filters (`genre`, `format_filter`, `country`, `min_confidence`)
  are portable (LIKE/=/>=) and additive to the Phase 4 API.

## Environment

- No secrets live here. PostgreSQL access comes from a DSN supplied at
  construction (CLI/env wiring belongs to the caller; see
  `backend/app.py`). psycopg is optional until you instantiate
  `PostgresStorage`; everything else runs dependency-free.

The full intended entity model remains documented in
[`docs/data-model.md`](../docs/data-model.md).
