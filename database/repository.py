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

    # -- Phase 11: automation discovery jobs -----------------------------------

    def record_discovery_job(self, report: dict) -> str:
        """Persist one automation discovery job run; returns its run_id.

        ``report`` is the same shape the discovery job endpoint returns so
        stored metadata and API responses stay in sync.
        """
        ...

    def get_discovery_job(self, run_id: str) -> dict | None:
        """One stored automation discovery job run (or None)."""
        ...

    def clone(self):
        """A new independent storage bound to the same database.

        The background discovery worker runs on its own connection so an
        in-flight job never blocks polling status reads.
        """
        ...

    # -- Phase 8: submission assets + link accessibility ----------------------

    def save_track(self, track: dict) -> dict:
        """Insert or update a track row; returns the stored projection.

        ``track_id`` ('sha256:<hex>') is the only asset identifier; no
        filesystem paths cross this boundary.
        """
        ...

    def get_track(self, track_id: str) -> dict | None: ...
    def delete_track(self, track_id: str) -> str | None:
        """Delete one stored asset record; returns the deleted id or None.

        Only the database record is removed — never the uploaded file bytes
        in the asset store (the existing app defines no file-level removal).
        """
        ...
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

    def delete_outreach(self, outreach_id: str) -> str | None:
        """Delete one outreach record; returns the deleted id or None.

        Any attempt history for the record is removed with it; the raw
        station/contact data that produced the record is untouched.
        """
        ...

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

    # -- Phase 4: DJ intelligence ---------------------------------------------

    def list_djs(self, limit: int = ..., offset: int = ...,
                 q: str | None = ..., genre: str | None = ...,
                 country: str | None = ..., location: str | None = ...,
                 station: str | None = ..., dj_type: str | None = ...,
                 platform: str | None = ...,
                 has_contact: bool | None = ...,
                 sort: str | None = ...,
                 order: str | None = ...,
                 exclude_dev: bool = ...,
                 exclude_rejected: bool = ...) \
            -> tuple[list[dict], int, int, int]:
        """Filtered DJ listing; returns (rows, total), default order by name.

        With ``exclude_dev`` the tuple becomes
        ``(rows, visible_total, dev_fixtures_excluded)``; with
        ``exclude_rejected`` also set it becomes
        ``(rows, visible_total, dev_fixtures_excluded,
        rejected_excluded)``. ``rejected_excluded`` counts rows whose
        ``verification.classification.verdict`` is ``rejected`` (NOT a DJ) —
        they never surface in the normal listing.

        ``station`` matches the optional station AFFILIATION metadata only;
        ``has_contact`` requires/excludes profiles with at least one stored
        channel; rows include the read-path ``has_contact`` flag.
        """
        ...

    def update_dj_verification(self, dj_id: str, verification: dict) -> bool:
        """Replace one DJ's ``verification`` JSON (touch ``last_stored_at``).

        Used by data cleanup to persist classification verdicts without
        touching any other DJ column. Returns True when the row existed.
        """
        ...

    def get_dj(self, dj_id: str) -> dict | None:
        """One DJ row (JSON columns decoded) or None."""
        ...

    def get_dj_channels(self, dj_id: str) -> list[dict]:
        """Source-backed channels for one DJ, grouped by channel name."""
        ...

    def save_dj(self, record: dict, channels: list[dict] | None = ...) -> None:
        """Upsert one DJ row and replace its channel rows (transactional)."""
        ...

    def delete_dj(self, dj_id: str) -> str | None:
        """Delete one DJ (channels cascade); returns the id or None.

        Outreach records created for the DJ are preserved (target_type dj).
        """
        ...

    def close(self) -> None: ...
