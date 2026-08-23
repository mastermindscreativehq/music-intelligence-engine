-- Phase 6: PostgreSQL/Supabase schema (0001_init).
--
-- Portable mirror of the tested SQLite contract (database/schema.py v2):
-- identical columns, identity keys, and payload shapes; TEXT-JSON becomes
-- JSONB. Generic entity naming follows docs/data-model.md
-- (organizations instead of stations); radio maps onto it without bespoke
-- structure. FACT provenance stays verbatim inside JSONB Fact dicts;
-- INFERENCE labels are never rewritten; absent evidence stays NULL.
-- Verification status uses the reconciled six-value vocabulary.

CREATE TABLE IF NOT EXISTS organizations (
    identity_key              TEXT PRIMARY KEY,   -- 'domain:x' | 'namegeo:y'
    identity_kind             TEXT NOT NULL,
    name                      TEXT NOT NULL,
    organization_type         TEXT,
    website                   TEXT,
    domain                    TEXT,
    country                   TEXT,
    state_or_region           TEXT,
    city                      TEXT,
    market_area               TEXT,
    station_type              TEXT,
    classification_confidence DOUBLE PRECISION,
    classification_evidence   JSONB,
    formats                   JSONB,
    genres                    JSONB,
    genre_evidence            JSONB,
    language                  TEXT,
    description               TEXT,
    social_urls               JSONB,
    source_urls               JSONB,
    discovered_at             TEXT,
    last_verified_at          TEXT,
    last_observed_at          TEXT,
    confidence_score          DOUBLE PRECISION,
    confidence_reasons        JSONB,
    status                    TEXT,
    raw_metadata              JSONB,
    first_stored_at           TEXT NOT NULL,
    last_stored_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS organization_emails (
    identity_key TEXT NOT NULL REFERENCES organizations(identity_key) ON DELETE CASCADE,
    value        TEXT NOT NULL,
    fact         JSONB NOT NULL,
    PRIMARY KEY (identity_key, value)
);

CREATE TABLE IF NOT EXISTS organization_phones (
    identity_key TEXT NOT NULL REFERENCES organizations(identity_key) ON DELETE CASCADE,
    value        TEXT NOT NULL,
    fact         JSONB NOT NULL,
    PRIMARY KEY (identity_key, value)
);

CREATE TABLE IF NOT EXISTS contacts (
    contact_uid               TEXT PRIMARY KEY,
    identity_key              TEXT NOT NULL REFERENCES organizations(identity_key) ON DELETE CASCADE,
    engine_contact_id         TEXT,
    name                      TEXT,
    role                      TEXT,
    email                     TEXT,
    phone                     TEXT,
    source_url                TEXT,
    confidence_score          DOUBLE PRECISION,
    confidence_reasons        JSONB,
    preferred_for_submissions BOOLEAN NOT NULL DEFAULT FALSE,
    verified_at               TEXT,
    provenance                JSONB,
    first_stored_at           TEXT NOT NULL,
    last_stored_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contacts_org ON contacts(identity_key);

CREATE TABLE IF NOT EXISTS submission_paths (
    identity_key    TEXT PRIMARY KEY REFERENCES organizations(identity_key) ON DELETE CASCADE,
    payload         JSONB NOT NULL,
    first_stored_at TEXT NOT NULL,
    last_stored_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_fetches (
    fetch_id     BIGSERIAL PRIMARY KEY,
    identity_key TEXT NOT NULL REFERENCES organizations(identity_key) ON DELETE CASCADE,
    url          TEXT NOT NULL,
    ok           BOOLEAN,
    status       INTEGER,
    error_kind   TEXT,
    fetched_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_fetches_org ON source_fetches(identity_key);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id           TEXT PRIMARY KEY,
    source           TEXT NOT NULL,
    started_at       TEXT NOT NULL,
    completed_at     TEXT,
    records_accepted INTEGER,
    records_failed   INTEGER
);

CREATE TABLE IF NOT EXISTS ingestion_failures (
    failure_id BIGSERIAL PRIMARY KEY,
    run_id     TEXT NOT NULL REFERENCES ingestion_runs(run_id),
    stage      TEXT,
    error_kind TEXT,
    message    TEXT,
    url        TEXT
);

CREATE TABLE IF NOT EXISTS verification_runs (
    run_id       TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    completed_at TEXT,
    summary      JSONB,
    source       TEXT
);

CREATE TABLE IF NOT EXISTS verification_results (
    result_id    BIGSERIAL PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES verification_runs(run_id) ON DELETE CASCADE,
    identity_key TEXT NOT NULL REFERENCES organizations(identity_key) ON DELETE CASCADE,
    claim        TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN (
                     'unverified', 'verified', 'failed', 'stale',
                     'conflicting', 'unsupported')),
    method       TEXT,
    verifier     TEXT,
    evidence     JSONB,
    reasons      JSONB,
    checked_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verification_org
    ON verification_results(identity_key);
