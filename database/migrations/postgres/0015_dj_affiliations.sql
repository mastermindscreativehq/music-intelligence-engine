-- Phase: DJ affiliations (multiple confirmed station affiliations)
-- Additive, idempotent. Does not modify existing djs rows or classify by NULLs.

CREATE TABLE IF NOT EXISTS dj_affiliations (
    aff_id TEXT PRIMARY KEY,
    dj_id TEXT NOT NULL REFERENCES djs(dj_id) ON DELETE CASCADE,
    station_key TEXT,
    station_name TEXT,
    role TEXT,
    source_url TEXT,
    verification_status TEXT NOT NULL DEFAULT 'unverified',
    evidence JSONB,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dj_affiliations_dj_id ON dj_affiliations(dj_id);
CREATE INDEX IF NOT EXISTS idx_dj_affiliations_station_key ON dj_affiliations(station_key);
CREATE INDEX IF NOT EXISTS idx_dj_affiliations_dj_id_status ON dj_affiliations(dj_id, verification_status);
