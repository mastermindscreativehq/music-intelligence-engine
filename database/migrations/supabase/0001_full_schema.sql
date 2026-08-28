-- ============================================================================
-- Music Intelligence Engine — FINAL Supabase PostgreSQL schema
-- File:    database/migrations/supabase/0001_full_schema.sql
-- Purpose: single self-contained script for manual review and execution in
--          the Supabase SQL Editor against an EMPTY project.
-- ============================================================================
--
-- HOW TO RUN
--   Supabase Dashboard -> SQL Editor -> paste entire file -> Run.
--   The whole script is wrapped in one transaction: either everything is
--   created or nothing changes. Every DDL statement uses IF NOT EXISTS, so
--   an accidental second run is a harmless no-op.
--
-- RELATIONSHIP TO THE REPOSITORY
--   Sections A and B reproduce the TESTED contract of
--   database/migrations/postgres/0001_init.sql and 0002_submissions.sql
--   verbatim (same tables, columns, constraints, indexes). Those files plus
--   database/pg_store.py remain the source of truth that application code
--   and the automatic migration runner (apply_pg_migrations) use. Section D
--   stamps the schema_migrations ledger so that pointing the backend at this
--   database later (MIE_PG_DSN) will NOT re-issue those migrations.
--   Section C adds the ONLY new structure approved in review: per-target
--   OUTREACH STATUS tracking, kept strictly separate from intelligence data.
--
-- ARCHITECTURE SUMMARY (docs/data-model.md is the governing document)
--   * Generic entity model: radio_station is organization_type='radio_station';
--     playlist curators, blogs/publications, DJs, labels/A&R, festivals,
--     influencers reuse these tables without structural change.
--   * One organization -> many contacts -> multiple channels (org-level fact
--     tables + per-contact email/phone fields).
--   * Provenance is preserved VERBATIM: every discovered value keeps its full
--     Fact dict (value / source_url / source_type / method / discovered_at /
--     also_seen_at) inside JSONB columns; nothing found is ever flattened or
--     destroyed.
--   * Found != verified != relevant: confidence always ships with reasons;
--     verification history is append-only with a six-value vocabulary.
--   * Outreach state lives in its own tables keyed by identity_key /
--     contact_uid so future campaign activity can never corrupt intelligence.
--   * Timestamps are TEXT ISO-8601 BY DESIGN: both storage backends (SQLite
--     reference and PostgreSQL) return byte-identical payload shapes.
--
BEGIN;

-- ============================================================================
-- SECTION A — Core intelligence  (mirrors 0001_init.sql verbatim)
-- ============================================================================

-- Organizations: the generic industry entity. Radio stations are rows with
-- organization_type='radio_station'. identity_key is the stable business
-- identity ('domain:<registrable-domain>' or 'namegeo:<slug>') shared with
-- the discovery engine, making repeated ingestion idempotent.
CREATE TABLE IF NOT EXISTS organizations (
    identity_key              TEXT PRIMARY KEY,   -- 'domain:x' | 'namegeo:y'
    identity_kind             TEXT NOT NULL,
    name                      TEXT NOT NULL,      -- station_name for radio
    organization_type         TEXT,               -- radio_station|playlist_curator|dj|blog|publication|label|a_and_r|festival|event|influencer|creator|other (vocabulary documented, intentionally NOT constrained: new verticals must not require a migration)
    website                   TEXT,
    domain                    TEXT,               -- normalized registrable domain
    country                   TEXT,
    state_or_region           TEXT,               -- spec field "state"
    city                      TEXT,
    market_area               TEXT,
    station_type              TEXT,
    classification_confidence DOUBLE PRECISION,
    classification_evidence   JSONB,              -- array of Fact dicts
    formats                   JSONB,              -- array: e.g. ["college","community"]
    genres                    JSONB,              -- array: multi-genre support
    genre_evidence            JSONB,              -- object: genre -> supporting facts
    language                  TEXT,
    description               TEXT,
    social_urls               JSONB,              -- social links: platform -> url
    source_urls               JSONB,              -- array of page URLs crawled
    discovered_at             TEXT,
    last_verified_at          TEXT,
    last_observed_at          TEXT,
    confidence_score          DOUBLE PRECISION,   -- aggregate org/email confidence
    confidence_reasons        JSONB,              -- explainable confidence
    status                    TEXT,               -- operational status from evidence
    raw_metadata              JSONB,
    first_stored_at           TEXT NOT NULL,
    last_stored_at            TEXT NOT NULL
);

-- Organization-level EMAIL channels. One row per distinct address; the full
-- provenance Fact dict is stored verbatim so FACT metadata never degrades.
CREATE TABLE IF NOT EXISTS organization_emails (
    identity_key TEXT NOT NULL REFERENCES organizations(identity_key) ON DELETE CASCADE,
    value        TEXT NOT NULL,
    fact         JSONB NOT NULL,        -- {value, source_url, source_type, method, discovered_at, also_seen_at}
    PRIMARY KEY (identity_key, value)
);

-- Organization-level PHONE channels (same contract as emails).
CREATE TABLE IF NOT EXISTS organization_phones (
    identity_key TEXT NOT NULL REFERENCES organizations(identity_key) ON DELETE CASCADE,
    value        TEXT NOT NULL,
    fact         JSONB NOT NULL,
    PRIMARY KEY (identity_key, value)
);

-- Contacts: people or roles at an organization (1 org -> many contacts).
-- contact_uid is a content hash (sha256[:32]) of the stable business fields,
-- so engine-run UUIDs can never create duplicates across ingestions.
-- Attribution discipline: name/role are only set from explicit on-page
-- evidence; each attribution carries its own provenance entry
-- (role_label_rule / name_adjacency_rule). Unattributed observations stay.
CREATE TABLE IF NOT EXISTS contacts (
    contact_uid               TEXT PRIMARY KEY,
    identity_key              TEXT NOT NULL REFERENCES organizations(identity_key) ON DELETE CASCADE,
    engine_contact_id         TEXT,             -- engine-run UUID (audit only)
    name                      TEXT,             -- contact_name (NULL = role-only/observed)
    role                      TEXT,             -- contact_role: music_director, program_director, ...
    email                     TEXT,             -- primary channel (denormalized)
    phone                     TEXT,             -- formatted as found, e.g. "(555) 010-2468"
    source_url                TEXT,             -- page the contact was extracted from
    confidence_score          DOUBLE PRECISION, -- contact_confidence
    confidence_reasons        JSONB,
    preferred_for_submissions BOOLEAN NOT NULL DEFAULT FALSE,
    verified_at               TEXT,             -- last human/automated verification
    provenance                JSONB,            -- append-only list of Fact dicts
    first_stored_at           TEXT NOT NULL,
    last_stored_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contacts_org ON contacts(identity_key);

-- Submission path intelligence: ONE current path per organization stored
-- verbatim as produced by the enrichment engine — URL, instructions,
-- restrictions, and inference-labeled methods bundle included.
CREATE TABLE IF NOT EXISTS submission_paths (
    identity_key    TEXT PRIMARY KEY REFERENCES organizations(identity_key) ON DELETE CASCADE,
    payload         JSONB NOT NULL,     -- SubmissionPath.to_dict()
    first_stored_at TEXT NOT NULL,
    last_stored_at  TEXT NOT NULL
);

-- Fetch outcomes per organization; final state mirrors the last ingestion
-- (delete-then-insert within one transaction by the storage layer).
CREATE TABLE IF NOT EXISTS source_fetches (
    fetch_id     BIGSERIAL PRIMARY KEY,
    identity_key TEXT NOT NULL REFERENCES organizations(identity_key) ON DELETE CASCADE,
    url          TEXT NOT NULL,
    ok           BOOLEAN,
    status       INTEGER,               -- HTTP status when reached
    error_kind   TEXT,                  -- timeout | robots_disallowed | http_status | ...
    fetched_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_fetches_org ON source_fetches(identity_key);

-- Ingestion audit ledger: one row per run plus per-record failures.
CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id           TEXT PRIMARY KEY,
    source           TEXT NOT NULL,     -- 'cli' | 'api' | 'tests'
    started_at       TEXT NOT NULL,
    completed_at     TEXT,
    records_accepted INTEGER,
    records_failed   INTEGER
);

CREATE TABLE IF NOT EXISTS ingestion_failures (
    failure_id BIGSERIAL PRIMARY KEY,
    run_id     TEXT NOT NULL REFERENCES ingestion_runs(run_id),
    stage      TEXT,                    -- 'validation' | 'storage'
    error_kind TEXT,
    message    TEXT,
    url        TEXT
);

-- Verification workflow: append-only history. Re-running verification never
-- rewrites prior results. Status vocabulary is a closed contract:
--   unverified | verified | failed | stale | conflicting | unsupported
CREATE TABLE IF NOT EXISTS verification_runs (
    run_id       TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    completed_at TEXT,
    summary      JSONB,                 -- counts per status
    source       TEXT
);

CREATE TABLE IF NOT EXISTS verification_results (
    result_id    BIGSERIAL PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES verification_runs(run_id) ON DELETE CASCADE,
    identity_key TEXT NOT NULL REFERENCES organizations(identity_key) ON DELETE CASCADE,
    claim        TEXT NOT NULL,         -- verbatim claim from enrichment.verify
    status       TEXT NOT NULL CHECK (status IN (
                     'unverified', 'verified', 'failed', 'stale',
                     'conflicting', 'unsupported')),
    method       TEXT,
    verifier     TEXT,                  -- 'rule' | 'human:<name>' | model id
    evidence     JSONB,                 -- provenance preserved
    reasons      JSONB,
    checked_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verification_org
    ON verification_results(identity_key);

-- ============================================================================
-- SECTION B — Submission assets & link accessibility (0002_submissions.sql)
-- ============================================================================

-- Content-addressed music library. track_id ('sha256:<hex>') is an OPAQUE
-- public identifier; audio blobs live OUTSIDE the database and are never
-- persisted here (storage backend owns key->location mapping).
CREATE TABLE IF NOT EXISTS tracks (
    track_id          TEXT PRIMARY KEY,   -- 'sha256:<hex>'
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

-- Append-only accessibility history per checked submission/instructions URL;
-- mirrors the SourceFetchRecord vocabulary so failure kinds stay comparable.
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

-- ============================================================================
-- SECTION C — Outreach status tracking (approved additive extension)
-- ============================================================================
-- Requirement: track outreach_status WITHOUT corrupting intelligence data.
-- These tables are deliberately SEPARATE from the intelligence tables above:
-- the discovery/extraction engine never writes here, so outreach state can
-- be freely edited, replayed, or wiped without touching provenance.
-- Application wiring (console/API writes, campaign linkage) arrives with the
-- outreach phases; the status vocabulary is intentionally unconstrained
-- beyond non-emptiness until that workflow pins it.
--
-- Scope: one CURRENT row per target. contact_uid NULL means the status is at
-- the ORGANIZATION level; non-NULL pins it to a specific person/role.
-- Two partial unique indexes give "one row per (org)" and "one row per
-- (org, contact)" without relying on PG15+ UNIQUE NULLS NOT DISTINCT.

CREATE TABLE IF NOT EXISTS outreach_status (
    outreach_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    identity_key TEXT NOT NULL REFERENCES organizations(identity_key)
                 ON DELETE CASCADE,
    contact_uid  TEXT REFERENCES contacts(contact_uid) ON DELETE CASCADE,
    status       TEXT NOT NULL CHECK (length(btrim(status)) > 0),
    notes        TEXT,
    updated_by   TEXT,                  -- operator name / process id
    created_at   TEXT NOT NULL,         -- ISO-8601, writer-supplied
    updated_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_outreach_org_level
    ON outreach_status(identity_key) WHERE contact_uid IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_outreach_contact_level
    ON outreach_status(identity_key, contact_uid)
    WHERE contact_uid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_outreach_by_status ON outreach_status(status);

-- Append-only transition history; the current table above is a projection.
CREATE TABLE IF NOT EXISTS outreach_status_history (
    history_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    identity_key TEXT NOT NULL REFERENCES organizations(identity_key)
                 ON DELETE CASCADE,
    contact_uid  TEXT REFERENCES contacts(contact_uid) ON DELETE CASCADE,
    status       TEXT NOT NULL CHECK (length(btrim(status)) > 0),
    notes        TEXT,
    changed_by   TEXT,
    changed_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outreach_history_target
    ON outreach_status_history(identity_key, contact_uid);

-- ============================================================================
-- SECTION D — Migration ledger stamping
-- ============================================================================
-- Recreates the exact ledger shape the repository's automatic runner uses
-- (database/schema_migrations.py) and marks versions 1 and 2 as applied, so
-- enabling the PostgreSQL backend against this database later will not
-- re-issue Section A/B DDL.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    filename TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO schema_migrations (version, filename) VALUES
    (1, '0001_init.sql'),
    (2, '0002_submissions.sql')
ON CONFLICT (version) DO NOTHING;

COMMIT;

-- ============================================================================
-- SECTION E — Documentation only (NOT executed by this script)
-- ============================================================================
--
-- E.1 Row Level Security — MANUAL POST-SCHEMA STEP (run when ready).
--     Enabling RLS with NO policies = deny-by-default for anon/authenticated
--     PostgREST roles while direct DSN connections (backend owner) are
--     unaffected. Never add permissive public policies for convenience.
--
--     ALTER TABLE organizations          ENABLE ROW LEVEL SECURITY;
--     ALTER TABLE organization_emails    ENABLE ROW LEVEL SECURITY;
--     ALTER TABLE organization_phones    ENABLE ROW LEVEL SECURITY;
--     ALTER TABLE contacts               ENABLE ROW LEVEL SECURITY;
--     ALTER TABLE submission_paths       ENABLE ROW LEVEL SECURITY;
--     ALTER TABLE source_fetches         ENABLE ROW LEVEL SECURITY;
--     ALTER TABLE ingestion_runs         ENABLE ROW LEVEL SECURITY;
--     ALTER TABLE ingestion_failures     ENABLE ROW LEVEL SECURITY;
--     ALTER TABLE verification_runs      ENABLE ROW LEVEL SECURITY;
--     ALTER TABLE verification_results   ENABLE ROW LEVEL SECURITY;
--     ALTER TABLE tracks                 ENABLE ROW LEVEL SECURITY;
--     ALTER TABLE submission_link_checks ENABLE ROW LEVEL SECURITY;
--     ALTER TABLE outreach_status        ENABLE ROW LEVEL SECURITY;
--     ALTER TABLE outreach_status_history ENABLE ROW LEVEL SECURITY;
--
-- E.2 Deferred until their phases ship (NO premature infrastructure):
--     campaigns, campaign_recipients, messages, delivery_events,
--     sent submissions, suppressions/opt-outs. Future campaign_recipients
--     rows will reference contacts(contact_uid); suppression checks gate any
--     send path before it exists.
--
-- E.3 Adding a future vertical (curators, DJs, labels, festivals, ...):
--     no DDL required — insert organizations with the appropriate
--     organization_type; contacts/channels/provenance/outreach apply as-is.
-- ============================================================================
