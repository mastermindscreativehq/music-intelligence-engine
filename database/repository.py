"""Backend-agnostic storage surface (Phase 6).

Both persistence implementations satisfy this protocol structurally:

- ``PersistenceService``   (SQLite, stdlib) — the Phase 4/5 reference
  implementation powering all offline tests and development.
- ``PostgresStorage``      (database/pg_store.py, psycopg) — the
  PostgreSQL/Supabase implementation; identical payloads, JSONB columns.

The contract: intelligence goes IN as ``RadioIntelligenceRecord`` dicts
(through the same validation gate), comes OUT as plain decoded dicts with
FACT provenance verbatim and INFERENCE labels intact. Neither backend is
allowed to reinterpret, promote, or drop evidence.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class IntelligenceRepository(Protocol):

    def ingest_intelligence(self, records: list[dict], *,
                            source: str = ...) -> object:
        """Idempotent upsert of validated intelligence records."""
        ...

    def list_stations(self, limit: int = ..., offset: int = ...,
                      q: str | None = ...,
                      status: str | None = ...,
                      genre: str | None = ...,
                      format_filter: str | None = ...,
                      country: str | None = ...,
                      min_confidence: float | None = ...,
                      ) -> tuple[list[dict], int]:
        """Filtered listing; returns (rows, total)."""
        ...

    def get_station(self, identity_key: str) -> dict | None: ...
    def get_station_emails(self, identity_key: str) -> list[dict]: ...
    def get_station_phones(self, identity_key: str) -> list[dict]: ...
    def get_station_contacts(self, identity_key: str) -> list[dict]: ...
    def get_submission(self, identity_key: str) -> dict | None: ...
    def get_fetches(self, identity_key: str) -> list[dict]: ...

    def persist_verification(self, records: list[dict], report: dict, *,
                             source: str = ...) -> dict:
        """Append-only persistence of a verify_records() report."""
        ...

    def get_verification(self, identity_key: str) -> dict | None: ...
    def get_ingestion_run(self, run_id: str) -> dict | None: ...
    def close(self) -> None: ...
