"""SQLite schema and versioned migrations for the Phase 4 storage boundary.

Deliberate scope: persist the intelligence ALREADY produced by the Phase 3
enrichment engine (RadioIntelligenceRecord dicts). This is the minimum clean
structure required for that — no speculative tables.

Design notes:

- SQLite (stdlib) keeps the zero-dependency policy; PostgreSQL/Supabase
  remains the documented future target (docs/data-model.md) and is NOT
  implemented here. The schema maps 1:1 onto it (TEXT/REAL/INTEGER only,
  JSON columns portable to JSONB).
- Station identity reuses enrichment.dedupe.identity_key() so storage-level
  deduplication matches discovery-level deduplication exactly:
      "domain:<registrable-domain>"  or  "namegeo:<slug>"
- FACT / INFERENCE / UNKNOWN are preserved structurally, never flattened:
  emails/phones keep their full Fact dicts; the submission payload keeps
  its inference-labeled methods bundle; absent evidence stays NULL.
- Deterministic upserts make repeated ingestion safe (idempotent).

Migration mechanism: ordered list applied inside a transaction; applied
version tracked via PRAGMA user_version. Simple, auditable, dependency-free.
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 8

MIGRATIONS: list[tuple[int, str]] = [
    (1, """
CREATE TABLE IF NOT EXISTS stations (
    identity_key              TEXT PRIMARY KEY,   -- 'domain:x' | 'namegeo:y'
    identity_kind             TEXT NOT NULL,      -- 'domain' | 'namegeo'
    name                      TEXT NOT NULL,
    organization_type         TEXT,
    website                   TEXT,
    domain                    TEXT,
    country                   TEXT,
    state_or_region           TEXT,
    city                      TEXT,
    market_area               TEXT,
    station_type              TEXT,
    classification_confidence REAL,
    classification_evidence   TEXT,               -- JSON array
    formats                   TEXT,               -- JSON array
    genres                    TEXT,               -- JSON array
    genre_evidence            TEXT,               -- JSON object
    language                  TEXT,
    description               TEXT,
    social_urls               TEXT,               -- JSON object
    source_urls               TEXT,               -- JSON array
    discovered_at             TEXT,
    last_verified_at          TEXT,
    last_observed_at          TEXT,
    confidence_score          REAL,
    confidence_reasons        TEXT,               -- JSON array
    status                    TEXT,
    raw_metadata              TEXT,               -- JSON object
    first_stored_at           TEXT NOT NULL,
    last_stored_at            TEXT NOT NULL
);

-- Email Facts, one row per distinct value; the full Fact dict (provenance)
-- is stored verbatim so FACT metadata never degrades.
CREATE TABLE IF NOT EXISTS station_emails (
    identity_key TEXT NOT NULL REFERENCES stations(identity_key) ON DELETE CASCADE,
    value        TEXT NOT NULL,
    fact         TEXT NOT NULL,                 -- Fact dict JSON
    PRIMARY KEY (identity_key, value)
);

CREATE TABLE IF NOT EXISTS station_phones (
    identity_key TEXT NOT NULL REFERENCES stations(identity_key) ON DELETE CASCADE,
    value        TEXT NOT NULL,
    fact         TEXT NOT NULL,                 -- Fact dict JSON
    PRIMARY KEY (identity_key, value)
);

-- Contact identity is a content hash of the stable business fields, so the
-- engine's per-run UUIDs cannot create duplicates across ingestions.
CREATE TABLE IF NOT EXISTS contacts (
    contact_uid               TEXT PRIMARY KEY,  -- sha256(...)[:32]
    identity_key              TEXT NOT NULL REFERENCES stations(identity_key) ON DELETE CASCADE,
    engine_contact_id         TEXT,
    name                      TEXT,
    role                      TEXT,
    email                     TEXT,
    phone                     TEXT,
    source_url                TEXT,
    confidence_score          REAL,
    confidence_reasons        TEXT,              -- JSON array
    preferred_for_submissions INTEGER NOT NULL DEFAULT 0,
    verified_at               TEXT,
    provenance                TEXT,              -- JSON array
    first_stored_at           TEXT NOT NULL,
    last_stored_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contacts_station ON contacts(identity_key);

-- One submission path per station (1:1), stored verbatim as produced by the
-- intelligence engine — including its inference-labeled methods bundle.
CREATE TABLE IF NOT EXISTS submission_paths (
    identity_key    TEXT PRIMARY KEY REFERENCES stations(identity_key) ON DELETE CASCADE,
    payload         TEXT NOT NULL,             -- SubmissionPath.to_dict() JSON
    first_stored_at TEXT NOT NULL,
    last_stored_at  TEXT NOT NULL
);

-- Enrichment-time fetch outcomes per station; final state mirrors the last
-- ingestion for that station (delete-then-insert within one transaction).
CREATE TABLE IF NOT EXISTS source_fetches (
    fetch_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_key TEXT NOT NULL REFERENCES stations(identity_key) ON DELETE CASCADE,
    url         TEXT NOT NULL,
    ok          INTEGER,
    status      INTEGER,
    error_kind  TEXT,
    fetched_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_fetches_station ON source_fetches(identity_key);

-- Ingestion audit ledger (mirrors EnrichmentResult + Failure).
CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id           TEXT PRIMARY KEY,
    source           TEXT NOT NULL,
    started_at       TEXT NOT NULL,
    completed_at     TEXT,
    records_accepted INTEGER,
    records_failed   INTEGER
);

CREATE TABLE IF NOT EXISTS ingestion_failures (
    failure_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL REFERENCES ingestion_runs(run_id),
    stage      TEXT,
    error_kind TEXT,
    message    TEXT,
    url        TEXT
);
"""),

    # Phase 6: append-only persistence for Phase 5 verification output.
    # Status vocabulary is the reconciled six-value contract
    # (docs/data-model.md): unverified|verified|failed|stale|
    # conflicting|unsupported. Results are INSERT-only history; a repeated
    # verification run appends new rows and never rewrites old ones.
    (2, """
CREATE TABLE IF NOT EXISTS verification_runs (
    run_id       TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    completed_at TEXT,
    summary      TEXT,               -- JSON object: counts per status
    source       TEXT
);

CREATE TABLE IF NOT EXISTS verification_results (
    result_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL REFERENCES verification_runs(run_id) ON DELETE CASCADE,
    identity_key TEXT NOT NULL REFERENCES stations(identity_key) ON DELETE CASCADE,
    claim        TEXT NOT NULL,      -- verbatim from enrichment.verify output
    status       TEXT NOT NULL CHECK (status IN (
                     'unverified', 'verified', 'failed', 'stale',
                     'conflicting', 'unsupported')),
    method       TEXT,
    verifier     TEXT,
    evidence     TEXT,               -- JSON array (provenance preserved)
    reasons      TEXT,               -- JSON array
    checked_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verification_station
    ON verification_results(identity_key);
"""),

    # Phase 8: music submission assets + submission-link accessibility.
    # tracks: content-addressed library entries. track_id is the OPAQUE
    # public asset identifier ('sha256:<hex>'); no filesystem paths are
    # ever persisted — the storage backend owns the key->location mapping.
    # Blobs themselves live outside the database and are immutable.
    (3, """
CREATE TABLE IF NOT EXISTS tracks (
    track_id          TEXT PRIMARY KEY,   -- 'sha256:<hex>' (opaque)
    sha256            TEXT NOT NULL UNIQUE,
    original_filename TEXT,
    size_bytes        INTEGER NOT NULL CHECK (size_bytes > 0),
    content_type      TEXT NOT NULL DEFAULT 'audio/mpeg',
    status            TEXT NOT NULL CHECK (status IN
                         ('ready', 'quarantined', 'archived')),
    reject_reason     TEXT,
    notes             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

-- Append-only accessibility history per checked URL; mirrors the
-- SourceFetchRecord vocabulary so failure kinds stay comparable.
CREATE TABLE IF NOT EXISTS submission_link_checks (
    check_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_key TEXT NOT NULL REFERENCES stations(identity_key)
                 ON DELETE CASCADE,
    url          TEXT NOT NULL,
    target_kind  TEXT NOT NULL CHECK (target_kind IN
                    ('submission_url', 'instructions_page')),
    ok           INTEGER NOT NULL,
    status       INTEGER,
    error_kind   TEXT,
    latency_ms   INTEGER,
    checked_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_link_checks_station
    ON submission_link_checks(identity_key);
"""),

    # Phase 9: outreach messages + attempt ledger. A message row is the
    # operator's outreach draft/record; attempts append every delivery
    # handoff/event so history is traceable. Status is the explicit
    # vocabulary draft|opened_in_email|sent|failed and is NEVER set to
    # 'sent' merely because a mail client opened. track references are
    # stored by opaque track_id (a foreign key into tracks) but a message
    # may also carry freeform track metadata captured at compose time.
    (4, """
CREATE TABLE IF NOT EXISTS outreach_messages (
    outreach_id      TEXT PRIMARY KEY,   -- 'om_<hex>'
    contact_uid      TEXT,
    identity_key     TEXT,
    recipient_name   TEXT,
    recipient_role   TEXT,
    organization     TEXT,
    email            TEXT NOT NULL,
    source_url       TEXT,
    track_id         TEXT,
    track            TEXT,               -- JSON object (track metadata)
    context          TEXT,               -- JSON object (artist/track context)
    subject          TEXT,
    message          TEXT,
    from_email       TEXT,
    sharing          TEXT,               -- JSON object (sharing options)
    status           TEXT NOT NULL CHECK (status IN
                       ('draft', 'opened_in_email', 'sent', 'failed')),
    provider         TEXT NOT NULL DEFAULT 'local',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outreach_contact
    ON outreach_messages(contact_uid);
CREATE INDEX IF NOT EXISTS idx_outreach_status
    ON outreach_messages(status);

CREATE TABLE IF NOT EXISTS outreach_attempts (
    attempt_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    outreach_id   TEXT NOT NULL REFERENCES outreach_messages(outreach_id)
                  ON DELETE CASCADE,
    event         TEXT NOT NULL CHECK (event IN
                    ('opened_in_email', 'sent', 'failed')),
    provider      TEXT NOT NULL DEFAULT 'local',
    at            TEXT NOT NULL,
    meta          TEXT                -- JSON object
);
CREATE INDEX IF NOT EXISTS idx_outreach_attempts_msg
    ON outreach_attempts(outreach_id);
"""),
    (5, """
ALTER TABLE outreach_messages
    ADD COLUMN outreach_class TEXT NOT NULL DEFAULT 'email';
ALTER TABLE outreach_messages
    ADD COLUMN submission_url TEXT;
"""),

    # Stabilization 2026-09: adopt the canonical outreach lifecycle
    # ready -> sent -> responded -> follow_up -> closed (plus the honest
    # terminal 'failed'). Non-destructive: legacy values are REMAPPED in
    # place, never deleted ('draft' and 'opened_in_email' both become
    # 'ready'; the mail-client handoff nuance is preserved verbatim in the
    # attempts ledger, whose event vocabulary now covers the full set).
    # SQLite cannot alter a CHECK constraint, so both tables are rebuilt
    # with their full current shape (v4 columns + v5 additions) and a
    # relaxed constraint, then the data is copied verbatim modulo the
    # status/event remap.
    (6, """
PRAGMA foreign_keys=OFF;

CREATE TABLE outreach_attempts_v6 (
    attempt_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    outreach_id   TEXT NOT NULL REFERENCES outreach_messages(outreach_id)
                  ON DELETE CASCADE,
    event         TEXT NOT NULL CHECK (event IN
                    ('opened_in_email', 'sent', 'responded', 'follow_up',
                     'failed', 'closed')),
    provider      TEXT NOT NULL DEFAULT 'local',
    at            TEXT NOT NULL,
    meta          TEXT                -- JSON object
);
INSERT INTO outreach_attempts_v6
    (attempt_id, outreach_id, event, provider, at, meta)
    SELECT attempt_id, outreach_id, event, provider, at, meta
    FROM outreach_attempts;
DROP TABLE outreach_attempts;
ALTER TABLE outreach_attempts_v6 RENAME TO outreach_attempts;
CREATE INDEX IF NOT EXISTS idx_outreach_attempts_msg
    ON outreach_attempts(outreach_id);

CREATE TABLE outreach_messages_v6 (
    outreach_id      TEXT PRIMARY KEY,   -- 'om_<hex>'
    contact_uid      TEXT,
    identity_key     TEXT,
    recipient_name   TEXT,
    recipient_role   TEXT,
    organization     TEXT,
    email            TEXT NOT NULL,
    source_url       TEXT,
    track_id         TEXT,
    track            TEXT,               -- JSON object (track metadata)
    context          TEXT,               -- JSON object (artist/track context)
    subject          TEXT,
    message          TEXT,
    from_email       TEXT,
    sharing          TEXT,               -- JSON object (sharing options)
    status           TEXT NOT NULL CHECK (status IN
                       ('ready', 'sent', 'responded', 'follow_up',
                        'failed', 'closed')),
    provider         TEXT NOT NULL DEFAULT 'local',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    outreach_class   TEXT NOT NULL DEFAULT 'email',
    submission_url   TEXT
);
INSERT INTO outreach_messages_v6
    (outreach_id, contact_uid, identity_key, recipient_name, recipient_role,
     organization, email, source_url, track_id, track, context, subject,
     message, from_email, sharing, status, provider, created_at, updated_at,
     outreach_class, submission_url)
    SELECT outreach_id, contact_uid, identity_key, recipient_name,
           recipient_role, organization, email, source_url, track_id, track,
           context, subject, message, from_email, sharing,
           CASE status
               WHEN 'draft' THEN 'ready'
               WHEN 'opened_in_email' THEN 'ready'
               ELSE status
           END,
           provider, created_at, updated_at, outreach_class, submission_url
    FROM outreach_messages;
DROP TABLE outreach_messages;
ALTER TABLE outreach_messages_v6 RENAME TO outreach_messages;
CREATE INDEX IF NOT EXISTS idx_outreach_contact
    ON outreach_messages(contact_uid);
CREATE INDEX IF NOT EXISTS idx_outreach_status
    ON outreach_messages(status);

PRAGMA foreign_keys=ON;
"""),

    # Phase 4: DJ intelligence. DJs are discrete, source-backed outreach
    # subjects, independent of the station registry (station links are an
    # optional reference, never a required FK). Channels are per-fact rows
    # with provenance (source_url) so nothing is ever invented: a value is
    # stored only when observed. contact/submission routes distinguish a
    # verified direct DJ contact (email), an official submission route,
    # a generic contact route, and social routes — all presence-derived.
    # Fully additive: existing tables are untouched.
    (7, """
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
    genres           TEXT,               -- JSON array
    formats          TEXT,               -- JSON array
    source_urls      TEXT,               -- JSON array (provenance)
    verification     TEXT,               -- JSON object
    discovered_at    TEXT,
    last_observed_at TEXT,
    first_stored_at  TEXT NOT NULL,
    last_stored_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_djs_station ON djs(station_key);
CREATE INDEX IF NOT EXISTS idx_djs_country ON djs(country);
CREATE INDEX IF NOT EXISTS idx_djs_genres  ON djs(genres);

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

-- Outreach targets now identify their kind so DJ outreach stays isolated
-- from station / opportunity history. Additive: existing rows default to
-- the historic behavior ('station').
ALTER TABLE outreach_messages
    ADD COLUMN target_type TEXT NOT NULL DEFAULT 'station';
"""),

    # Phase 11: automation discovery jobs. One row per scheduled/automation
    # discovery run so operators can answer WHEN a job ran, on what
    # configuration, through which provider, and with what outcome — without
    # re-deriving it from logs. Additive: no existing table is touched.
    (8, """
CREATE TABLE IF NOT EXISTS discovery_jobs (
    run_id             TEXT PRIMARY KEY,   -- 'job_<24hex>' (opaque)
    organization_type  TEXT NOT NULL,      -- 'dj' today; future target types
    config             TEXT NOT NULL,      -- JSON job configuration
    provider           TEXT,               -- 'serpapi_google' | 'djs_http_search:<host>' | ...
    status             TEXT NOT NULL,      -- completed | completed_with_failures |
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
"""),
]


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Bring *conn* up to SCHEMA_VERSION; returns the resulting version."""
    conn.execute("PRAGMA foreign_keys=ON")
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, sql in MIGRATIONS:
        if version <= current:
            continue
        with conn:   # transactional migration
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version={int(version)}")
    return conn.execute("PRAGMA user_version").fetchone()[0]
