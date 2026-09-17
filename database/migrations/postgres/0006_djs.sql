-- Phase 4: DJ intelligence on PostgreSQL/Supabase (mirror of SQLite schema v7).
--
-- DJs are discrete, source-backed outreach subjects, independent of the
-- station registry (station links are an optional reference, never a
-- required FK). Channels are per-fact rows with provenance (source_url) so
-- nothing is ever invented: a value is stored only when observed.
-- outreach_messages gains target_type (additive; existing rows default to
-- the historic 'station' behavior so station/opportunity history is
-- untouched).

CREATE TABLE IF NOT EXISTS djs (
    dj_id            TEXT PRIMARY KEY,   -- 'dj_<24hex>' (opaque)
    name             TEXT NOT NULL,
    stage_name       TEXT,
    role             TEXT,
    program          TEXT,
    station_key      TEXT,               -- stations.identity_key when mapped
    station_name     TEXT,
    platform         TEXT,
    country          TEXT,
    state_or_region  TEXT,
    city             TEXT,
    genres           JSONB,              -- array
    formats          JSONB,              -- array
    source_urls      JSONB,              -- array (provenance)
    verification     JSONB,              -- object
    discovered_at    TEXT,
    last_observed_at TEXT,
    first_stored_at  TEXT NOT NULL,
    last_stored_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_djs_station ON djs(station_key);
CREATE INDEX IF NOT EXISTS idx_djs_country ON djs(country);

CREATE TABLE IF NOT EXISTS dj_channels (
    dj_id       TEXT NOT NULL REFERENCES djs(dj_id) ON DELETE CASCADE,
    channel     TEXT NOT NULL CHECK (channel IN
                  ('email', 'website', 'instagram', 'x', 'facebook',
                   'youtube', 'submission_email', 'submission_page',
                   'contact_page')),
    value       TEXT NOT NULL,
    source_url  TEXT NOT NULL,
    verified_at TEXT,
    PRIMARY KEY (dj_id, channel, value)
);
CREATE INDEX IF NOT EXISTS idx_dj_channels_dj ON dj_channels(dj_id);

ALTER TABLE outreach_messages
    ADD COLUMN IF NOT EXISTS target_type TEXT NOT NULL DEFAULT 'station';