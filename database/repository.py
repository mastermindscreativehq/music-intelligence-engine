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

    # -- Phase 8: submission assets + link accessibility ----------------------

    def save_track(self, track: dict) -> dict:
        """Insert or update a track row; returns the stored projection.

        ``track_id`` ('sha256:<hex>') is the only asset identifier; no
        filesystem paths cross this boundary.
        """
        ...

    def get_track(self, track_id: str) -> dict | None: ...
    def list_tracks(self, limit: int = ..., offset: int = ...,
                    status: str | None = ...) -> tuple[list[dict], int]:
        """Listing; returns (rows, total), newest first."""
        ...

    def record_link_check(self, identity_key: str, entry: dict) -> None:
        """Append one accessibility check row (never rewrites history)."""
        ...

    def get_link_checks(self, identity_key: str,
                        limit: int = ...) -> list[dict]:
        """Most recent checks first."""
        ...

    # -- Phase 9: outreach records + attempt ledger ---------------------------

    def save_outreach(self, record: dict) -> dict:
        """Insert or overwrite one outreach message row."""
        ...

    def get_outreach(self, outreach_id: str) -> dict | None: ...

    def list_outreach(self, limit: int = ..., offset: int = ...,
                      status: str | None = ...) -> tuple[list[dict], int]:
        """Listing; returns (rows, total), newest first."""
        ...

    def append_outreach_attempt(self, outreach_id: str,
                                attempt: dict) -> None:
        """Append one traceable delivery event (never rewrites history)."""
        ...

    def set_outreach_status(self, outreach_id: str, status: str,
                            at: str | None = ...) -> None:
        """Advance a message's status with an explicit timestamp."""
        ...

    def get_outreach_attempts(self, outreach_id: str) -> list[dict]:
        """All recorded events for a message, oldest first."""
        ...

    def close(self) -> None: ...
