-- Phase 11: automation discovery jobs (mirror of SQLite schema v8).
--
-- One row per scheduled / automation discovery run so operators can answer
-- WHEN a job ran, on what configuration, through which provider, and with
-- what outcome -- without re-deriving it from logs. Additive: no existing
-- table is touched.

CREATE TABLE IF NOT EXISTS discovery_jobs (
    run_id             TEXT PRIMARY KEY,   -- 'job_<24hex>' (opaque)
    organization_type  TEXT NOT NULL,      -- 'dj' today; future target types
    config             JSONB NOT NULL,     -- job configuration
    provider           TEXT,               -- 'serpapi_google' | 'djs_http_search:<host>' | ...
    status             TEXT NOT NULL,      -- queued | running | completed |
                                           -- completed_with_failures |
                                           -- not_configured | failed
    queries_run        INTEGER NOT NULL DEFAULT 0,
    candidates_found   INTEGER NOT NULL DEFAULT 0,
    records_ingested   INTEGER NOT NULL DEFAULT 0,
    duplicates         INTEGER NOT NULL DEFAULT 0,
    failures           INTEGER NOT NULL DEFAULT 0,
    error_message      TEXT,
    started_at         TEXT NOT NULL,
    completed_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_discovery_jobs_started
    ON discovery_jobs(started_at);
CREATE INDEX IF NOT EXISTS idx_discovery_jobs_org
    ON discovery_jobs(organization_type);