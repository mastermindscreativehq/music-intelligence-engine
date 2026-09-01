-- Phase 9: PostgreSQL/Supabase mirror of SQLite schema v4 (Phase 9).
--
-- outreach_messages: the operator's outreach draft/record. A message row is
-- never marked 'sent' merely because a mail client opened; only a
-- provider-confirmed send earns that status (vocabulary:
-- draft | opened_in_email | sent | failed). track references are stored by
-- opaque track_id (a foreign key into tracks) but a message may also carry
-- freeform track metadata captured at compose time.
-- outreach_attempts: append-only ledger of every delivery handoff/event.
-- Timestamps stay TEXT (ISO-8601) exactly like every other table so payloads
-- are byte-identical across backends; JSON fields stay JSONB for Postgres.

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
    track            JSONB,              -- track metadata object
    context          JSONB,              -- artist/track context object
    subject          TEXT,
    message          TEXT,
    from_email       TEXT,
    sharing          JSONB,              -- sharing options object
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
    attempt_id    BIGSERIAL PRIMARY KEY,
    outreach_id   TEXT NOT NULL REFERENCES outreach_messages(outreach_id)
                  ON DELETE CASCADE,
    event         TEXT NOT NULL CHECK (event IN
                    ('opened_in_email', 'sent', 'failed')),
    provider      TEXT NOT NULL DEFAULT 'local',
    "at"          TEXT NOT NULL,
    meta          JSONB
);
CREATE INDEX IF NOT EXISTS idx_outreach_attempts_msg
    ON outreach_attempts(outreach_id);