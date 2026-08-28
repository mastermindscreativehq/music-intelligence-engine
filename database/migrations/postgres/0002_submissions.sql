-- Phase 8: PostgreSQL/Supabase mirror of SQLite schema v3 (Phase 8).
--
-- tracks: content-addressed submission-asset library. track_id is the
-- OPAQUE public asset identifier; storage locations are never persisted,
-- so the backend can move to object storage without contract changes.
-- submission_link_checks: append-only accessibility history per URL.
-- Timestamps stay TEXT (ISO-8601) exactly like every other table so
-- payloads are byte-identical across backends.

CREATE TABLE IF NOT EXISTS tracks (
    track_id          TEXT PRIMARY KEY,   -- 'sha256:<hex>' (opaque)
    sha256            TEXT NOT NULL UNIQUE,
    original_filename TEXT,
    size_bytes        BIGINT NOT NULL CHECK (size_bytes > 0),
    content_type      TEXT NOT NULL DEFAULT 'audio/mpeg',
    status            TEXT NOT NULL CHECK (status IN
                         ('ready', 'quarantined', 'archived')),
    reject_reason     TEXT,
    notes             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS submission_link_checks (
    check_id     BIGSERIAL PRIMARY KEY,
    identity_key TEXT NOT NULL REFERENCES organizations(identity_key)
                 ON DELETE CASCADE,
    url          TEXT NOT NULL,
    target_kind  TEXT NOT NULL CHECK (target_kind IN
                    ('submission_url', 'instructions_page')),
    ok           BOOLEAN NOT NULL,
    status       INTEGER,
    error_kind   TEXT,
    latency_ms   INTEGER,
    checked_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_link_checks_station
    ON submission_link_checks(identity_key);
